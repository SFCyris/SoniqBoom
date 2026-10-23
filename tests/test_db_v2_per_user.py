# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-user listening data and the versioned library database (V1 → V2).

* A start that finds a V1 database (a snapshot without ``db_version``, or AOF
  records without ``uid``) backs the files up first, converts them (the data
  goes to the legacy owner — the oldest admin), writes a V2 snapshot and
  empties the AOF; a second start does nothing; a crash before the V2 write
  converts again at the next start; a failed backup converts nothing; a
  newer database is refused.
* Plays, history and unplayed lists are per user; the history cap is per user.
* The live store, AOF replay (``merger._apply_entry``) and its undo
  (``persistence._entry_undo``) agree on the per-user ops.
* Track-id remaps move every user's data (HVSC: both copies keep their own).
* ``.sbz`` backups: a V1 backup goes to the legacy owner, a V2 one maps users
  by name (the rest kept aside until such an account exists), a newer one is
  refused.
* A deleted account's plays and history go; plays made with no account go to
  the first account.
"""
from __future__ import annotations

import asyncio
import copy
import gzip
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from soniqboom.core import merger, persistence
from soniqboom.core.persistence import DB_VERSION, DatabaseVersionError
from soniqboom.core.store import TrackStore
from soniqboom.core.user_data import HISTORY_MAX, OWNER_CONFIG_KEY

ROOT = Path(__file__).resolve().parent.parent

SFC = "uid-sfc"                     # the oldest admin
LATE_ADMIN = "uid-admin2"
READER = "uid-reader"
BOB = "uid-bob"


@pytest.fixture()
def fresh_store():
    from soniqboom.core import store as store_mod
    old = store_mod._store
    store_mod._store = None
    yield
    store_mod._store = old
    persistence.release_library()


def _users(d: Path, *recs) -> None:
    """A users.json as the user store writes it (the fields read here)."""
    users = [{"id": uid, "username": name, "password_hash": "x", "role": role,
              "created_at": created, "enabled": True}
             for uid, name, role, created in recs]
    (d / "users.json").write_text(json.dumps({"users": users}))


def _std_users(d: Path) -> None:
    _users(d, (READER, "reader", "readonly", 50.0),          # oldest account, not an admin
              (LATE_ADMIN, "admin2", "admin", 300.0),
              (SFC, "sfc", "admin", 100.0))


def _v1(d: Path) -> None:
    """A V1 data directory: a snapshot with flat play stats / history, an AOF
    with records without ``uid``, the radio favourites and Subsonic state."""
    state = {
        "tracks": {t: {"id": t, "path": f"/m/{t}.mp3", "title": t} for t in ("t1", "t2", "t3")},
        "ratings": {"t1": 4},
        "play_stats": {"t1": {"count": 3, "last_played": 100}},
        "history": [{"track_id": "t1", "title": "t1", "artist": "", "ts": 100}],
        "config": {"x": 1},
    }
    (d / "library.json").write_text(json.dumps(state))
    with open(d / "library.aof", "w") as f:
        for r in ({"op": "record_play", "ts": 200, "id": "t2"},
                  {"op": "push_history", "ts": 200,
                   "data": {"track_id": "t2", "title": "t2", "artist": "", "ts": 200}},
                  {"op": "set_rating", "ts": 201, "id": "t3", "rating": 2}):
            f.write(json.dumps(r) + "\n")
    (d / "radio").mkdir(exist_ok=True)
    (d / "radio" / "favorites.json").write_text(json.dumps([{"id": "custom:x"}]))
    (d / "subsonic_state.json").write_text(json.dumps({"stars": {}}))


def _originals(d: Path) -> dict:
    return {rel: (d / rel).read_bytes() for rel in
            ("library.json", "library.aof", "radio/favorites.json", "subsonic_state.json")}


def _backups(d: Path) -> list[Path]:
    root = d / "backups"
    return sorted(p for p in root.iterdir() if not p.name.startswith(".")) if root.exists() else []


def _aof_records(d: Path) -> list[dict]:
    p = d / "library.aof"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# ── The V1 → V2 upgrade at a start ──────────────────────────────────────────

def test_a_v1_database_is_backed_up_then_converted_to_the_oldest_admin(tmp_path, fresh_store):
    from soniqboom.core.store import get_store
    _v1(tmp_path)
    _std_users(tmp_path)
    before = _originals(tmp_path)
    persistence.init_persistence(tmp_path)

    # The backup: one folder, byte-identical copies, a README.
    (bk,) = _backups(tmp_path)
    assert bk.name.startswith("db-v1-")
    for rel, raw in before.items():
        assert (bk / rel).read_bytes() == raw, rel
    readme = (bk / "README.txt").read_text()
    assert "Stop the SoniqBoom server" in readme and str(tmp_path) in readme
    assert not list((tmp_path / "backups").glob(".*incomplete"))

    # The new snapshot: V2, the flat data under the legacy owner (sfc, the
    # oldest ADMIN — not the older readonly account), the AOF folded in.
    snap = json.loads((tmp_path / "library.json").read_text())
    assert snap["db_version"] == DB_VERSION == 2
    assert snap["play_stats"] == {SFC: {"t1": {"count": 3, "last_played": 100},
                                        "t2": {"count": 1, "last_played": 200}}}
    assert [e["track_id"] for e in snap["history"][SFC]] == ["t1", "t2"]
    assert set(snap["history"]) == {SFC}
    assert snap["ratings"] == {"t1": 4, "t3": 2}                 # ratings stay library-wide
    assert snap["config"][OWNER_CONFIG_KEY] == SFC and snap["config"]["x"] == 1
    # No V1 record is left in the AOF.
    assert all(isinstance(r.get("uid"), str) for r in _aof_records(tmp_path)
               if r.get("op") in ("record_play", "push_history"))
    assert (tmp_path / "library.aof").stat().st_size == 0
    # The live store holds the same.
    st = get_store()
    assert st.get_play_stats("t2", uid=SFC)["count"] == 1
    assert st.get_play_stats("t2", uid="")["count"] == 0
    assert st.user_data_owner() == SFC
    # The files the backup is of are left as they were (only library.json and the AOF change).
    assert (tmp_path / "radio" / "favorites.json").read_bytes() == before["radio/favorites.json"]


def test_a_second_start_does_nothing(tmp_path, fresh_store):
    _v1(tmp_path)
    _std_users(tmp_path)
    persistence.init_persistence(tmp_path)
    snap = (tmp_path / "library.json").read_bytes()
    persistence.init_persistence(tmp_path)
    assert len(_backups(tmp_path)) == 1                           # no second backup
    assert (tmp_path / "library.json").read_bytes() == snap       # not rewritten


def test_a_crash_after_the_backup_converts_again_at_the_next_start(tmp_path, fresh_store,
                                                                     monkeypatch):
    _v1(tmp_path)
    _std_users(tmp_path)
    before = _originals(tmp_path)

    class _Died(BaseException):
        """A SIGKILL between the backup and the V2 snapshot."""

    def die(*a, **k):
        raise _Died
    monkeypatch.setattr(persistence, "write_library_file", die)
    with pytest.raises(_Died):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    # The V1 files are untouched; the backup is complete.
    assert _originals(tmp_path) == before
    (bk,) = _backups(tmp_path)
    assert (bk / "library.json").read_bytes() == before["library.json"]

    persistence.init_persistence(tmp_path)                        # the next start
    snap = json.loads((tmp_path / "library.json").read_text())
    assert snap["db_version"] == 2
    assert snap["play_stats"][SFC]["t2"]["count"] == 1            # once, not twice
    assert snap["play_stats"][SFC]["t1"]["count"] == 3
    assert len(snap["history"][SFC]) == 2
    assert len(_backups(tmp_path)) == 1                           # the backup was reused


def test_a_crash_after_the_v2_write_before_the_aof_drop_counts_nothing_twice(
        tmp_path, fresh_store, monkeypatch):
    _v1(tmp_path)
    _std_users(tmp_path)

    class _Died(BaseException):
        pass

    def die(*a, **k):
        raise _Died
    monkeypatch.setattr(persistence, "drop_aof_prefix", die)
    with pytest.raises(_Died):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    # V2 snapshot written, the V1 records still in the AOF — but held by its mark.
    assert json.loads((tmp_path / "library.json").read_text())["db_version"] == 2
    assert (tmp_path / "library.aof").stat().st_size > 0
    persistence.init_persistence(tmp_path)
    assert len(_backups(tmp_path)) == 1                           # nothing left to convert
    from soniqboom.core.store import get_store
    assert get_store().get_play_stats("t2", uid=SFC)["count"] == 1


def test_a_failed_backup_converts_nothing(tmp_path, fresh_store, monkeypatch):
    _v1(tmp_path)
    _std_users(tmp_path)
    before = _originals(tmp_path)

    def no_space(src, dst, *a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(persistence.shutil, "copyfile", no_space)
    with pytest.raises(DatabaseVersionError, match="could not be backed up"):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    assert _originals(tmp_path) == before                         # nothing converted
    assert _backups(tmp_path) == []                               # no partial folder kept
    assert not list((tmp_path / "backups").glob(".*"))


def test_a_newer_database_is_refused(tmp_path, fresh_store):
    state = {"db_version": DB_VERSION + 1, "tracks": {"t1": {"id": "t1", "path": "/m/t1"}}}
    (tmp_path / "library.json").write_text(json.dumps(state))
    raw = (tmp_path / "library.json").read_bytes()
    with pytest.raises(DatabaseVersionError) as ei:
        persistence.init_persistence(tmp_path)
    assert f"V{DB_VERSION + 1}" in str(ei.value) and str(tmp_path / "backups") in str(ei.value)
    assert (tmp_path / "library.json").read_bytes() == raw
    # The merger leaves it alone too.
    (tmp_path / "library.aof").write_text(json.dumps(
        {"op": "record_play", "ts": 1, "id": "t1", "uid": ""}) + "\n")
    assert merger._do_merge(tmp_path) == 0
    assert (tmp_path / "library.json").read_bytes() == raw


def test_v1_records_on_a_v2_snapshot_are_backed_up_and_go_to_the_owner(tmp_path, fresh_store):
    """An AOF an older version wrote on top of a V2 snapshot (a downgrade and
    back): its records without uid go to the recorded owner, after a backup."""
    state = {"db_version": 2, "tracks": {"t1": {"id": "t1", "path": "/m/t1"}},
             "play_stats": {SFC: {"t1": {"count": 1, "last_played": 5}}},
             "config": {OWNER_CONFIG_KEY: SFC}}
    (tmp_path / "library.json").write_text(json.dumps(state))
    (tmp_path / "library.aof").write_text(json.dumps(
        {"op": "record_play", "ts": 9, "id": "t1"}) + "\n")
    persistence.init_persistence(tmp_path)
    assert len(_backups(tmp_path)) == 1
    snap = json.loads((tmp_path / "library.json").read_text())
    assert snap["play_stats"] == {SFC: {"t1": {"count": 2, "last_played": 9}}}


def test_without_accounts_the_v1_data_stays_unowned_until_the_first_account(tmp_path,
                                                                            fresh_store):
    from soniqboom.core.store import get_store
    from soniqboom.core.user_data import claim_unowned
    _v1(tmp_path)
    persistence.init_persistence(tmp_path)
    st = get_store()
    snap = json.loads((tmp_path / "library.json").read_text())
    assert set(snap["play_stats"]) == {""} and OWNER_CONFIG_KEY not in snap["config"]
    aof: list = []
    st._aof_append = lambda op, **kw: aof.append((op, kw))
    users = types.SimpleNamespace(list_users=lambda: [])
    assert not claim_unowned(st, users)                           # still no account
    first = types.SimpleNamespace(id="first", role="admin", created_at=10.0, enabled=True)
    users = types.SimpleNamespace(list_users=lambda: [first])
    assert claim_unowned(st, users)
    assert st.get_play_stats("t2", uid="first")["count"] == 1
    assert st.get_history(10, uid="") == [] and len(st.get_history(10, uid="first")) == 2
    assert st.user_data_owner() == "first"
    assert aof == [("set_config", {"key": OWNER_CONFIG_KEY, "value": "first"}),
                   ("move_user_data", {"src": "", "dst": "first"})]
    assert not claim_unowned(st, users)                           # done: nothing again
    # The two records replay to the same state.
    state = {"play_stats": {"": {"t2": {"count": 1, "last_played": 200}}},
             "history": {"": [{"track_id": "t2", "ts": 200}]}, "config": {}}
    for op, kw in aof:
        merger._apply_entry(state, {"op": op, **kw})
    assert state["play_stats"] == {"first": {"t2": {"count": 1, "last_played": 200}}}
    assert state["config"][OWNER_CONFIG_KEY] == "first"


def test_plays_with_no_account_go_to_the_first_account_on_a_reload(monkeypatch):
    """The server learns of its first account by a reload / a restored
    users.json (soniqboom-setadm is another process):
    ``users.claim_unowned_user_data`` moves the no-user bucket."""
    from soniqboom.api import users as users_api
    from soniqboom.core import store as store_mod
    st = TrackStore()
    st.record_play("t1", uid="")
    st.push_history({"track_id": "t1", "ts": 1}, uid="")
    monkeypatch.setattr(store_mod, "get_store", lambda: st)
    admin = types.SimpleNamespace(id="adm", role="admin", created_at=5.0, enabled=True)
    monkeypatch.setattr(users_api, "get_user_store",
                        lambda: types.SimpleNamespace(list_users=lambda: [admin]))
    assert users_api.claim_unowned_user_data()
    assert st.get_play_stats("t1", uid="adm")["count"] == 1
    assert "" not in st._play_stats and "" not in st._history


def test_the_legacy_owner_is_the_oldest_enabled_admin():
    from soniqboom.core.users import legacy_owner_id
    recs = [{"id": "r", "role": "readonly", "created_at": 1},
            {"id": "a-off", "role": "admin", "created_at": 2, "enabled": False},
            {"id": "a2", "role": "admin", "created_at": 9},
            {"id": "a1", "role": "admin", "created_at": 5}]
    assert legacy_owner_id(recs) == "a1"
    assert legacy_owner_id(recs[:2]) == "a-off"                   # only a disabled admin
    assert legacy_owner_id(recs[:1]) == "r"                       # no admin at all
    assert legacy_owner_id([]) is None


# ── Per-user maps in the store ──────────────────────────────────────────────

def _lib_store(n: int = 4) -> TrackStore:
    st = TrackStore()
    for i in range(n):
        st.upsert_track({"id": f"t{i}", "path": f"/m/t{i}.mp3", "title": f"t{i}",
                         "added_at": i})
    return st


def test_two_users_plays_history_and_unplayed_are_isolated():
    st = _lib_store()
    st.record_play("t0", uid="alice")
    st.record_play("t0", uid="alice")
    st.record_play("t1", uid="bob")
    st.push_history({"track_id": "t0", "ts": 1}, uid="alice")
    st.push_history({"track_id": "t1", "ts": 2}, uid="bob")
    assert st.get_play_stats("t0", uid="alice")["count"] == 2
    assert st.get_play_stats("t0", uid="bob") == {"count": 0}
    assert st.get_all_play_stats(uid="bob") == {"t1": {"count": 1, "last_played": st.get_play_stats("t1", uid="bob")["last_played"]}}
    assert st.get_play_stats_batch(["t0", "t1"], uid="alice").keys() == {"t0"}
    assert [h["track_id"] for h in st.get_history(10, uid="alice")] == ["t0"]
    assert [h["track_id"] for h in st.get_history(10, uid="bob")] == ["t1"]
    assert st.get_history(10, uid="carol") == []
    assert [t["id"] for t in st.list_unplayed(10, uid="alice")] == ["t3", "t2", "t1"]
    assert [t["id"] for t in st.list_unplayed(10, uid="bob")] == ["t3", "t2", "t0"]
    assert len(st.list_unplayed(10, uid="carol")) == 4
    # uid is required and must be a str: no silent write to a default user.
    with pytest.raises(TypeError):
        st.record_play("t0")                                     # type: ignore[call-arg]
    with pytest.raises(TypeError):
        st.record_play("t0", uid=None)                           # type: ignore[arg-type]
    with pytest.raises(TypeError):
        st.push_history({"track_id": "t0", "ts": 1})              # type: ignore[call-arg]


def test_the_history_cap_is_per_user_live_and_on_replay():
    st = _lib_store(1)
    aof: list = []
    st._aof_append = lambda op, **kw: aof.append({"op": op, **kw})
    for i in range(HISTORY_MAX + 10):
        st.push_history({"track_id": "t0", "ts": i}, uid="alice")
    for i in range(3):
        st.push_history({"track_id": "t0", "ts": i}, uid="bob")
    assert len(st.get_history(10_000, uid="alice")) == HISTORY_MAX == 500
    assert st.get_history(1, uid="alice")[0]["ts"] == HISTORY_MAX + 9
    assert len(st.get_history(10_000, uid="bob")) == 3
    state: dict = {}
    for rec in aof:
        merger._apply_entry(state, rec)
    assert state["history"] == st._history


def test_the_snapshot_is_v2_and_loads_back():
    st = _lib_store(2)
    st.record_play("t0", uid="alice")
    st.push_history({"track_id": "t0", "ts": 1}, uid="alice")
    snap = json.loads(json.dumps(st.to_snapshot()))
    assert snap["db_version"] == DB_VERSION
    assert snap["play_stats"] == {"alice": st._play_stats["alice"]}
    assert snap["history"] == {"alice": [{"track_id": "t0", "ts": 1}]}


# ── Live store, AOF replay and undo agree ───────────────────────────────────

def _ops_script(st: TrackStore) -> None:
    st.record_play("t0", uid="alice")
    st.record_play("t0", at=5, uid="alice")
    st.record_play("t1", uid="bob")
    st.record_play("t2", uid="")
    st.push_history({"track_id": "t0", "ts": 10}, uid="alice")
    st.push_history({"track_id": "t1", "ts": 5}, uid="bob")
    st.push_history({"track_id": "t2", "ts": 3}, uid="")
    st.set_rating("t0", 5)
    st.set_rating("t3", 1)
    st.record_play("t3", uid="bob")
    st.remap_track_ids({"t0": "n0"})
    st.remap_track_ids({"t3": "n0", "t1": "n1"}, keep_both=True)
    st.set_config(OWNER_CONFIG_KEY, "alice")
    st.move_user_data("", "alice")
    st.record_play("n1", uid="carol")
    st.push_history({"track_id": "n1", "ts": 7}, uid="carol")
    st.delete_user_data("carol")


def test_replay_and_merge_give_the_live_store():
    st = _lib_store()
    aof: list = []
    st._aof_append = lambda op, **kw: aof.append({"op": op, "ts": 1, **kw})
    _ops_script(st)
    state = {"db_version": 2, "tracks": {}}
    for rec in aof:
        merger._apply_entry(state, rec)
    live = json.loads(json.dumps(st.to_snapshot()))
    for key in ("play_stats", "history", "ratings", "config"):
        assert state.get(key, {}) == live[key], key
    assert "carol" not in live["play_stats"] and "" not in live["play_stats"]


@pytest.mark.parametrize("op", ["record_play", "push_history", "remap_track_ids",
                                "remap_keep_both", "move_user_data", "delete_user_data",
                                "record_play_v1"])
def test_every_per_user_op_rolls_back_exactly(op):
    """``_entry_undo`` captures what ``_apply_entry`` touches: an op that
    fails after its change (simulated) leaves the state as it was."""
    base = {
        "db_version": 2, "tracks": {},
        "ratings": {"a": 3, "b": 4},
        "play_stats": {"u1": {"a": {"count": 2, "last_played": 9}, "b": {"count": 1, "last_played": 1}},
                       "u2": {"a": {"count": 1, "last_played": 3}},
                       "": {"c": {"count": 1, "last_played": 4}}},
        "history": {"u1": [{"track_id": "a", "ts": 1}], "": [{"track_id": "c", "ts": 2}]},
        "playlists": {"p": {"id": "p", "track_ids": ["a"]}},
        "waveforms": {"a": [0.1]},
        "config": {OWNER_CONFIG_KEY: "u1"},
    }
    entry = {
        "record_play": {"op": "record_play", "id": "a", "ts": 20, "uid": "u1"},
        "record_play_v1": {"op": "record_play", "id": "z", "ts": 20},
        "push_history": {"op": "push_history", "data": {"track_id": "b", "ts": 0}, "uid": "u3"},
        "remap_track_ids": {"op": "remap_track_ids", "mapping": {"a": "b"}},
        "remap_keep_both": {"op": "remap_track_ids", "mapping": {"a": "b"}, "keep_both": True},
        "move_user_data": {"op": "move_user_data", "src": "", "dst": "u2"},
        "delete_user_data": {"op": "delete_user_data", "uid": "u1"},
    }[op]
    state = copy.deepcopy(base)
    undo = persistence._entry_undo(state, entry)
    assert undo is not None
    merger._apply_entry(state, entry)
    assert state != base                                           # (it did change something)
    persistence._entry_restore(state, undo)
    assert state == base


def test_a_uidless_record_goes_to_the_legacy_owner():
    state = {"config": {OWNER_CONFIG_KEY: "u1"}}
    merger._apply_entry(state, {"op": "record_play", "id": "a", "ts": 3})
    merger._apply_entry(state, {"op": "push_history", "data": {"track_id": "a", "ts": 3}})
    assert state["play_stats"] == {"u1": {"a": {"count": 1, "last_played": 3}}}
    assert state["history"] == {"u1": [{"track_id": "a", "ts": 3}]}
    state = {}
    merger._apply_entry(state, {"op": "record_play", "id": "a", "ts": 3})
    assert state["play_stats"] == {"": {"a": {"count": 1, "last_played": 3}}}


# ── Track-id remaps per user ────────────────────────────────────────────────

def test_a_remap_moves_every_users_data():
    st = _lib_store()
    st.record_play("t0", uid="alice")
    st.record_play("t0", uid="bob")
    st.push_history({"track_id": "t0", "ts": 1}, uid="alice")
    st.push_history({"track_id": "t0", "ts": 1}, uid="bob")
    counts = st.remap_track_ids({"t0": "n0"})
    assert counts["play_stats"] == 2 and counts["history"] == 2
    for u in ("alice", "bob"):
        assert st.get_play_stats("n0", uid=u)["count"] == 1
        assert st.get_play_stats("t0", uid=u)["count"] == 0
        assert st.get_history(5, uid=u)[0]["track_id"] == "n0"


def test_keep_both_applies_per_user_and_per_container():
    """HVSC's two copies: where the target has its own (a rating, one user's
    plays), both keep theirs; another user's plays still move."""
    st = _lib_store()
    st.set_rating("t0", 5)
    st.set_rating("n0", 2)
    st.record_play("t0", uid="alice")
    st.record_play("n0", uid="alice")                              # alice played both copies
    st.record_play("t0", uid="bob")                                # bob only the old one
    st.push_history({"track_id": "t0", "ts": 1}, uid="alice")
    st.push_history({"track_id": "t0", "ts": 1}, uid="bob")
    st.remap_track_ids({"t0": "n0"}, keep_both=True)
    assert st.get_rating("t0") == 5 and st.get_rating("n0") == 2          # nothing overwritten
    assert st.get_play_stats("t0", uid="alice")["count"] == 1
    assert st.get_play_stats("n0", uid="alice")["count"] == 1
    assert st.get_history(5, uid="alice")[0]["track_id"] == "t0"          # stays with her plays
    assert st.get_play_stats("n0", uid="bob")["count"] == 1               # moved
    assert st.get_play_stats("t0", uid="bob")["count"] == 0
    assert st.get_history(5, uid="bob")[0]["track_id"] == "n0"


