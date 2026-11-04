# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Online collections and scene information requested outside the web app:
``soniqboom collections|scene-info list|add``, ``first-run-setup`` (the
installer's questions), a new deployment's first start
(``SONIQBOOM_COLLECTIONS`` / ``SONIQBOOM_SCENE_INFO``) and the server applying
the requests (``core/setup_requests``)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from soniqboom.config import load_local_conf, settings, update_local_conf
from soniqboom.core import setup_requests as sr

ALL_COLL = ["modland", "hvsc", "aminet", "sceneorg"]
ALL_SCENE = ["hvsc", "modland", "songdb", "demozoo"]


@pytest.fixture
def clean(monkeypatch, tmp_path):
    """The suite's conf without a ``setup`` section, put back afterwards; an
    empty data folder (a new install)."""
    before = load_local_conf().get("setup")

    def drop(conf):
        conf.pop("setup", None)
    update_local_conf(drop)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    sr._retry_at.clear()
    yield tmp_path

    def put(conf):
        conf.pop("setup", None)
        if before is not None:
            conf["setup"] = before
    update_local_conf(put)


def test_choices_parse():
    assert sr.parse_choice("yes", "collections") == ALL_COLL
    assert sr.parse_choice("ALL", "scene_info") == ALL_SCENE
    assert sr.parse_choice("no", "collections") == []
    assert sr.parse_choice("sceneorg, hvsc", "collections") == ["sceneorg", "hvsc"]
    assert sr.parse_choice("", "collections", default=["x"]) == ["x"]
    assert sr.parse_choice(None, "collections") is None
    assert sr.parse_choice("hvsc,nope", "collections") is None
    assert sr.parse_choice("songdb", "collections") is None      # (a scene-info name)
    # quotes an env file or Compose's list syntax keeps, a stray comma
    assert sr.parse_choice('"no"', "collections") == []
    assert sr.parse_choice("'hvsc,aminet'", "collections") == ["hvsc", "aminet"]
    assert sr.parse_choice("no,", "scene_info") == []


def test_requests_add_up_in_a_fixed_order(clean):
    assert sr.request("collections", ["aminet"]) == ["aminet"]
    assert sr.request("collections", ["hvsc", "aminet"]) == ["hvsc", "aminet"]
    assert sr.pending() == {"collections": ["hvsc", "aminet"], "scene_info": []}
    sr._drop("collections", "hvsc")
    assert sr.pending()["collections"] == ["aminet"]


@pytest.mark.parametrize("env, coll, scene", [
    ({}, ALL_COLL, ALL_SCENE),
    ({"SONIQBOOM_COLLECTIONS": "no", "SONIQBOOM_SCENE_INFO": "hvsc,demozoo"},
     [], ["hvsc", "demozoo"]),
    # a value it can't read adds nothing (an opt-out never turns into a yes)
    ({"SONIQBOOM_COLLECTIONS": "aminet", "SONIQBOOM_SCENE_INFO": "nein"},
     ["aminet"], []),
])
def test_a_new_installs_first_start(clean, env, coll, scene):
    chosen = sr.first_start_defaults(env)
    assert chosen == {"collections": coll, "scene_info": scene}
    assert sr.pending() == {"collections": coll, "scene_info": scene}
    assert sr.asked()
    sr._drop("collections", "aminet")
    assert sr.first_start_defaults(env) is None        # once only
    assert "aminet" not in sr.pending()["collections"]


def test_an_existing_install_is_not_changed(clean):
    (clean / "library.json").write_text("{}")
    assert sr.first_start_defaults({}) is None
    assert not sr.asked()                       # (the installer asks it, once)
    assert sr.pending() == {"collections": [], "scene_info": []}


# ── The CLI (a process of its own, on a conf and data folder of its own) ────

def _cli(tmp_path, *args, stdin=None, conf=None, state=None):
    """``conf``: SoniqBoom.conf to start from; ``state``: the server's
    ``setup_state.json``."""
    env = dict(os.environ, HOME=str(tmp_path), SONIQBOOM_CONF=str(tmp_path / "SoniqBoom.conf"),
               SONIQBOOM_DATA_DIR=str(tmp_path / "data"))
    (tmp_path / "data").mkdir(exist_ok=True)
    if conf is not None:
        (tmp_path / "SoniqBoom.conf").write_text(json.dumps(conf))
    if state is not None:
        (tmp_path / "data" / "setup_state.json").write_text(json.dumps(state))
    r = subprocess.run([sys.executable, "-c", "from soniqboom.main import cli; cli()", *args],
                       env=env, capture_output=True, text=True, input=stdin, timeout=120)
    conf = json.loads((tmp_path / "SoniqBoom.conf").read_text())
    return r, conf.get("setup") or {}


def test_cli_add_and_list(tmp_path):
    r, setup = _cli(tmp_path, "collections", "add", "hvsc", "sceneorg")
    assert r.returncode == 0, r.stderr
    assert "Requested: HVSC (High Voltage SID Collection), scene.org." in r.stdout
    assert setup["pending"] == {"collections": ["hvsc", "sceneorg"], "scene_info": []}
    r, setup = _cli(tmp_path, "scene-info", "add")                  # (all)
    assert setup["pending"]["scene_info"] == ALL_SCENE
    r, _ = _cli(tmp_path, "collections", "list")
    lines = {ln.split()[0]: ln for ln in r.stdout.splitlines() if ln.startswith("  ")}
    assert "requested" in lines["hvsc"] and "requested" not in lines["modland"]
    r, setup = _cli(tmp_path, "collections", "add", "songdb")
    assert r.returncode == 2 and "unknown online collection" in r.stderr
    assert setup["pending"]["collections"] == ["hvsc", "sceneorg"]
    r, setup = _cli(tmp_path, "collections", "add", "hvsc", "all")
    assert r.returncode == 0 and setup["pending"]["collections"] == ALL_COLL


def test_cli_first_run_setup(tmp_path):
    # no terminal: yes to both, unless a flag says no
    r, setup = _cli(tmp_path, "first-run-setup", "--scene-info", "no")
    assert r.returncode == 0, r.stderr
    assert setup["pending"] == {"collections": ALL_COLL, "scene_info": []}
    assert setup["asked"] is True
    # asked once only: a second run changes nothing
    r, setup2 = _cli(tmp_path, "first-run-setup")
    assert setup2 == setup and "soniqboom collections add" in r.stdout


def test_cli_first_run_setup_on_an_existing_library(tmp_path):
    """An update without a terminal adds nothing and asks next time; the
    flags answer for it (and the earlier marker name is dropped)."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "library.aof").write_text("")
    r, setup = _cli(tmp_path, "first-run-setup")
    assert r.returncode == 0 and setup == {}
    assert "soniqboom collections add" in r.stdout
    r, setup = _cli(tmp_path, "first-run-setup", "--collections", "aminet", "--scene-info", "no",
                    conf={"setup": {"first_run_done": True}})
    assert r.returncode == 0, r.stderr
    assert setup["asked"] is True and "first_run_done" not in setup
    assert setup["pending"] == {"collections": ["aminet"], "scene_info": []}
    assert "Checking what is already set up" in r.stdout


def test_cli_an_update_without_a_terminal_takes_only_the_flags(tmp_path):
    """``install.sh --no-collections`` piped: no scene information either
    (only a new install gets yes without a terminal)."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "library.aof").write_text("")
    r, setup = _cli(tmp_path, "first-run-setup", "--collections", "no")
    assert r.returncode == 0, r.stderr
    assert setup["asked"] is True
    assert sr._pending_of({"setup": setup}) == {"collections": [], "scene_info": []}


def test_cli_reads_the_library_when_no_server_recorded_it(tmp_path):
    """No ``setup_state.json`` yet (no server of this version has run): the
    root folders and the collections on come from the library's files."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "library.json").write_text(json.dumps({
        "db_version": 2, "tracks": {}, "scan_dirs": {
            "/music/Aminet": {"path": "/music/Aminet"},
            "https://archive.scene.org/pub/music": {}},
        "config": {"online_sceneorg_enabled": True}}))
    r, setup = _cli(tmp_path, "collections", "add")
    assert r.returncode == 0, r.stderr
    assert "Aminet is already in your library as the root folder /music/Aminet" in r.stdout
    assert "scene.org is already on — left as is." in r.stdout
    assert setup["pending"]["collections"] == ["modland", "hvsc"]
    assert not (tmp_path / "data" / "setup_state.json").exists()      # (nothing written)


def test_cli_scene_information_already_there_is_not_offered(tmp_path):
    (tmp_path / "data" / "scene").mkdir(parents=True)
    (tmp_path / "data" / "scene" / "songdb.sqlite").write_bytes(b"x" * 10)
    (tmp_path / "data" / "scene" / "demozoo.sqlite").write_bytes(b"")     # (empty: none)
    r, setup = _cli(tmp_path, "scene-info", "add", "songdb", "demozoo",
                    conf={"renderers": {"hvsc_source": "online"}})
    assert r.returncode == 0, r.stderr
    assert "The UADE song database is already downloaded — left as is." in r.stdout
    assert setup["pending"]["scene_info"] == ["demozoo"]
    r, setup = _cli(tmp_path, "scene-info", "add", "hvsc")
    assert "HVSC song lengths and STIL are already downloaded — left as is." in r.stdout
    assert "Nothing to add." in r.stdout


def test_cli_a_waiting_removal_is_not_already_on(tmp_path):
    state = {"folders": [], "collections_on": ["aminet"]}
    r, setup = _cli(tmp_path, "collections", "remove", "aminet", state=state)
    assert setup["removing"]["collections"] == ["aminet"]
    r, setup = _cli(tmp_path, "collections", "add", "aminet")
    assert "already on" not in r.stdout
    assert setup["pending"]["collections"] == ["aminet"]
    assert setup["removing"]["collections"] == []


def test_cli_the_question_in_a_terminal_leaves_out_what_is_there(tmp_path):
    """The installer's path on an update: what is there is mentioned and not
    asked; a "no" also takes back what was waiting."""
    import pty
    import select
    import time
    data = tmp_path / "data"
    (data / "scene").mkdir(parents=True)
    (data / "library.aof").write_text("")
    for n in ("modland", "songdb", "demozoo"):
        (data / "scene" / f"{n}.sqlite").write_bytes(b"x")
    (data / "setup_state.json").write_text(json.dumps(
        {"folders": ["/music/HVSC"], "collections_on": ["modland", "hvsc"]}))
    (tmp_path / "SoniqBoom.conf").write_text(json.dumps(
        {"setup": {"pending": {"collections": ["aminet"], "scene_info": []}}}))
    env = dict(os.environ, HOME=str(tmp_path), SONIQBOOM_CONF=str(tmp_path / "SoniqBoom.conf"),
               SONIQBOOM_DATA_DIR=str(data))
    pid, fd = pty.fork()
    if pid == 0:                                        # (the child)
        os.execve(sys.executable, [sys.executable, "-c",
                                   "from soniqboom.main import cli; cli()", "first-run-setup"], env)
    out, sent, t0 = b"", 0, time.time()
    while time.time() - t0 < 60:
        if select.select([fd], [], [], 0.5)[0]:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if out.count(b"[Y/n]") > sent:
                os.write(fd, b"n\n")
                sent += 1
    os.waitpid(pid, 0)
    text = out.decode(errors="replace").replace("\r", "")
    assert "Modland is already on — left as is." in text
    assert "HVSC is already on — left as is." in text
    assert "HVSC song lengths and STIL already come with the HVSC collection" in text
    assert "Add the online collections — Aminet and scene.org?" in text
    assert "Set up the scene information" not in text           # (all of it is there)
    assert sent == 1
    setup = json.loads((tmp_path / "SoniqBoom.conf").read_text())["setup"]
    assert setup["asked"] is True and setup["pending"]["collections"] == []


_SHARES = {"network_shares": {
    "s1": {"protocol": "ftp", "host": "ftp.example.org", "remote_path": "/mirror/Aminet/mods"},
    "s2": {"protocol": "https", "host": "music.example.org", "remote_path": "/",
           "base_url": "https://music.example.org/", "alias": "Jukebox"},
}}


def test_cli_first_run_mentions_what_is_already_there(tmp_path):
    """A collection on, or held by a root folder (a network share, a local
    folder the server recorded), is mentioned and left as is."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "library.aof").write_text("")
    r, setup = _cli(tmp_path, "first-run-setup", "--collections", "yes", "--scene-info", "no",
                    conf=_SHARES, state={"folders": ["/music/C64Music"],
                                         "collections_on": ["sceneorg"]})
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert ("Aminet is already in your library as the root folder "
            "ftp://ftp.example.org/mirror/Aminet/mods — left as is.") in out
    assert ("HVSC is already in your library as the root folder /music/C64Music "
            "— left as is.") in out
    assert "scene.org is already on — left as is." in out
    assert "To add it anyway: Settings → Collections" in out
    assert setup["pending"] == {"collections": ["modland"], "scene_info": []}
    assert "Online collections: modland" in out


def test_cli_add_leaves_what_is_already_there(tmp_path):
    r, setup = _cli(tmp_path, "collections", "add", conf=_SHARES,
                    state={"folders": [], "collections_on": ["modland"]})
    assert r.returncode == 0, r.stderr
    assert "Aminet is already in your library as the root folder" in r.stdout
    assert "Modland is already on — left as is." in r.stdout
    assert setup["pending"]["collections"] == ["hvsc", "sceneorg"]
    r, setup = _cli(tmp_path, "collections", "add", "aminet")
    assert r.returncode == 0 and "Nothing to add." in r.stdout
    assert setup["pending"]["collections"] == ["hvsc", "sceneorg"]


def test_a_collection_is_known_by_its_name_as_a_word(monkeypatch):
    import soniqboom.config as config
    monkeypatch.setattr(config, "load_local_conf", lambda: {})
    for name, text in [("modland", "https://modland.ziphoid.com/pub/modules"),
                       ("hvsc", "https://hvsc.brona.dk/"), ("hvsc", "/music/HVSC_83"),
                       ("aminet", "smb://aminet-nas/music"), ("hvsc", "/m/C64Music"),
                       ("sceneorg", "/m/scene.org")]:
        assert sr.linked(name, [text]) == text, (name, text)
    for name, text in [("hvsc", "/Volumes/Data/archvsc"), ("sceneorg", "/home/u/sceneorgasm"),
                       ("aminet", "/music/Amiga"), ("modland", "/music/Mods")]:
        assert sr.linked(name, [text]) is None, (name, text)


def test_a_collection_is_known_by_its_site_mirror_or_folder_name(monkeypatch):
    import soniqboom.config as config
    shares = {
        "a": {"protocol": "ftp", "host": "de.aminet.net", "remote_path": "/pub"},
        "m": {"protocol": "ftp", "host": "ftp.modland.com", "remote_path": "/pub/modules"},
        "n": {"protocol": "smb", "host": "nas", "share": "Music", "remote_path": "/Modland"},
        "o": {"protocol": "https", "host": "x.org", "remote_path": "/",
              "base_url": "https://x.org/", "alias": "Amiga stuff"},
    }
    monkeypatch.setattr(config, "load_local_conf", lambda: {"network_shares": shares})
    assert sr.linked("aminet") == "ftp://de.aminet.net/pub"
    assert sr.linked("modland") == "ftp://ftp.modland.com/pub/modules"
    assert sr.linked("hvsc") is None and sr.linked("sceneorg") is None
    assert sr.linked("sceneorg", ["/Volumes/X/files.scene.org/music"]) == \
        "/Volumes/X/files.scene.org/music"
    assert sr.linked("hvsc", ["/music/Amiga", "/music/HVSC_83/C64Music"]) == \
        "/music/HVSC_83/C64Music"
    shares.pop("a")
    assert sr.linked("aminet", ["/music/Amiga"]) is None
    shares["n"] = {"protocol": "smb", "host": "nas", "share": "Music", "alias": "Aminet mods"}
    assert sr.linked("modland") == "ftp://ftp.modland.com/pub/modules"
    assert sr.linked("aminet") == "Aminet mods (smb://nas/Music)"


def test_the_server_records_its_folders_and_collections(clean, monkeypatch):
    monkeypatch.setattr(sr, "_local_roots", lambda: ["/music/Aminet"])
    monkeypatch.setattr(sr, "collection_on", lambda n: n == "hvsc")
    monkeypatch.setattr(sr, "_state_written", None)
    assert sr.read_state() == {"folders": [], "collections_on": [],    # (a new install)
                               "hvsc_local_docs": True}
    sr._record_state()
    assert sr.read_state() == {"folders": ["/music/Aminet"], "collections_on": ["hvsc"],
                               "hvsc_local_docs": True}
    writes = []
    monkeypatch.setattr(sr.os, "replace", lambda *a: writes.append(a))
    sr._record_state()
    assert writes == []                     # (unchanged: not written again)


# ── The server applying them ─────────────────────────────────────────────────

@pytest.fixture
def calls(monkeypatch):
    """The admin handlers the requests use, recorded instead of run."""
    from fastapi import HTTPException
    from soniqboom.api import admin
    from soniqboom.core import demozoo, scene_metadata, songdb
    got = []
    fail = set()

    async def online(key, body, _tok=""):
        if f"busy-{key}" in fail:
            raise HTTPException(409, f"{key} is busy — try again when the current step has finished.")
        got.append(("online", key, body))

    async def modland(body, _tok=""):
        if "modland-conflict" in fail:
            raise HTTPException(409, "already added as the network share \"Mods\"")
        got.append(("modland", body))

    async def settings_(body, _tok=""):
        got.append(("settings", body))

    def refresh(name, has):
        async def f(_tok=""):
            got.append((name, "refresh"))
            if name in fail:
                return {"error": "download failed"}
            has[0] = True
            return {}
        return f

    def apply(name):
        async def f(_tok=""):
            got.append((name, "apply"))
            return {}
        return f
    idx = {"scene": [False], "songdb": [False], "demozoo": [False]}
    monkeypatch.setattr(admin, "admin_online_catalog_set", online)
    monkeypatch.setattr(admin, "admin_modland_catalog_set", modland)
    monkeypatch.setattr(admin, "update_settings", settings_)
    monkeypatch.setattr(admin, "admin_scene_refresh", refresh("scene", idx["scene"]))
    monkeypatch.setattr(admin, "admin_songdb_refresh", refresh("songdb", idx["songdb"]))
    monkeypatch.setattr(admin, "admin_demozoo_refresh", refresh("demozoo", idx["demozoo"]))
    monkeypatch.setattr(admin, "admin_scene_apply", apply("scene"))
    monkeypatch.setattr(admin, "admin_songdb_apply", apply("songdb"))
    monkeypatch.setattr(admin, "admin_demozoo_apply", apply("demozoo"))
    monkeypatch.setattr(scene_metadata, "has_index", lambda: idx["scene"][0])
    monkeypatch.setattr(songdb, "has_index", lambda: idx["songdb"][0])
    monkeypatch.setattr(songdb, "set_auto_apply", lambda on: got.append(("songdb", "auto", on)))
    monkeypatch.setattr(demozoo, "has_index", lambda: idx["demozoo"][0])
    monkeypatch.setattr(settings, "hvsc_source", "local")
    from soniqboom.core import online_hvsc
    monkeypatch.setattr(online_hvsc.HVSC_ONLINE, "enabled", lambda: False)
    monkeypatch.setattr(settings, "hvsc_docs_path", "")
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    # the root folders: none but the local ones a test gives
    monkeypatch.setattr(sr, "_local_roots", lambda: [])
    monkeypatch.setattr(sr, "_root_folders", lambda local=None: [(p, [p]) for p in local or ()])
    monkeypatch.setattr(sr, "collection_on", lambda n: False)
    monkeypatch.setattr(sr, "_index_present",
                        lambda n: idx["scene" if n == "modland" else n][0])
    return got, fail, idx


async def test_every_request_does_what_its_switch_does(clean, calls):
    got, fail, idx = calls
    sr.request("collections", ["modland", "aminet", "sceneorg"])
    sr.request("scene_info", ALL_SCENE)
    done = await sr.apply_pending()
    assert done == {"collections": ["modland", "aminet", "sceneorg"], "scene_info": ALL_SCENE,
                    "removed": {"collections": [], "scene_info": []},
                    "left": {"collections": [], "scene_info": []}}
    assert got == [
        ("modland", {"enabled": True}),
        ("online", "aminet", {"enabled": True}),
        ("online", "sceneorg", {"enabled": True}),
        ("settings", {"renderers": {"hvsc_source": "online"}}),
        ("scene", "refresh"),
        ("songdb", "auto", True), ("songdb", "refresh"),
        ("demozoo", "refresh"), ("demozoo", "apply"),
    ]
    assert sr.pending() == {"collections": [], "scene_info": []}
    # asked again once the indexes are in: already there, left as is
    got.clear()
    sr.request("scene_info", ["modland", "songdb", "demozoo"])
    done = await sr.apply_pending()
    assert got == [] and sr.pending()["scene_info"] == []
    assert done["left"]["scene_info"] == ["modland", "songdb", "demozoo"]


async def test_a_failed_download_waits_and_a_local_hvsc_folder_stays(clean, calls, monkeypatch):
    got, fail, idx = calls
    fail.update({"demozoo", "modland-conflict"})
    monkeypatch.setattr(settings, "hvsc_docs_path", "/music/C64Music/DOCUMENTS")
    sr.request("collections", ["modland"])
    sr.request("scene_info", ["hvsc", "demozoo"])
    done = await sr.apply_pending()
    # the share conflict won't go away by waiting: dropped (and logged);
    # the local HVSC folder is kept; the failed download stays requested
    assert (done["collections"], done["left"]["scene_info"]) == (["modland"], ["hvsc"])
    assert ("settings", {"renderers": {"hvsc_source": "online"}}) not in got
    assert sr.pending() == {"collections": [], "scene_info": ["demozoo"]}
    got.clear()
    await sr.apply_pending()                           # (within the hour: not again)
    assert got == []
    sr._retry_at.clear()                               # (an hour later)
    fail.clear()
    assert (await sr.apply_pending())["scene_info"] == ["demozoo"]
    assert sr.pending()["scene_info"] == []


async def test_a_collection_already_in_a_root_folder_is_left_as_is(clean, calls, monkeypatch):
    """Not switched on; HVSC song lengths then come from the internet (the
    collection that would have brought them stays off)."""
    got, _fail, _idx = calls
    monkeypatch.setattr(sr, "_local_roots", lambda: ["/music/HVSC"])
    sr.request("collections", ["hvsc", "aminet"])
    sr.request("scene_info", ["hvsc"])
    done = await sr.apply_pending()
    assert (done["collections"], done["left"]["collections"]) == (["aminet"], ["hvsc"])
    assert sr.pending()["collections"] == []
    assert got == [("online", "aminet", {"enabled": True}),
                   ("settings", {"renderers": {"hvsc_source": "online"}})]
    # one already on is left as is too
    got.clear()
    monkeypatch.setattr(sr, "_local_roots", lambda: [])
    monkeypatch.setattr(sr, "collection_on", lambda n: n == "hvsc")
    sr.request("collections", ["hvsc"])
    assert (await sr.apply_pending())["left"]["collections"] == ["hvsc"]
    assert got == []


async def test_scene_information_already_there_is_left_as_is(clean, calls, monkeypatch):
    """Downloaded already, or the HVSC documents in use from elsewhere: not
    applied again (and the HVSC collection's own option not changed)."""
    got, _fail, idx = calls
    idx["songdb"][0] = True
    monkeypatch.setattr(sr, "collection_on", lambda n: n == "hvsc")
    sr.request("scene_info", ["songdb", "hvsc"])
    done = await sr.apply_pending()
    assert done["left"]["scene_info"] == ["hvsc", "songdb"] and got == []
    assert sr.pending()["scene_info"] == []


async def test_offline_the_requests_wait(clean, calls, monkeypatch):
    got, _fail, _idx = calls
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "1")
    sr.request("collections", ["hvsc"])
    done = await sr.apply_pending()
    assert (done["collections"], done["scene_info"]) == ([], [])
    assert got == [] and sr.pending()["collections"] == ["hvsc"]


