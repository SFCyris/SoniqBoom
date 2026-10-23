# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Restoring a backup and the state a Docker container keeps.

* ``soniqboom import`` (CLI): refuses while a server holds the data directory;
  writes the imported library and removes the replaced library's AOF, so the
  next start loads exactly the backup.
* Backup → Import .sbz (web): saved as ``library.json`` with the old AOF
  records dropped — it used to live in memory only.
* Credentials keyed by ``SONIQBOOM_CREDENTIAL_KEY_FILE`` survive a host-name
  change; values made with the machine key still decrypt.
* ``SONIQBOOM_DEFAULT_SOUNDFONT`` is linked into an empty SoundFonts folder.
* ``year:<N`` filters (it matched every year).
* A YM4 file — raw or LHA-wrapped — is reported undecodable (StSound refuses it).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from soniqboom.core import persistence

ROOT = Path(__file__).resolve().parent.parent


def _lib(d: Path, tracks) -> None:
    state = {"tracks": {t: {"id": t, "path": f"/m/{t}.mp3", "title": t} for t in tracks}}
    (d / "library.json").write_text(json.dumps(state))


def _aof(d: Path, *records) -> None:
    with open(d / "library.aof", "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _boot(d: Path) -> dict:
    state = persistence.load_snapshot(d)
    persistence.replay_aof(state, d)
    return state


def _sbz(path: Path, tracks) -> Path:
    data = {"tracks": {t: {"id": t, "path": f"/m/{t}.mp3", "title": t} for t in tracks}}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"version": 2, "data": data}, f)
    return path


@pytest.fixture()
def fresh_store():
    from soniqboom.core import store as store_mod
    old = store_mod._store
    store_mod._store = None
    yield
    store_mod._store = old
    persistence.release_library()


# ── CLI import ───────────────────────────────────────────────────────────────