# ── Backups (.sbz) ──────────────────────────────────────────────────────────

def test_a_v1_backup_goes_to_the_legacy_owner_not_the_importer():
    """Whoever imports, an older backup's shared data goes to this server's
    legacy owner (the oldest enabled admin) — the importer gets nothing."""
    payload = {"version": 2, "data": {
        "tracks": {"t1": {"id": "t1"}},
        "play_stats": {"t1": {"count": 2, "last_played": 9}},
        "history": [{"track_id": "t1", "ts": 9}],
        "config": {OWNER_CONFIG_KEY: "someone-else"}}}
    data = persistence.import_backup_state(payload, accounts={"x": "y"}, legacy_owner=SFC)
    assert data["db_version"] == DB_VERSION
    assert data["play_stats"] == {SFC: {"t1": {"count": 2, "last_played": 9}}}
    assert data["history"] == {SFC: [{"track_id": "t1", "ts": 9}]}
    assert data["config"][OWNER_CONFIG_KEY] == SFC
    data = persistence.import_backup_state(json.loads(json.dumps(payload)) | {
        "data": {"tracks": {}, "play_stats": {"t1": {"count": 1}}}}, accounts={},
        legacy_owner="")
    assert data["play_stats"] == {"": {"t1": {"count": 1}}}       # no account: no-user bucket