async def test_hvsc_documents_come_with_the_hvsc_collection(clean, calls):
    """With the HVSC collection requested too, the HVSC card is not switched
    to "Download from the internet" (that stops a scan finding a local HVSC
    folder later; the collection's documents let one take over)."""
    got, _fail, _idx = calls
    sr.request("collections", ["hvsc"])
    sr.request("scene_info", ["hvsc"])
    done = await sr.apply_pending()
    assert (done["collections"], done["scene_info"]) == (["hvsc"], ["hvsc"])
    assert got == [("online", "hvsc", {"enabled": True})]


async def test_a_busy_collection_waits_and_a_new_request_skips_the_wait(clean, calls):
    got, fail, _idx = calls
    fail.add("busy-aminet")
    sr.request("collections", ["aminet"])
    assert (await sr.apply_pending())["collections"] == []
    assert sr.pending()["collections"] == ["aminet"]
    fail.clear()
    await sr.apply_pending()                           # (within the hour: waits)
    assert got == []
    sr.request("collections", ["aminet"])              # asked again: tried at once
    assert (await sr.apply_pending())["collections"] == ["aminet"]


async def test_a_request_not_cleared_from_the_conf_is_not_redone_at_once(clean, calls, monkeypatch):
    got, _fail, idx = calls
    sr.request("scene_info", ["demozoo"])

    def broken(kind, name, remove=False):
        raise OSError("disk full")
    monkeypatch.setattr(sr, "_drop", broken)
    assert (await sr.apply_pending())["scene_info"] == []
    assert got == [("demozoo", "refresh"), ("demozoo", "apply")]
    monkeypatch.setattr(sr, "_index_present", lambda n: False)   # (not "already there")
    await sr.apply_pending()
    assert got == [("demozoo", "refresh"), ("demozoo", "apply")]   # (not every 10 s)