def _cli_import(data_dir: Path, sbz: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "SONIQBOOM_DATA_DIR": str(data_dir)}
    return subprocess.run([sys.executable, "-c",
                           "import asyncio, sys; from soniqboom.cli.export_import import import_db; "
                           "asyncio.run(import_db(sys.argv[1]))", str(sbz)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_cli_import_replaces_the_library_and_drops_the_old_aof(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _lib(data, ["old1", "old2"])
    # the replaced library's journal: a new track and a play — replayed on top
    # of the import, they'd bring back what the backup doesn't hold
    _aof(data, {"op": "upsert_track", "ts": 1, "id": "old3",
                "data": {"id": "old3", "path": "/m/old3.mp3", "title": "old3"}},
         {"op": "record_play", "ts": 2, "id": "old1"})
    r = _cli_import(data, _sbz(tmp_path / "b.sbz", ["new1", "new2", "new3"]))
    assert r.returncode == 0, r.stderr
    assert not (data / "library.aof").exists()
    state = _boot(data)
    assert sorted(state["tracks"]) == ["new1", "new2", "new3"]
    assert not state.get("play_stats")


def test_cli_import_refuses_while_a_server_holds_the_data_directory(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _lib(data, ["old1"])
    assert persistence.claim_library(data, wait=0)       # this process plays the server
    try:
        r = _cli_import(data, _sbz(tmp_path / "b.sbz", ["new1"]))
    finally:
        persistence.release_library()
    assert r.returncode == 1
    assert "stop it" in r.stderr
    assert sorted(json.loads((data / "library.json").read_text())["tracks"]) == ["old1"]


# ── Web import ───────────────────────────────────────────────────────────────

def test_web_import_is_saved_and_the_old_aof_is_dropped(tmp_data_dir, fresh_store, monkeypatch):
    from soniqboom import main as main_mod
    from soniqboom.api import admin
    from soniqboom.core.aof import AOFWriter
    from soniqboom.core.store import get_store

    data = tmp_data_dir
    _lib(data, ["old1", "old2"])
    _aof(data, {"op": "record_play", "ts": 2, "id": "old1"})
    assert persistence.claim_library(data, wait=0)
    persistence.init_persistence(data)

    async def run() -> bool:
        w = AOFWriter(data / "library.aof")
        get_store()._aof_append = w.append
        await w.start_auto_flush()
        monkeypatch.setattr(main_mod, "_aof_writer", w, raising=False)
        get_store().record_play("old2", uid="")                  # buffered, the old library's
        persistence.populate_store(
            {"tracks": {t: {"id": t, "path": f"/m/{t}.mp3", "title": t} for t in ("n1", "n2")}})
        get_store().rebuild_indexes()
        saved = await admin._save_imported_library() is None
        # the journal keeps working after the save
        get_store().record_play("n1", uid="")
        await w.flush()
        alive = w._flush_task is not None
        w.stop()
        return saved and alive

    assert asyncio.run(run())
    state = _boot(data)                                  # what the next start loads
    assert sorted(state["tracks"]) == ["n1", "n2"]
    plays = {k: v.get("count") for k, v in (state.get("play_stats") or {}).get("", {}).items()}
    assert plays == {"n1": 1}


def test_web_import_reports_not_saved_without_the_data_directory(tmp_data_dir, fresh_store):
    from soniqboom.api import admin
    persistence.release_library()
    assert asyncio.run(admin._save_imported_library()) == "not-owner"


def test_cli_import_of_an_empty_backup_leaves_the_library(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _lib(data, ["a", "b"])
    _aof(data, {"op": "record_play", "ts": 2, "id": "a"})
    r = _cli_import(data, _sbz(tmp_path / "empty.sbz", []))
    assert r.returncode == 1 and "holds no tracks" in r.stderr
    assert (data / "library.aof").exists()
    state = _boot(data)
    assert sorted(state["tracks"]) == ["a", "b"]
    assert state["play_stats"][""]["a"]["count"] == 1        # (a record without uid: no owner)


def test_cli_import_snapshot_marks_the_old_aof(tmp_path, fresh_store, monkeypatch):
    """A stop between writing library.json and removing the AOF: the mark
    makes the next start skip the replaced library's records."""
    data = tmp_path / "data"
    data.mkdir()
    _lib(data, ["old1"])
    _aof(data, {"op": "upsert_track", "ts": 1, "id": "old2",
                "data": {"id": "old2", "path": "/m/old2.mp3", "title": "old2"}})
    persistence.populate_store(
        {"tracks": {"n1": {"id": "n1", "path": "/m/n1.mp3", "title": "n1"}}})
    real_unlink = Path.unlink

    def no_aof_unlink(self, *a, **kw):
        if self.name == "library.aof":
            raise KeyboardInterrupt("stopped here")
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", no_aof_unlink)
    with pytest.raises(KeyboardInterrupt):
        persistence.write_imported_library(data)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert (data / "library.aof").exists()
    assert sorted(_boot(data)["tracks"]) == ["n1"]


def test_web_import_of_an_empty_backup_is_refused(tmp_data_dir, fresh_store):
    from fastapi import HTTPException
    from soniqboom.api import admin
    from soniqboom.core.store import get_store
    persistence.populate_store(
        {"tracks": {"a": {"id": "a", "path": "/m/a.mp3", "title": "a"}}})

    class _Up:
        async def read(self):
            return gzip.compress(json.dumps({"version": 2, "data": {"tracks": {}}}).encode())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(admin.admin_import(file=_Up(), _tok="x"))
    assert ei.value.status_code == 400
    assert get_store().track_count() == 1


# ── Credentials ──────────────────────────────────────────────────────────────

def test_credential_key_file_survives_a_host_name_change(tmp_path, monkeypatch):
    from soniqboom.core import credentials
    monkeypatch.setattr(credentials, "_key_file_cache", {})
    monkeypatch.setattr(credentials.platform, "node", lambda: "container-a")
    legacy = credentials.encrypt("legacy-secret")         # machine key (no key file yet)

    key_file = tmp_path / "credential.key"
    monkeypatch.setenv("SONIQBOOM_CREDENTIAL_KEY_FILE", str(key_file))
    token = credentials.encrypt("share-pass")
    assert key_file.is_file()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert credentials.decrypt(legacy) == "legacy-secret"   # old values still read

    # a recreated container: new host name, same volume
    monkeypatch.setattr(credentials, "_key_file_cache", {})
    monkeypatch.setattr(credentials.platform, "node", lambda: "container-b")
    assert credentials.decrypt(token) == "share-pass"


def test_an_empty_key_file_is_replaced(tmp_path, monkeypatch):
    from soniqboom.core import credentials
    monkeypatch.setattr(credentials, "_key_file_cache", {})
    key_file = tmp_path / "credential.key"
    key_file.write_bytes(b"")                            # a first write cut short
    monkeypatch.setenv("SONIQBOOM_CREDENTIAL_KEY_FILE", str(key_file))
    token = credentials.encrypt("v")
    assert len(key_file.read_bytes().strip()) == 44
    assert list(tmp_path.glob("credential.key.invalid-*"))
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    monkeypatch.setattr(credentials, "_key_file_cache", {})
    monkeypatch.setattr(credentials.platform, "node", lambda: "elsewhere")
    assert credentials.decrypt(token) == "v"
    assert not list(tmp_path.glob("*.tmp"))


def test_without_a_key_file_a_host_name_change_still_loses_the_value(monkeypatch):
    from soniqboom.core import credentials
    monkeypatch.delenv("SONIQBOOM_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.setattr(credentials.platform, "node", lambda: "host-a")
    token = credentials.encrypt("x")
    monkeypatch.setattr(credentials.platform, "node", lambda: "host-b")
    assert credentials.decrypt(token) is None


# ── SoundFont seeding ────────────────────────────────────────────────────────

def test_default_soundfont_is_linked_into_an_empty_folder_only(tmp_path, monkeypatch):
    from soniqboom import config
    gm = tmp_path / "sys" / "FluidR3_GM.sf2"
    gm.parent.mkdir()
    gm.write_bytes(b"RIFF....sfbk")
    sf_dir = tmp_path / "fonts"
    monkeypatch.setattr(config.settings, "soundfonts_dir", str(sf_dir))
    monkeypatch.setenv("SONIQBOOM_DEFAULT_SOUNDFONT", str(gm))

    link = config.seed_default_soundfont()
    assert link == sf_dir / "FluidR3_GM.sf2" and link.is_symlink()
    assert config.get_active_soundfont() == link
    assert config.seed_default_soundfont() is None       # not again

    link.unlink()
    (sf_dir / "Mine.sf3").write_bytes(b"x")              # the user's own font
    assert config.seed_default_soundfont() is None
    assert not (sf_dir / "FluidR3_GM.sf2").exists()

    (sf_dir / "Mine.sf3").unlink()
    gone = sf_dir / "Old.sf2"
    gone.symlink_to(tmp_path / "missing.sf2")            # a link to a font that's gone
    assert config.seed_default_soundfont() == sf_dir / "FluidR3_GM.sf2"
    assert not gone.is_symlink()

    monkeypatch.delenv("SONIQBOOM_DEFAULT_SOUNDFONT")
    (sf_dir / "FluidR3_GM.sf2").unlink()
    assert config.seed_default_soundfont() is None


# ── year:<N ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,lo,hi", [
    ("@year:[-inf (2020]", None, 2019),                 # what api.search emits for year:<2020
    ("@year:[(2020 +inf]", 2021, None),
    ("@year:[2010 2020]", 2010, 2020),
    ("@year:[-inf 2019)]", None, 2018),                  # legacy suffix form
])
def test_year_bounds(q, lo, hi):  # noqa: D103
    from soniqboom.core.data import _parse_tag_query
    kw = _parse_tag_query(q)
    assert kw.get("year_min") == lo
    assert kw.get("year_max") == hi


@pytest.mark.parametrize("q,lo,hi", [
    ("year:<2000", None, 1999), ("year:>2000", 2001, None), ("year:<=2000", None, 2000),
    ("year:>=2000", 2000, None), ("year:1990-2000", 1990, 2000), ("year:2000-", 2000, None),
    ("year:-2000", None, 2000), ("year:1995", 1995, 1995), ('year:"<2000"', None, 1999),
    # spaces around the operator or the dash
    ("year:< 2000", None, 1999), ("year: <2000", None, 1999), ("year: 2000", 2000, 2000),
    ("year: >= 2000", 2000, None), ("year:<= 1999", None, 1999),
    ("year:1990 - 2000", 1990, 2000), ("year:1990- 2000", 1990, 2000),
])
def test_year_operators_end_to_end(q, lo, hi):
    from soniqboom.api.search import _parse_advanced_query
    from soniqboom.core.data import _parse_tag_query
    kw = _parse_tag_query(_parse_advanced_query(q))
    assert (kw.get("year_min"), kw.get("year_max")) == (lo, hi)


# ── YM4 ──────────────────────────────────────────────────────────────────────

YM_LH5 = ROOT / "internal" / "testdata" / "atari" / "ym" / "gear1.ym"    # -lh5- wrapped YM


def test_raw_ym4_is_undecodable_and_ym5_is_not(tmp_path):
    from soniqboom.core.metadata import ym_is_decodable
    ym4 = tmp_path / "a.ym"
    ym4.write_bytes(b"YM4!" + b"\0" * 64)
    ym5 = tmp_path / "b.ym"
    ym5.write_bytes(b"YM5!LeOnArD!" + b"\0" * 64)
    assert ym_is_decodable(ym4) is False
    assert ym_is_decodable(ym5) is True


@pytest.mark.skipif(not YM_LH5.exists(), reason="needs internal/testdata YM files")
def test_wrapped_ym4_is_undecodable_and_badged(monkeypatch):
    """A real -lh5- YM plays; the same wrapper around a YM4 payload doesn't —
    at play (the payload unpacked by ym_is_decodable) and at scan (badge)."""
    import lhafile
    from soniqboom.core import metadata
    assert YM_LH5.read_bytes()[2:7] == b"-lh5-"
    assert metadata._ym_payload_magic(YM_LH5) in metadata._YM_RAW_MAGICS
    assert metadata.ym_is_decodable(YM_LH5) is True
    assert "defect" not in metadata._extract_ym(YM_LH5, "t")

    real_read = lhafile.Lhafile.read
    monkeypatch.setattr(lhafile.Lhafile, "read",
                        lambda self, name: b"YM4!" + real_read(self, name)[4:])
    assert metadata.ym_is_decodable(YM_LH5) is False
    d = metadata._extract_ym(YM_LH5, "t")
    assert d["defect"] == "corrupt" and "YM4" in d["defect_detail"]


@pytest.mark.skipif(not YM_LH5.exists(), reason="needs internal/testdata YM files")
def test_ym4_scanned_before_is_badged_at_play(tmp_path, fresh_store, monkeypatch):
    import shutil
    import lhafile
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core.store import get_store
    f = tmp_path / "old.ym"
    shutil.copy(YM_LH5, f)
    persistence.populate_store({"tracks": {"y": {"id": "y", "path": str(f), "title": "y"}}})
    real_read = lhafile.Lhafile.read
    monkeypatch.setattr(lhafile.Lhafile, "read",
                        lambda self, name: b"YM4!" + real_read(self, name)[4:])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(stream._render_ym(f, track_id="y"))
    assert ei.value.status_code == 415
    t = get_store().get_track("y")
    assert t["defect"] == "corrupt" and "YM4" in t["defect_detail"]


def test_spaced_year_filter_keeps_the_free_text():
    from soniqboom.api.search import _parse_advanced_query
    from soniqboom.core.data import _parse_tag_query
    kw = _parse_tag_query(_parse_advanced_query("year:< 2000 commando"))
    assert (kw.get("year_max"), kw.get("query")) == (1999, "commando")


# ── Snapshot encoding (the web import's pause) ───────────────────────────────

def test_encode_state_round_trips_what_json_did():
    st = {"tracks": {"a": {"id": "a", "title": "Ünïcödé ♪", "genre": ["Chip"], "duration": 12.5,
                           "big": 2 ** 70, "lone": "x\ud800y", "flag": True, "none": None}},
          "hash_lookups": {1: "int key"}}
    out = json.loads(persistence.encode_state(st))     # (fallback path: big int / surrogate)
    assert out["tracks"]["a"]["big"] == 2 ** 70
    assert out["hash_lookups"] == {"1": "int key"}
    plain = {"tracks": {"b": {"id": "b", "title": "Ünïcödé ♪", "duration": float("nan"), "n": 3}}}
    out2 = json.loads(persistence.encode_state(plain))   # orjson path
    assert out2["tracks"]["b"]["title"] == "Ünïcödé ♪" and out2["tracks"]["b"]["n"] == 3
    assert out2["tracks"]["b"]["duration"] is None        # nan → null


def test_snapshots_on_the_loop_use_encode_state(tmp_path, fresh_store, monkeypatch):
    """The shutdown / web-import snapshot is encoded on the event loop — it
    must go through the fast encoder."""
    calls = []
    real = persistence.encode_state
    monkeypatch.setattr(persistence, "encode_state", lambda st: calls.append(1) or real(st))
    persistence.populate_store({"tracks": {"a": {"id": "a", "path": "/m/a.mp3", "title": "a"}}})
    assert persistence.write_snapshot_sync(tmp_path) is True
    assert calls and json.loads((tmp_path / "library.json").read_text())["tracks"]["a"]["id"] == "a"