def test_a_v2_backup_maps_users_by_name_and_keeps_the_rest_aside():
    """Matched by name → that account; no account of that name → kept aside
    (nobody sees it) until one exists; the no-user bucket stays one."""
    from soniqboom.core.user_data import PENDING_CONFIG_KEY
    payload = {"version": 2, "db_version": 2,
               "users_map": {"s-alice": "Alice", "s-bob": "bob", "s-gone": "carol",
                             "s-adm": "root"},
               "data": {
                   "db_version": 2, "tracks": {},
                   "play_stats": {"s-alice": {"a": {"count": 1, "last_played": 1}},
                                  "s-bob": {"a": {"count": 2, "last_played": 2}},
                                  "s-gone": {"a": {"count": 4, "last_played": 4}},
                                  "s-orphan": {"a": {"count": 8, "last_played": 8}},
                                  "": {"b": {"count": 1, "last_played": 7}}},
                   "history": {"s-gone": [{"track_id": "a", "ts": 4}],
                               "": [{"track_id": "b", "ts": 7}]},
                   "config": {OWNER_CONFIG_KEY: "s-gone"}}}
    accounts = {"alice": "t-alice", "bob": "s-alice"}        # (an id that is a source id too)
    data = persistence.import_backup_state(payload, accounts=accounts, legacy_owner="t-adm")
    assert data["play_stats"] == {
        "t-alice": {"a": {"count": 1, "last_played": 1}},
        "s-alice": {"a": {"count": 2, "last_played": 2}},   # bob's, onto the account named bob
        "import:s-gone": {"a": {"count": 4, "last_played": 4}},
        "import:s-orphan": {"a": {"count": 8, "last_played": 8}},
        "": {"b": {"count": 1, "last_played": 7}},
    }
    assert set(data["history"]) == {"import:s-gone", ""}
    assert "t-adm" not in data["play_stats"]                  # nothing for the importer
    assert data["config"][PENDING_CONFIG_KEY] == {"import:s-gone": "carol",
                                                  "import:s-orphan": None}
    assert OWNER_CONFIG_KEY not in data["config"]             # (its user has no account here)