async def test_the_server_picks_up_a_request_while_it_runs(clean, calls, monkeypatch):
    import asyncio
    got, _fail, _idx = calls
    monkeypatch.setattr(sr, "_POLL_S", 0.02)
    sr.start()
    try:
        await asyncio.sleep(0.05)
        assert got == []
        sr.request("collections", ["sceneorg"])
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.02)
        assert got == [("online", "sceneorg", {"enabled": True})]
        assert sr.all_tasks()
    finally:
        for t in sr.all_tasks():
            t.cancel()
        await asyncio.sleep(0)


def test_the_server_and_its_start_are_wired():
    """The start-up applies the requests and the stop cancels them; the CLI's
    serve decides a new install's first start before the server runs."""
    import inspect
    from soniqboom import main
    assert "setup_requests.start()" in inspect.getsource(main.startup)
    assert "setup_requests.all_tasks()" in inspect.getsource(main._background_writer_tasks)
    src = inspect.getsource(main.cli)
    assert src.index("_first_start_setup()") < src.index("uvicorn.run(")


async def test_the_real_switch_handler(clean, monkeypatch):
    """``admin_online_catalog_set`` itself, with a stand-in collection: the
    request starts its ``enable`` job."""
    from soniqboom.core import online_catalog
    started = []

    class Cat:
        label = "Stand-in"
        def enabled(self): return False
        def busy(self): return False
        def status(self): return {"state": "off"}
        async def enable(self): return {}
        def start_job(self, fn, *a, **kw):
            started.append(fn.__name__)
            return True
    monkeypatch.setattr(online_catalog, "get", lambda key: Cat() if key == "aminet" else None)
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    monkeypatch.setattr(sr, "_local_roots", lambda: [])
    monkeypatch.setattr(sr, "_root_folders", lambda local=None: [])
    sr.request("collections", ["aminet"])
    assert (await sr.apply_pending())["collections"] == ["aminet"]
    assert started == ["enable"]