def test_kept_aside_data_goes_to_the_account_of_that_name_when_it_exists(fresh_store):
    from soniqboom.core.store import get_store
    from soniqboom.core.user_data import PENDING_CONFIG_KEY, claim_pending
    payload = {"version": 2, "db_version": 2, "users_map": {SFC: "sfc", BOB: "bob"},
               "data": {"db_version": 2, "tracks": {},
                        "play_stats": {BOB: {"b": {"count": 9, "last_played": 2}}},
                        "history": {BOB: [{"track_id": "b", "ts": 2, "title": "bob-private"}]},
                        "config": {OWNER_CONFIG_KEY: SFC}}}
    # The usual restore order: the admin exists (setadm), bob not yet.
    data = persistence.import_backup_state(payload, accounts={"sfc": "t-sfc"},
                                           legacy_owner="t-sfc")
    assert data["history"] == {"import:" + BOB: [{"track_id": "b", "ts": 2,
                                                  "title": "bob-private"}]}
    persistence.populate_store(data)
    st = get_store()
    aof: list = []
    st._aof_append = lambda op, **kw: aof.append((op, kw))
    users = [types.SimpleNamespace(id="t-sfc", username="sfc", role="admin",
                                   created_at=1.0, enabled=True)]
    us = types.SimpleNamespace(list_users=lambda: users)
    assert not claim_pending(st, us)                          # no bob yet: nothing moves
    assert st.get_history(5, uid="t-sfc") == []
    users.append(types.SimpleNamespace(id="t-bob", username="Bob", role="readonly",
                                       created_at=2.0, enabled=True))
    assert claim_pending(st, us)
    assert st.get_history(5, uid="t-bob")[0]["title"] == "bob-private"
    assert st.get_play_stats("b", uid="t-bob")["count"] == 9
    assert st.get_config(PENDING_CONFIG_KEY) is None
    assert aof == [("move_user_data", {"src": "import:" + BOB, "dst": "t-bob"}),
                   ("delete_config", {"key": PENDING_CONFIG_KEY})]


def test_a_newer_backup_is_refused():
    with pytest.raises(DatabaseVersionError):
        persistence.import_backup_state({"version": 2, "db_version": DB_VERSION + 1,
                                         "data": {}}, accounts={}, legacy_owner="a")
    with pytest.raises(DatabaseVersionError):
        persistence.import_backup_state({"version": 2, "data": {"db_version": DB_VERSION + 1,
                                                                "tracks": {}}},
                                        accounts={}, legacy_owner="a")