# ── Removing ─────────────────────────────────────────────────────────────────

def test_the_later_request_for_a_name_wins(clean):
    sr.request("collections", ["hvsc", "aminet"])
    sr.request("collections", ["aminet"], remove=True)
    assert sr.pending() == {"collections": ["hvsc"], "scene_info": []}
    assert sr.pending(remove=True) == {"collections": ["aminet"], "scene_info": []}
    sr.request("collections", ["aminet"])
    assert sr.pending(remove=True)["collections"] == []
    assert sr.pending()["collections"] == ["hvsc", "aminet"]


def test_cli_remove(tmp_path):
    r, setup = _cli(tmp_path, "collections", "add")
    r, setup = _cli(tmp_path, "collections", "remove", "aminet", "modland")
    assert r.returncode == 0, r.stderr
    assert "Removal requested: Modland, Aminet." in r.stdout
    assert setup["pending"]["collections"] == ["hvsc", "sceneorg"]
    assert setup["removing"]["collections"] == ["modland", "aminet"]
    r, _ = _cli(tmp_path, "collections", "list")
    lines = {ln.split()[0]: ln for ln in r.stdout.splitlines() if ln.startswith("  ")}
    assert "remove requested" in lines["aminet"] and "add requested" in lines["hvsc"]
    r, setup = _cli(tmp_path, "scene-info", "remove", "all")
    assert setup["removing"]["scene_info"] == ALL_SCENE
    assert "download is deleted" in r.stdout
    # nothing named: refused (never "all" by default)
    r = subprocess.run([sys.executable, "-c", "from soniqboom.main import cli; cli()",
                        "collections", "remove"],
                       env=dict(os.environ, HOME=str(tmp_path),
                                SONIQBOOM_CONF=str(tmp_path / "SoniqBoom.conf"),
                                SONIQBOOM_DATA_DIR=str(tmp_path / "data")),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2


@pytest.fixture
def removals(calls, monkeypatch, tmp_path):
    """The resets / switches the removals use, recorded; the scene indexes
    as files in a scratch folder."""
    from soniqboom.api import admin
    from soniqboom.core import demozoo, online_hvsc, scene_metadata, songdb
    got, fail, _idx = calls
    files = {}
    for name, mod in (("songdb", songdb), ("demozoo", demozoo)):
        f = tmp_path / f"{name}.sqlite"
        f.write_text("x")
        (tmp_path / f"{name}.sqlite-wal").write_text("x")
        files[name] = f
        monkeypatch.setattr(mod, "_db_path", lambda f=f: f)
        monkeypatch.setitem(mod._status, "refreshing", False)
        monkeypatch.setitem(mod._status, "applying", False)

    async def songdb_reset():
        got.append(("songdb", "reset"))
        return {}

    async def demozoo_reset(_tok=""):
        got.append(("demozoo", "reset"))
        return {"error": "apply already running"} if "demozoo-busy" in fail else {}

    async def modland_remove():
        got.append(("modland", "remove"))
        return {"error": None}
    monkeypatch.setattr(songdb, "reset_to_file_state", songdb_reset)
    monkeypatch.setattr(admin, "admin_demozoo_reset", demozoo_reset)
    monkeypatch.setattr(scene_metadata, "remove_from_library", modland_remove)
    opts = {online_hvsc.OPT_LOCAL_DOCS: True}
    monkeypatch.setattr(online_hvsc.HVSC_ONLINE, "enabled", lambda: True)
    monkeypatch.setattr(online_hvsc.HVSC_ONLINE, "options", lambda: dict(opts))
    monkeypatch.setattr(settings, "hvsc_source", "online")
    return got, fail, files, opts


async def test_every_removal_does_what_its_switch_or_reset_does(clean, removals):
    got, fail, files, opts = removals
    sr.request("collections", ["modland", "sceneorg"], remove=True)
    sr.request("scene_info", ALL_SCENE, remove=True)
    done = await sr.apply_pending()
    assert done["removed"] == {"collections": ["modland", "sceneorg"], "scene_info": ALL_SCENE}
    assert got == [
        ("modland", {"enabled": False}),
        ("online", "sceneorg", {"enabled": False}),
        ("settings", {"renderers": {"hvsc_source": "local"}}),
        ("online", "hvsc", {"options": {"local_docs": False}}),
        ("modland", "remove"),
        ("songdb", "auto", False), ("songdb", "reset"),
        ("demozoo", "reset"),
    ]
    assert not any(f.exists() or f.with_name(f.name + "-wal").exists() for f in files.values())
    assert sr.pending(remove=True) == {"collections": [], "scene_info": []}


async def test_a_failed_reset_keeps_the_download_and_waits(clean, removals):
    got, fail, files, _opts = removals
    fail.add("demozoo-busy")
    sr.request("scene_info", ["demozoo"], remove=True)
    done = await sr.apply_pending()
    assert done["removed"]["scene_info"] == []
    assert files["demozoo"].exists()                    # not deleted under a running apply
    assert sr.pending(remove=True)["scene_info"] == ["demozoo"]


async def test_removals_need_no_internet(clean, removals, monkeypatch):
    got, _fail, _files, _opts = removals
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "1")
    sr.request("collections", ["aminet"])
    sr.request("collections", ["sceneorg"], remove=True)
    done = await sr.apply_pending()
    assert done["removed"]["collections"] == ["sceneorg"] and done["collections"] == []
    assert got == [("online", "sceneorg", {"enabled": False})]


async def test_adding_hvsc_back_turns_the_collections_option_on(clean, removals):
    got, _fail, _files, opts = removals
    opts["local_docs"] = False
    sr.request("scene_info", ["hvsc"])
    from soniqboom.config import settings as st
    st.hvsc_source = "local"
    await sr.apply_pending()
    assert got == [("online", "hvsc", {"options": {"local_docs": True}})]


def test_cli_remove_takes_names_only(tmp_path):
    """``remove y`` / ``true`` must not mean "everything"."""
    for word in ("y", "true", "none"):
        r, setup = _cli(tmp_path, "scene-info", "remove", word)
        assert r.returncode == 2, word
        assert not (setup.get("removing") or {}).get("scene_info")
    r, setup = _cli(tmp_path, "scene-info", "remove", "demozoo,songdb")
    assert r.returncode == 0 and setup["removing"]["scene_info"] == ["songdb", "demozoo"]


async def test_removing_hvsc_with_the_collection_off_deletes_the_documents(
        clean, removals, monkeypatch, tmp_path):
    from soniqboom.core import online_hvsc
    got, _fail, _files, opts = removals
    docs = tmp_path / "hvsc" / "DOCUMENTS"
    docs.mkdir(parents=True)
    (docs / "Songlengths.md5").write_text("x")
    monkeypatch.setattr(online_hvsc.HVSC_ONLINE, "enabled", lambda: False)
    monkeypatch.setattr(online_hvsc.HVSC_ONLINE, "busy", lambda: False)
    monkeypatch.setattr(online_hvsc.HVSC_ONLINE, "docs_dir", lambda: docs)
    monkeypatch.setattr(online_hvsc, "fallback_active", lambda: False)
    sr.request("scene_info", ["hvsc"], remove=True)
    done = await sr.apply_pending()
    assert done["removed"]["scene_info"] == ["hvsc"]
    assert not docs.exists()
    # the option goes off too: the collection switched on later keeps them
    # to its own tunes
    assert ("online", "hvsc", {"options": {"local_docs": False}}) in got


async def test_a_reversed_request_is_skipped_by_the_running_pass(clean, calls, monkeypatch):
    got, _fail, idx = calls
    sr.request("collections", ["aminet"])
    sr.request("scene_info", ["demozoo"])
    real = sr._apply_collection

    async def and_then_reverse(name):
        sr.request("scene_info", ["demozoo"], remove=True)     # (arrives meanwhile)
        return await real(name)
    monkeypatch.setattr(sr, "_apply_collection", and_then_reverse)
    done = await sr.apply_pending()
    assert done["collections"] == ["aminet"] and done["scene_info"] == []
    assert ("demozoo", "refresh") not in got
    assert sr.pending(remove=True)["scene_info"] == ["demozoo"]


async def test_a_busy_removal_is_tried_again_within_minutes(clean, removals):
    import time
    _got, fail, _files, _opts = removals
    fail.add("demozoo-busy")
    sr.request("scene_info", ["demozoo"], remove=True)
    await sr.apply_pending()
    until, _failed = sr._retry_at[(True, "scene_info", "demozoo")]
    assert until - time.time() <= sr._BUSY_RETRY_S + 1