def _cli(data_dir: Path, *argv) -> subprocess.CompletedProcess:
    env = {**os.environ, "SONIQBOOM_DATA_DIR": str(data_dir)}
    return subprocess.run([sys.executable, "-c",
                           "import asyncio, sys; from soniqboom.cli import export_import as e; "
                           "asyncio.run(getattr(e, sys.argv[1])(sys.argv[2]))", *argv],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_the_command_line_export_and_import_carry_users_by_name(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    _v1(src)
    _std_users(src)
    out = tmp_path / "b.sbz"
    before = {p.name: p.read_bytes() for p in src.iterdir() if p.is_file()}
    r = _cli(src, "export_db", str(out))
    assert r.returncode == 0, r.stderr
    # Read only: the V1 folder is left exactly as it was (converted in memory).
    assert {p.name: p.read_bytes() for p in src.iterdir() if p.is_file()} == before
    assert not (src / "backups").exists() and not (src / "library.lock").exists()
    with gzip.open(out, "rt") as f:
        payload = json.load(f)
    assert payload["db_version"] == DB_VERSION
    assert payload["users_map"] == {READER: "reader", LATE_ADMIN: "admin2", SFC: "sfc"}
    assert payload["data"]["play_stats"].keys() == {SFC}
    assert payload["data"]["play_stats"][SFC]["t2"]["count"] == 1      # (the AOF's play)
    # Another server: sfc exists there under another id; its oldest admin imports.
    _users(dst, ("d-sfc", "SFC", "admin", 500.0), ("d-old", "boss", "admin", 1.0))
    r = _cli(dst, "import_db", str(out))
    assert r.returncode == 0, r.stderr
    snap = json.loads((dst / "library.json").read_text())
    assert snap["db_version"] == DB_VERSION
    assert set(snap["play_stats"]) == {"d-sfc"}
    assert snap["config"][OWNER_CONFIG_KEY] == "d-sfc"
    # The (not yet migrated) shared station list travelled as sfc's.
    assert payload["favorites"] == {SFC: [{"id": "custom:x"}]}
    doc = json.loads((dst / "radio" / "favorites" / "d-sfc.json").read_text())
    assert doc == {"uid": "d-sfc", "favorites": [{"id": "custom:x"}]}


def test_the_command_line_import_of_a_v1_backup_goes_to_the_oldest_admin(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _users(data, ("x-admin", "boss", "admin", 1.0))
    sbz = tmp_path / "old.sbz"
    with gzip.open(sbz, "wt") as f:
        json.dump({"version": 2, "data": {"tracks": {"t1": {"id": "t1", "path": "/m/t1"}},
                                          "play_stats": {"t1": {"count": 1, "last_played": 1}},
                                          "history": []}}, f)
    r = _cli(data, "import_db", str(sbz))
    assert r.returncode == 0, r.stderr
    snap = json.loads((data / "library.json").read_text())
    assert snap["play_stats"] == {"x-admin": {"t1": {"count": 1, "last_played": 1}}}
    # A newer backup is refused, the library left as it is.
    with gzip.open(sbz, "wt") as f:
        json.dump({"version": 2, "db_version": DB_VERSION + 1,
                   "data": {"db_version": DB_VERSION + 1, "tracks": {"t9": {"id": "t9"}}}}, f)
    raw = (data / "library.json").read_bytes()
    r = _cli(data, "import_db", str(sbz))
    assert r.returncode == 1 and "newer SoniqBoom" in r.stderr
    assert (data / "library.json").read_bytes() == raw


# ── A deleted account ───────────────────────────────────────────────────────

def test_deleting_a_user_drops_their_plays_and_history(monkeypatch, tmp_path):
    from soniqboom.api import subsonic
    from soniqboom.core import subsonic_state
    st = _lib_store()
    st.record_play("t0", uid="alice")
    st.push_history({"track_id": "t0", "ts": 1}, uid="alice")
    st.record_play("t0", uid="bob")
    aof: list = []
    st._aof_append = lambda op, **kw: aof.append((op, kw))
    monkeypatch.setattr(subsonic, "_library_store", lambda: st)
    subsonic_state.reset_state(tmp_path / "subsonic_state.json")
    try:
        asyncio.run(subsonic.forget_user("alice"))
    finally:
        subsonic_state.reset_state()
    assert "alice" not in st._play_stats and "alice" not in st._history
    assert st.get_play_stats("t0", uid="bob")["count"] == 1
    assert aof == [("delete_user_data", {"uid": "alice"})]
    assert st.delete_user_data("alice") == {"play_stats": 0, "history": 0}   # (no record again)
    assert len(aof) == 1


def test_deleting_the_legacy_owner_hands_the_role_to_the_next_admin(monkeypatch, tmp_path):
    from soniqboom.api import subsonic
    from soniqboom.api import users as users_api
    from soniqboom.core import store as store_mod
    from soniqboom.core import subsonic_state
    st = _lib_store()
    st.set_config(OWNER_CONFIG_KEY, "alice")
    st.record_play("t0", uid="alice")
    monkeypatch.setattr(subsonic, "_library_store", lambda: st)
    monkeypatch.setattr(store_mod, "get_store", lambda: st)
    nxt = types.SimpleNamespace(id="bob", role="admin", created_at=9.0, enabled=True)
    monkeypatch.setattr(users_api, "get_user_store",
                        lambda: types.SimpleNamespace(list_users=lambda: [nxt]))
    subsonic_state.reset_state(tmp_path / "subsonic_state.json")
    try:
        asyncio.run(subsonic.forget_user("alice"))
    finally:
        subsonic_state.reset_state()
    assert st.user_data_owner() == "bob" and "alice" not in st._play_stats


def test_an_empty_converted_snapshot_keeps_the_known_good_copy(tmp_path, fresh_store):
    """``library.json.prev`` is refreshed from a populated snapshot only — the
    conversion of an empty V1 snapshot must not replace a populated .prev."""
    (tmp_path / "library.json").write_text(json.dumps({"tracks": {}, "config": {}}))
    prev = json.dumps({"tracks": {"t1": {"id": "t1", "path": "/m/t1"}}})
    (tmp_path / "library.json.prev").write_text(prev)
    persistence.init_persistence(tmp_path)
    assert json.loads((tmp_path / "library.json").read_text())["db_version"] == DB_VERSION
    assert (tmp_path / "library.json.prev").read_text() == prev


def test_a_backup_a_crash_cut_short_is_removed_by_the_next(tmp_path, fresh_store):
    _v1(tmp_path)
    stale = tmp_path / "backups" / ".db-v1-20200101-000000.incomplete"
    stale.mkdir(parents=True)
    (stale / "library.json").write_text("{")
    persistence.init_persistence(tmp_path)
    assert not stale.exists() and len(_backups(tmp_path)) == 1


def test_a_recorded_owner_with_no_account_any_more_is_not_used(tmp_path, fresh_store):
    """A V1 snapshot naming a ``user_data_owner`` (an earlier, interrupted
    conversion, a restored file) whose account is gone: the oldest admin."""
    _v1(tmp_path)
    _std_users(tmp_path)
    state = json.loads((tmp_path / "library.json").read_text())
    state["config"][OWNER_CONFIG_KEY] = "uid-deleted"
    (tmp_path / "library.json").write_text(json.dumps(state))
    persistence.init_persistence(tmp_path)
    snap = json.loads((tmp_path / "library.json").read_text())
    assert set(snap["play_stats"]) == {SFC}
    assert snap["config"][OWNER_CONFIG_KEY] == SFC


def test_the_health_endpoint_reports_the_database_version():
    from starlette.requests import Request
    from soniqboom import main
    req = Request({"type": "http", "method": "GET", "path": "/api/health", "headers": [],
                   "client": ("127.0.0.1", 5000), "query_string": b""})
    body = asyncio.run(main.health(req))
    assert body["db_version"] == DB_VERSION == 2
    req = Request({"type": "http", "method": "GET", "path": "/api/health", "headers": [],
                   "client": ("192.0.2.7", 5000), "query_string": b""})
    assert asyncio.run(main.health(req))["db_version"] is None    # (as ``version``)


# ── Review findings (wave 1 QA probes, folded in) ───────────────────────────

def test_a_downgrade_and_upgrade_again_keeps_every_users_data(tmp_path, fresh_store):
    """An older version ran on a V2 library.json and wrote it back without
    ``db_version``: its per-user maps (and its own flat plays) — the re-upgrade
    keeps each user's buckets and gives only the flat entries to the owner."""
    _users(tmp_path, (SFC, "sfc", "admin", 1.0), ("uid-bob", "bob", "readonly", 2.0))
    state = {
        "tracks": {t: {"id": t, "path": f"/m/{t}.mp3", "title": t} for t in ("t1", "t2", "t9")},
        "play_stats": {SFC: {"t1": {"count": 5, "last_played": 10}},
                       "uid-bob": {"t2": {"count": 7, "last_played": 20}},
                       "t1": {"count": 2, "last_played": 40},       # the old version's own plays
                       "t9": {"count": 1, "last_played": 30}},
        "history": {SFC: [{"track_id": "t1", "ts": 10}], "uid-bob": [{"track_id": "t2", "ts": 20}]},
        "config": {OWNER_CONFIG_KEY: SFC},
    }
    (tmp_path / "library.json").write_text(json.dumps(state))
    persistence.init_persistence(tmp_path)
    snap = json.loads((tmp_path / "library.json").read_text())
    assert snap["play_stats"] == {SFC: {"t1": {"count": 7, "last_played": 40},
                                        "t9": {"count": 1, "last_played": 30}},
                                  "uid-bob": {"t2": {"count": 7, "last_played": 20}}}
    assert snap["history"] == state["history"]


def test_web_reads_and_plays_follow_the_signed_in_user(monkeypatch):
    """Every play / history endpoint answers for the signed-in user only
    (readonly users record and read their own); nobody signed in while
    accounts exist → 401, before any account → the no-user bucket."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from soniqboom.api import smart, tracks
    from soniqboom.api import users as users_api
    from soniqboom.core import data as data_mod
    from soniqboom.core import store as store_mod
    from soniqboom.core import users as core_users
    st = _lib_store(3)
    st.record_play("t0", uid="uid-ann")
    st.push_history({"track_id": "t0", "title": "ann-secret", "ts": 1}, uid="uid-ann")
    for mod in (store_mod, data_mod, smart):
        monkeypatch.setattr(mod, "get_store", lambda: st)
    who = {"tok-ann": types.SimpleNamespace(id="uid-ann", role="admin", enabled=True),
           "tok-ro": types.SimpleNamespace(id="uid-ro", role="readonly", enabled=True)}
    accounts = {"any": True}
    fake = types.SimpleNamespace(lookup_session=lambda tok: who.get(tok),
                                 has_any=lambda: accounts["any"])
    monkeypatch.setattr(users_api, "get_user_store", lambda: fake)
    monkeypatch.setattr(core_users, "get_user_store", lambda: fake)
    app = FastAPI()
    app.include_router(tracks.router)
    app.include_router(smart.router)
    c = TestClient(app)
    ro = {"sb_session": "tok-ro"}
    assert c.post("/tracks/t1/played", cookies=ro).status_code == 200
    assert [h["track_id"] for h in c.get("/smart/history", cookies=ro).json()] == ["t1"]
    assert [h["title"] for h in c.get("/smart/history",
                                       cookies={"sb_session": "tok-ann"}).json()] == ["ann-secret"]
    assert set(c.get("/tracks/meta/playstats", cookies=ro).json()) == {"t1"}
    assert c.post("/tracks/meta/playstats/batch", json={"ids": ["t0", "t1"]},
                  cookies=ro).json().keys() == {"t1"}
    assert c.get("/tracks/t0/stats", cookies=ro).json()["count"] == 0       # ann's play
    assert [r["id"] for r in c.get("/smart/most-played", cookies=ro).json()] == ["t1"]
    assert "t1" not in [r["id"] for r in c.get("/smart/unplayed", cookies=ro).json()]
    assert "t0" in [r["id"] for r in c.get("/smart/unplayed", cookies=ro).json()]
    for method, url in (("get", "/smart/history"), ("get", "/smart/most-played"),
                        ("get", "/smart/unplayed"), ("get", "/tracks/meta/playstats"),
                        ("get", "/tracks/t0/stats"), ("post", "/tracks/t2/played"),
                        ("get", "/smart/radio?seed=t0")):
        assert getattr(c, method)(url).status_code == 401, url
    assert st.get_play_stats("t2", uid="")["count"] == 0       # nothing filed under nobody
    accounts["any"] = False                                     # a fresh install
    assert c.post("/tracks/t2/played").status_code == 200
    assert [h["track_id"] for h in c.get("/smart/history").json()] == ["t2"]
    assert st.get_play_stats("t2", uid="")["count"] == 1


def test_a_plays_push_goes_to_that_users_sockets_only(monkeypatch):
    from soniqboom.api import library

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, data):
            self.sent.append(data)
    a1, a2, b = _WS(), _WS(), _WS()
    monkeypatch.setattr(library, "_ws_user_id", {a1: "ann", a2: "ann", b: "bob"})
    monkeypatch.setattr(library, "_verify_ws_session", lambda ws: True)
    assert asyncio.run(library.send_to_user("ann", {"event": "plays_changed"})) == 2
    assert a1.sent == a2.sent == [{"event": "plays_changed"}] and b.sent == []
    assert asyncio.run(library.send_to_user("", {"event": "x"})) == 0


def test_a_subsonic_play_records_under_its_user_and_tells_only_them(monkeypatch):
    from soniqboom.api import library, smart, subsonic
    st = _lib_store(2)
    monkeypatch.setattr(smart, "get_store", lambda: st)        # (the history's store)
    pushed = []

    monkeypatch.setattr(library, "push_to_user", lambda uid, data: pushed.append((uid, data)))
    user = types.SimpleNamespace(id="uid-ro", username="ro", role="readonly")
    asyncio.run(subsonic._record_subsonic_play(st, user, st.get_track("t1")))
    assert st.get_play_stats("t1", uid="uid-ro")["count"] == 1
    assert st.get_history(5, uid="uid-ro")[0]["track_id"] == "t1"
    assert pushed == [("uid-ro", {"event": "plays_changed"})]


def test_a_play_in_flight_for_a_deleted_account_is_not_recorded():
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    st.record_play("t0", uid="uid-bob")
    st.delete_user_data("uid-bob")
    assert st.record_play("t0", uid="uid-bob") == {"count": 0}  # the in-flight scrobble lands
    st.push_history({"track_id": "t0", "ts": 5, "title": "bob-late"}, uid="uid-bob")
    assert "uid-bob" not in st._play_stats and "uid-bob" not in st._history
    st.record_play("t0", uid="uid-ann")                          # others unaffected
    assert st.get_play_stats("t0", uid="uid-ann")["count"] == 1


def test_a_vanished_legacy_owner_hands_its_data_to_the_new_one():
    """users.json restored / made anew (ids changed): the legacy data under
    the former owner's id goes to the new legacy owner; another vanished id's
    data stays where it is."""
    from soniqboom.core.user_data import claim_unowned
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    st.set_config(OWNER_CONFIG_KEY, "old-sfc")
    st.record_play("t0", uid="old-sfc")
    st.push_history({"track_id": "t0", "ts": 1}, uid="old-sfc")
    st.record_play("t1", uid="old-bob")
    new = types.SimpleNamespace(id="new-sfc", role="admin", created_at=1.0, enabled=True)
    assert claim_unowned(st, types.SimpleNamespace(list_users=lambda: [new]))
    assert st.user_data_owner() == "new-sfc"
    assert st.get_play_stats("t0", uid="new-sfc")["count"] == 1
    assert len(st.get_history(10, uid="new-sfc")) == 1
    assert "old-sfc" not in st._play_stats
    assert st.get_play_stats("t1", uid="old-bob")["count"] == 1   # not the legacy owner's
    assert not claim_unowned(st, types.SimpleNamespace(list_users=lambda: [new]))


def test_failed_starts_reuse_one_backup(tmp_path, fresh_store, monkeypatch):
    _users(tmp_path, (SFC, "sfc", "admin", 1.0))
    (tmp_path / "library.json").write_text(json.dumps(
        {"tracks": {"t1": {"id": "t1", "path": "/m/t1"}}, "play_stats": {"t1": {"count": 1}},
         "history": []}))
    monkeypatch.setattr(persistence, "_write_library_text", lambda *a, **k: False)
    for _ in range(4):
        with pytest.raises(DatabaseVersionError):
            persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    (bk,) = _backups(tmp_path)
    manifest = json.loads((bk / "manifest.json").read_text())
    import hashlib
    raw = (tmp_path / "library.json").read_bytes()
    assert manifest["files"] == {"library.json": {"size": len(raw),
                                                  "sha256": hashlib.sha256(raw).hexdigest()}}
    # A changed file is a different database: a new backup.
    (tmp_path / "library.json").write_text(json.dumps(
        {"tracks": {"t1": {"id": "t1", "path": "/m/t1"}}, "play_stats": {"t1": {"count": 2}},
         "history": []}))
    persistence.init_persistence(tmp_path)
    assert len(_backups(tmp_path)) == 2


def test_the_backup_links_the_snapshot_and_checks_every_copy(tmp_path, fresh_store,
                                                              monkeypatch):
    _v1(tmp_path)
    _std_users(tmp_path)
    inode = (tmp_path / "library.json").stat().st_ino
    persistence.init_persistence(tmp_path)
    (bk,) = _backups(tmp_path)
    assert (bk / "library.json").stat().st_ino == inode        # a link, not a copy
    assert (tmp_path / "library.json").stat().st_ino != inode  # (the V2 file is a new one)
    assert json.loads((bk / "library.json").read_text()).get("db_version") is None


def test_a_corrupt_copy_or_too_little_space_converts_nothing(tmp_path, fresh_store,
                                                            monkeypatch):
    _v1(tmp_path)
    before = _originals(tmp_path)
    real = persistence.shutil.copyfile

    def corrupt(src, dst, *a, **k):
        real(src, dst)
        with open(dst, "ab") as f:
            f.write(b"x")
    monkeypatch.setattr(persistence.shutil, "copyfile", corrupt)
    with pytest.raises(DatabaseVersionError, match="differs from the original"):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    monkeypatch.setattr(persistence.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(total=1, used=1, free=1000))
    with pytest.raises(DatabaseVersionError, match="MB needed"):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    assert _originals(tmp_path) == before and _backups(tmp_path) == []


def test_an_unreadable_snapshot_is_not_replaced_by_an_empty_one(tmp_path, fresh_store,
                                                               monkeypatch):
    """library.json can't be read at that moment (EIO on a share): the
    conversion refuses to write an empty library over it — the AOF's play is
    kept and converted at the next start."""
    _users(tmp_path, (SFC, "sfc", "admin", 1.0))
    (tmp_path / "library.json").write_text(json.dumps(
        {"tracks": {t: {"id": t, "path": f"/m/{t}"} for t in ("t1", "t2")},
         "play_stats": {"t1": {"count": 3, "last_played": 1}}, "history": []}))
    (tmp_path / "library.aof").write_text(json.dumps(
        {"op": "record_play", "ts": 5, "id": "t2"}) + "\n")
    before = _originals_any(tmp_path)
    real = persistence._try_load_json
    calls = {"n": 0}

    def flaky(path):
        if path.name == "library.json" and calls["n"] == 0:
            calls["n"] += 1
            return None
        return real(path)
    monkeypatch.setattr(persistence, "_try_load_json", flaky)
    with pytest.raises(DatabaseVersionError, match="could not be read completely"):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    assert _originals_any(tmp_path) == before
    persistence.init_persistence(tmp_path)
    from soniqboom.core.store import get_store
    st = get_store()
    assert st.track_count() == 2
    assert st.get_play_stats("t2", uid=SFC)["count"] == 1


def _originals_any(d: Path) -> dict:
    return {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()
            and p.name.startswith("library.") and not p.name.endswith(".lock")}


def test_late_exec_records_are_only_dropped_after_the_v2_write(tmp_path, fresh_store,
                                                               monkeypatch):
    """A V1 snapshot whose mark holds part of the AOF, plus a set-aside
    late-exec file: counted once; nothing but the backup is written before
    the V2 snapshot (a crash there leaves the late file and the AOF as they
    were); a crash after the write, before the file's removal, counts
    nothing twice."""
    _users(tmp_path, (SFC, "sfc", "admin", 1.0))
    r1 = (json.dumps({"op": "record_play", "ts": 1, "id": "t1"}) + "\n").encode()
    r2 = (json.dumps({"op": "record_play", "ts": 2, "id": "t1"}) + "\n").encode()
    r3 = (json.dumps({"op": "record_play", "ts": 3, "id": "t1"}) + "\n").encode()
    (tmp_path / "library.json").write_text(json.dumps(
        {"tracks": {"t1": {"id": "t1", "path": "/m/t1"}},
         "play_stats": {"t1": {"count": 1, "last_played": 1}}, "history": [],
         persistence.AOF_MARK: persistence.aof_mark(r1)}))
    (tmp_path / "library.aof").write_bytes(r1 + r2)
    late = tmp_path / "library.aof.late-exec-1"
    late.write_bytes(r3)

    class _Died(BaseException):
        pass

    def die(*a, **k):
        raise _Died
    monkeypatch.setattr(persistence, "write_library_file", die)
    with pytest.raises(_Died):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    assert (tmp_path / "library.aof").read_bytes() == r1 + r2 and late.read_bytes() == r3
    real_unlink = Path.unlink

    def no_unlink(self, *a, **k):
        if self.name.startswith("library.aof.late-exec-"):
            raise _Died
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", no_unlink)
    with pytest.raises(_Died):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    assert late.exists()                                       # (the write happened)
    persistence.init_persistence(tmp_path)
    from soniqboom.core.store import get_store
    assert get_store().get_play_stats("t1", uid=SFC)["count"] == 3
    assert not late.exists()
    (bk,) = _backups(tmp_path)
    assert (bk / "library.aof.late-exec-1").read_bytes() == r3
    assert (bk / "library.aof").read_bytes() == r1 + r2
    assert merger._do_merge(tmp_path) == 0
    snap = json.loads((tmp_path / "library.json").read_text())
    assert snap["play_stats"][SFC]["t1"]["count"] == 3


def test_the_conversion_drops_exactly_the_aof_bytes_it_replayed(tmp_path, fresh_store,
                                                                monkeypatch):
    """A record appended to the AOF after the replay read it (by nothing at a
    start today — the mark must still cover only what was replayed) stays."""
    _v1(tmp_path)
    _std_users(tmp_path)
    real = persistence.write_library_file
    extra = json.dumps({"op": "record_play", "ts": 9, "id": "t3", "uid": SFC}) + "\n"

    def write_then_append(*a, **k):
        with open(tmp_path / "library.aof", "a") as f:
            f.write(extra)
        return real(*a, **k)
    monkeypatch.setattr(persistence, "write_library_file", write_then_append)
    persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    assert (tmp_path / "library.aof").read_text() == extra


def test_the_upgrade_check_skips_v2_records_unparsed(tmp_path, monkeypatch):
    lines = [json.dumps({"op": "record_play", "ts": 1.0 + i, "id": f"t{i}", "uid": SFC})
             for i in range(2000)]
    (tmp_path / "library.aof").write_text("\n".join(lines) + "\n")
    parsed = []
    real = persistence.loads_json
    monkeypatch.setattr(persistence, "loads_json", lambda raw: parsed.append(1) or real(raw))
    assert persistence._needs_upgrade(tmp_path, {"db_version": 2}) is None
    assert parsed == []
    with open(tmp_path / "library.aof", "a") as f:                # one V1 record at the end
        f.write(json.dumps({"op": "record_play", "ts": 5, "id": "x"}) + "\n")
    assert persistence._needs_upgrade(tmp_path, {"db_version": 2}) == 1
    assert len(parsed) == 1


def test_the_web_export_is_encoded_on_the_loop(monkeypatch):
    from soniqboom.api import admin
    st = _lib_store(2)
    st.record_play("t0", uid="ann")
    monkeypatch.setattr(admin, "get_store", lambda: st)
    from soniqboom.core import users as core_users
    monkeypatch.setattr(core_users, "get_user_store",
                        lambda: types.SimpleNamespace(list_users=lambda: [
                            types.SimpleNamespace(id="ann", username="Ann")]))

    async def run():
        resp = await admin.admin_export(_tok="user:ann")
        body = b"".join([c async for c in resp.body_iterator])
        return json.loads(gzip.decompress(body))
    payload = asyncio.run(run())
    assert payload["db_version"] == DB_VERSION and payload["users_map"] == {"ann": "Ann"}
    assert payload["data"]["play_stats"] == {"ann": st._play_stats["ann"]}


# ── Review round 2 ──────────────────────────────────────────────────────────

def test_an_older_mergers_flat_plays_in_a_v2_file_are_converted(tmp_path, fresh_store):
    """A downgrade: the older version's merger rewrites the V2 library.json
    keeping ``db_version`` 2 and adds flat entries — the next start backs it
    up and folds them into the legacy owner."""
    _users(tmp_path, (SFC, "sfc", "admin", 1.0))
    state = {"db_version": 2, "tracks": {"t1": {"id": "t1", "path": "/m/t1"},
                                         "t2": {"id": "t2", "path": "/m/t2"}},
             "play_stats": {SFC: {"t1": {"count": 1, "last_played": 1}},
                            "t2": {"count": 1, "last_played": 5}},
             "history": {SFC: [{"track_id": "t1", "ts": 1}]},
             "config": {OWNER_CONFIG_KEY: SFC}}
    (tmp_path / "library.json").write_text(json.dumps(state))
    persistence.init_persistence(tmp_path)
    assert len(_backups(tmp_path)) == 1
    snap = json.loads((tmp_path / "library.json").read_text())
    assert snap["play_stats"] == {SFC: {"t1": {"count": 1, "last_played": 1},
                                        "t2": {"count": 1, "last_played": 5}}}
    persistence.init_persistence(tmp_path)
    assert len(_backups(tmp_path)) == 1                           # (converted: once)


def test_an_owner_being_deleted_keeps_its_data_out_of_other_hands():
    """A claim running while the legacy owner's account is deleted (its id
    already gone from the accounts, its data not yet dropped) moves nothing."""
    from soniqboom.core.user_data import claim_unowned
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    st.set_config(OWNER_CONFIG_KEY, "alice")
    st.push_history({"track_id": "t0", "ts": 1, "title": "alice-private"}, uid="alice")
    st.user_deleting("alice")
    bob = types.SimpleNamespace(id="bob", role="admin", created_at=2.0, enabled=True)
    claim_unowned(st, types.SimpleNamespace(list_users=lambda: [bob]))
    assert st.get_history(5, uid="bob") == []
    st.delete_user_data("alice")
    assert "alice" not in st._history
    # A refused delete gives the account its writes back.
    st.user_deleting("carol")
    st.user_deleting("carol", False)
    st.record_play("t0", uid="carol")
    assert st.get_play_stats("t0", uid="carol")["count"] == 1


def test_the_web_delete_drops_the_data_with_the_account(monkeypatch):
    from soniqboom.api import users as users_api
    from soniqboom.core import store as store_mod
    st = TrackStore()
    st._aof_append = lambda *a, **k: None
    st.record_play("t0", uid="victim")
    monkeypatch.setattr(store_mod, "get_store", lambda: st)
    seen = []

    class _Users:
        def delete(self, uid):
            seen.append(st.user_deleted(uid))           # (marked before the account goes)
    monkeypatch.setattr(users_api, "get_user_store", lambda: _Users())

    async def _no_ws(uid):
        return 0
    monkeypatch.setattr(users_api, "close_open_ws_for", _no_ws)
    admin = types.SimpleNamespace(id="adm", role="admin")
    from soniqboom.api import subsonic

    async def _forget(uid):
        return None
    monkeypatch.setattr(subsonic, "forget_user", _forget)
    asyncio.run(users_api.delete_user("victim", admin=admin))
    assert seen == [True] and "victim" not in st._play_stats

    class _Refusing:
        def delete(self, uid):
            raise ValueError("last admin")
    monkeypatch.setattr(users_api, "get_user_store", lambda: _Refusing())
    st.record_play("t0", uid="keeper")
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(users_api.delete_user("keeper", admin=admin))
    assert not st.user_deleted("keeper") and st.get_play_stats("t0", uid="keeper")["count"] == 1


def test_a_plays_push_never_holds_up_the_caller_and_drops_a_dead_socket(monkeypatch):
    from soniqboom.api import library

    class _Stuck:
        async def send_json(self, data):
            await asyncio.sleep(30)

        async def close(self, code=None):
            pass
    stuck = _Stuck()
    monkeypatch.setattr(library, "_ws_user_id", {stuck: "ann"})
    monkeypatch.setattr(library, "_ws_clients", {stuck})
    monkeypatch.setattr(library, "_verify_ws_session", lambda ws: True)

    async def run():
        import time as _t
        t0 = _t.perf_counter()
        library.push_to_user("ann", {"event": "plays_changed"})
        quick = _t.perf_counter() - t0
        await asyncio.gather(*library._USER_PUSHES)
        return quick
    assert asyncio.run(run()) < 0.1
    assert stuck not in library._ws_user_id and stuck not in library._ws_clients


def test_a_late_file_the_aof_already_ends_with_is_removed_by_the_conversion(
        tmp_path, fresh_store):
    """A V1 start that crashed between adding a late-exec file to the AOF and
    removing it: the conversion replays the records once (with the AOF) and
    removes the file — a later start adds nothing again."""
    _users(tmp_path, (SFC, "sfc", "admin", 1.0))
    r = (json.dumps({"op": "record_play", "ts": 2, "id": "t1"}) + "\n").encode()
    (tmp_path / "library.json").write_text(json.dumps(
        {"tracks": {"t1": {"id": "t1", "path": "/m/t1"}}, "play_stats": {}, "history": []}))
    (tmp_path / "library.aof").write_bytes(r)
    late = tmp_path / "library.aof.late-exec-7"
    late.write_bytes(r)
    persistence.init_persistence(tmp_path)
    assert not late.exists()
    persistence.init_persistence(tmp_path)
    from soniqboom.core.store import get_store
    assert get_store().get_play_stats("t1", uid=SFC)["count"] == 1
    assert len(_backups(tmp_path)) == 1


def test_refused_lines_reach_the_quarantine_only_after_the_v2_write(tmp_path, fresh_store,
                                                                   monkeypatch):
    _v1(tmp_path)
    with open(tmp_path / "library.aof", "a") as f:
        f.write("{not json\n")

    class _Died(BaseException):
        pass

    def die(*a, **k):
        raise _Died
    monkeypatch.setattr(persistence, "write_library_file", die)
    with pytest.raises(_Died):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    assert not (tmp_path / "library.aof.quarantine").exists()   # (nothing written before)
    persistence.init_persistence(tmp_path)
    q = (tmp_path / "library.aof.quarantine").read_text().splitlines()
    assert len(q) == 1 and json.loads(q[0])["raw"] == "{not json"


def test_the_bak_rotation_never_writes_into_the_backups_link(tmp_path, monkeypatch):
    """After a conversion the backup's library.json may be the same file as
    library.json.bak; the rotation's copy fallback replaces the .bak with a
    new file instead of writing into it."""
    (tmp_path / "library.json").write_text('{"v": "new"}')
    (tmp_path / "library.json.bak").write_text('{"v": "old"}')
    keep = tmp_path / "kept-link"
    os.link(tmp_path / "library.json.bak", keep)

    def no_link(*a, **k):
        raise OSError("no hard links here")
    monkeypatch.setattr(persistence.os, "link", no_link)
    persistence._rotate_backup(tmp_path / "library.json", tmp_path / "library.json.bak", "T")
    assert (tmp_path / "library.json.bak").read_text() == '{"v": "new"}'
    assert keep.read_text() == '{"v": "old"}'


def test_nothing_loaded_is_refused_even_when_the_aof_adds_tracks(tmp_path, fresh_store,
                                                                monkeypatch):
    _users(tmp_path, (SFC, "sfc", "admin", 1.0))
    (tmp_path / "library.json").write_text(json.dumps(
        {"tracks": {t: {"id": t, "path": f"/m/{t}"} for t in ("t1", "t2", "t3")},
         "play_stats": {}, "history": []}))
    (tmp_path / "library.aof").write_text(
        json.dumps({"op": "upsert_track", "ts": 1, "id": "t9",
                    "data": {"id": "t9", "path": "/m/t9"}}) + "\n"
        + json.dumps({"op": "record_play", "ts": 5, "id": "t9"}) + "\n")
    real = persistence._try_load_json
    calls = {"n": 0}

    def flaky(path):
        if path.name == "library.json" and calls["n"] == 0:
            calls["n"] += 1
            return None
        return real(path)
    monkeypatch.setattr(persistence, "_try_load_json", flaky)
    with pytest.raises(DatabaseVersionError, match="could not be read completely"):
        persistence.init_persistence(tmp_path)
    monkeypatch.undo()
    persistence.init_persistence(tmp_path)
    from soniqboom.core.store import get_store
    assert get_store().track_count() == 4


def _v1_with_playlists(d: Path) -> None:
    _v1(d)
    state = json.loads((d / "library.json").read_text())
    state["playlists"] = {
        "p-shared": {"id": "p-shared", "name": "Old shared", "track_ids": ["t1"],
                     "owner_user_id": None},
        "p-sfc": {"id": "p-sfc", "name": "Mine", "track_ids": ["t1"], "owner_user_id": SFC},
        "p-gone": {"id": "p-gone", "name": "Deleted account's", "track_ids": ["t2"],
                   "owner_user_id": "uid-deleted-long-ago"},
        "p-aside": {"id": "p-aside", "name": "Kept aside", "track_ids": ["t3"],
                    "owner_user_id": "import:uid-elsewhere"},
    }
    (d / "library.json").write_text(json.dumps(state))


def test_the_conversion_deletes_playlists_of_deleted_accounts(tmp_path, fresh_store):
    """Playlists whose account was deleted before V2 (reachable by nobody) are
    removed at the conversion; the backup still holds them."""
    from soniqboom.core.store import get_store
    _v1_with_playlists(tmp_path)
    _std_users(tmp_path)
    persistence.init_persistence(tmp_path)
    pls = get_store()._playlists
    assert set(pls) == {"p-shared", "p-sfc", "p-aside"}
    assert pls["p-shared"]["owner_user_id"] == SFC           # the shared ones go to the owner
    backup = _backups(tmp_path)[0]
    assert "p-gone" in json.loads((backup / "library.json").read_text())["playlists"]


def test_no_playlist_is_deleted_when_the_accounts_cannot_be_read(tmp_path, fresh_store):
    from soniqboom.core.store import get_store
    _v1_with_playlists(tmp_path)
    (tmp_path / "users.json").write_text("{not json")
    persistence.init_persistence(tmp_path)
    assert "p-gone" in get_store()._playlists
