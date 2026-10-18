# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Folder browsing answers only under the registered scan roots.

``/api/fstree/children``, ``/tracks``, ``/tracks-with-meta`` and ``/refresh``
refuse every path outside the server's own scan roots — ``/``, a sibling
folder (existing or missing), a ``..`` or a symlink leading out of a root, a
root the client names — with one identical 403, before the path is looked at
on disk.  Browsing a root and its subfolders keeps working for local folders,
network shares and online collections (``test_modland_catalog`` covers the
catalogue end to end).  In-process: a read-only account over ``TestClient``."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soniqboom.api import fstree
from soniqboom.core import store as store_mod
from soniqboom.core import users as users_mod
from soniqboom.core.filesource import DirEntry, register_source, remove_source
from soniqboom.core.users import UserStore

PW = "Reader-pass-123!"
DENY = {"detail": "Path is not under any registered scan root"}


@pytest.fixture
def env(tmp_data_dir, tmp_path, monkeypatch):
    from soniqboom.main import app
    us = UserStore(tmp_data_dir, persist_sessions=True)
    monkeypatch.setattr(users_mod, "_instance", us)
    us.create("admin", "Admin-pass-123!", "admin")
    us.create("reader", PW, "readonly")
    st = store_mod.TrackStore()
    monkeypatch.setattr(store_mod, "_store", st)
    for name in ("_TRACKS_META_CACHE", "_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO", "_REFRESH_RECENT"):
        monkeypatch.setattr(fstree, name, {})

    fs = tmp_path.resolve() / "fs"
    lib = fs / "lib"
    (lib / "Album" / "Disc 1").mkdir(parents=True)
    (lib / "Album" / "song.mp3").write_bytes(b"\xff\xfb" + b"\0" * 64)
    (fs / "outside" / "Private").mkdir(parents=True)
    (fs / "outside" / "Private" / "secret-diary.mp3").write_bytes(b"\xff\xfb" + b"\0" * 64)
    (fs / "outside" / "top-secret.flac").write_bytes(b"fLaC" + b"\0" * 64)
    (fs / "libx").mkdir()                                   # a prefix sibling of "lib"
    (fs / "libx" / "x.mp3").write_bytes(b"\xff\xfb" + b"\0" * 64)
    os.symlink(fs / "outside", lib / "escape")              # leads out of the root
    os.symlink(lib / "Album", lib / "Shortcut")             # stays inside it
    st.upsert_scan_dir(str(lib))

    c = TestClient(app, base_url="http://127.0.0.1:8471")
    r = c.post("/api/auth/login", json={"username": "reader", "password": PW})
    assert r.status_code == 200, r.text

    class E:
        client = c
        store = st
    E.fs, E.lib, E.out, E.missing = fs, str(lib), str(fs / "outside"), str(fs / "not-here")
    return E


def _get(env, endpoint, **params):
    return env.client.get(f"/api/fstree/{endpoint}", params=params)


def _bad_paths(env):
    return {
        "root of the disk": "/",
        "existing sibling": env.out,
        "existing sibling, subfolder": env.out + "/Private",
        "missing sibling": env.missing,
        "missing folder under a missing sibling": env.missing + "/deeper",
        "prefix sibling of the root": str(env.fs / "libx"),
        "dot-dot escape": env.lib + "/../outside",
        "dot-dot escape, missing": env.lib + "/../not-here",
        "dot-dot escape via a subfolder": env.lib + "/Album/../../outside/Private",
        "symlink escape": env.lib + "/escape",
        "symlink escape, subfolder": env.lib + "/escape/Private",
        "symlink escape, missing": env.lib + "/escape/not-here",
        "relative path": "outside",
        "empty": "",
        "nul byte": env.lib + "/\x00",
    }


ENDPOINTS = [
    ("children", {}),
    ("tracks", {}),
    ("tracks", {"recursive": "true"}),
    ("tracks-with-meta", {}),
    ("tracks-with-meta", {"recursive": "true"}),
    ("tracks-with-meta", {"recursive": "true", "limit": "50"}),
]


@pytest.mark.parametrize("endpoint,extra", ENDPOINTS)
def test_every_path_outside_the_roots_gets_the_same_403(env, endpoint, extra):
    for label, path in _bad_paths(env).items():
        roots = [None, path, "/"] if endpoint == "children" else [None]
        for root in roots:
            params = {"path": path, **extra}
            if root is not None:
                params["root"] = root
            r = _get(env, endpoint, **params)
            assert r.status_code == 403, (endpoint, label, root, r.status_code, r.text[:200])
            assert r.json() == DENY, (endpoint, label, r.text[:200])


def test_refresh_refuses_the_same_paths(env):
    for label, path in _bad_paths(env).items():
        if not path:
            continue                                        # (400: path is required)
        r = env.client.post("/api/fstree/refresh", json={"path": path})
        assert r.status_code == 403, (label, r.status_code, r.text[:200])
        assert r.json() == DENY


def test_paths_outside_the_roots_are_never_looked_up_on_disk(env, monkeypatch):
    """Missing and existing out-of-root paths can't be told apart: the refusal
    comes before any stat / lstat / scandir / walk / realpath of the path."""
    touched: list[str] = []
    watch = (env.out, env.missing, str(env.fs / "libx"))

    def spy(fn):
        def wrapped(p, *a, **kw):
            s = os.fsdecode(p) if isinstance(p, (str, bytes, os.PathLike)) else ""
            if s.startswith(watch):
                touched.append(f"{fn.__name__}({s})")
            return fn(p, *a, **kw)
        return wrapped
    for name in ("stat", "lstat", "scandir", "walk", "listdir"):
        monkeypatch.setattr(os, name, spy(getattr(os, name)))
    monkeypatch.setattr(os.path, "realpath", spy(os.path.realpath))
    for endpoint, extra in ENDPOINTS:
        for path in (env.out, env.out + "/Private", env.missing, str(env.fs / "libx")):
            assert _get(env, endpoint, path=path, root=path, **extra).status_code == 403
    monkeypatch.undo()
    assert touched == []


def test_children_of_a_root_and_its_subfolders_still_list(env):
    for root in (env.lib, None, "/", env.out):              # the hint never matters
        params = {"path": env.lib}
        if root is not None:
            params["root"] = root
        r = _get(env, "children", **params)
        assert r.status_code == 200, (root, r.text)
        names = [c["name"] for c in r.json()["children"]]
        # The symlink that leads out of the root is not listed; one inside is.
        assert names == ["Album", "Shortcut"], (root, names)
        assert r.json()["children"][0]["path"] == env.lib + "/Album"
        assert r.json()["children"][0]["rel"] == "Album"
    r = _get(env, "children", path=env.lib + "/Album", root=env.lib)
    assert r.status_code == 200
    assert [c["rel"] for c in r.json()["children"]] == ["Album/Disc 1"]
    # A symlink that stays inside the root browses (as its real folder).
    r = _get(env, "children", path=env.lib + "/Shortcut", root=env.lib)
    assert r.status_code == 200 and [c["name"] for c in r.json()["children"]] == ["Disc 1"]
    # A missing folder INSIDE a root is a plain 404.
    r = _get(env, "children", path=env.lib + "/Gone", root=env.lib)
    assert r.status_code == 404


def test_track_listings_of_a_root_still_work(env):
    r = _get(env, "tracks", path=env.lib + "/Album")
    assert r.status_code == 200 and r.json()["tracks"] == [env.lib + "/Album/song.mp3"]
    r = _get(env, "tracks", path=env.lib, recursive="true")
    assert r.status_code == 200 and r.json()["tracks"] == [env.lib + "/Album/song.mp3"]
    r = _get(env, "tracks-with-meta", path=env.lib + "/Album")
    assert r.status_code == 200 and [t["path"] for t in r.json()] == [env.lib + "/Album/song.mp3"]
    r = _get(env, "tracks-with-meta", path=env.lib, recursive="true", limit="50")
    assert r.status_code == 200 and r.json()["total"] == 1
    assert _get(env, "tracks-with-meta", path=env.lib + "/Gone").status_code == 404
    r = env.client.post("/api/fstree/refresh", json={"path": env.lib + "/Album"})
    assert r.status_code == 200 and r.json()["queued"] is True


def test_a_root_registered_through_a_symlinked_spelling_browses(env, tmp_path):
    """A root stored under a symlinked spelling (``/tmp/…`` for
    ``/private/tmp/…``) browses by either spelling; a sibling still can't."""
    link = tmp_path.resolve() / "lib-link"
    os.symlink(env.lib, link)
    env.store.delete_scan_dir(env.lib)
    env.store.upsert_scan_dir(str(link))
    for path in (str(link), env.lib, str(link) + "/Album"):
        r = _get(env, "children", path=path, root=str(link))
        assert r.status_code == 200, (path, r.text)
    assert _get(env, "children", path=env.out, root=str(link)).status_code == 403
    assert _get(env, "tracks", path=str(link) + "/../outside").status_code == 403


def test_a_root_that_is_slash_allows_everything_under_it(env):
    """(An admin who registers ``/`` as a scan root browses the whole disk.)"""
    env.store.upsert_scan_dir("/")
    assert _get(env, "children", path=env.out, root="/").status_code == 200


# ── network shares ──────────────────────────────────────────────────────────

class _FakeShare:
    """A share whose server would happily resolve ``..`` above the root."""
    auth_key = None

    def __init__(self):
        self.listed: list[str] = []

    def list_dir(self, path):
        self.listed.append(path)
        if path in ("/", ""):
            return [DirEntry(name="Artist", path="/Artist", is_dir=True, size=0, mtime=0.0)]
        if path == "/Artist":
            return [DirEntry(name="Album", path="/Artist/Album", is_dir=True, size=0, mtime=0.0)]
        return [DirEntry(name="LEAKED", path=path + "/LEAKED", is_dir=True, size=0, mtime=0.0)]

    def close(self):
        pass


@pytest.fixture
def share(env, monkeypatch):
    root = "smb://nas/Music"
    src = _FakeShare()
    register_source(root, src)
    env.store.upsert_scan_dir(root, network_share_id="nas1")
    monkeypatch.setattr(fstree, "_get_or_build_scan_root_sorted", lambda s, h: ([], {}))
    yield root, src
    remove_source(root)


def test_share_browsing_still_works(env, share):
    root, src = share
    r = _get(env, "children", path=root, root=root)
    assert r.status_code == 200 and [c["name"] for c in r.json()["children"]] == ["Artist"]
    r = _get(env, "children", path=root + "/Artist", root=root)
    assert r.status_code == 200 and [c["path"] for c in r.json()["children"]] == [
        root + "/Artist/Album"]
    r = _get(env, "tracks-with-meta", path=root + "/Artist")
    assert r.status_code == 200 and r.json() == []
    assert src.listed == ["/", "/Artist"]


def test_share_paths_outside_the_share_root_are_refused(env, share):
    root, src = share
    for bad in (root + "/..", root + "/../Other", root + "/Artist/../../Other",
                root + "/./Artist", root + "/Artist\\..\\..\\Other",
                "smb://nas/Other", "smb://nas/MusicX", "smb://other-host/Music",
                "ftp://nas/Music", root + "/\x00"):
        for endpoint in ("children", "tracks", "tracks-with-meta"):
            r = _get(env, endpoint, path=bad, root=root)
            assert r.status_code == 403, (endpoint, bad, r.status_code, r.text[:200])
            assert r.json() == DENY
    assert src.listed == []                                 # the share was never asked


def test_a_registered_share_that_is_offline_still_says_so(env, monkeypatch):
    root = "smb://nas/Offline"
    env.store.upsert_scan_dir(root, network_share_id="nas2")  # no source connected
    r = _get(env, "children", path=root, root=root)
    assert r.status_code == 503


# ── indexed folders, and the event loop ─────────────────────────────────────

def _index(env, rel_paths):
    import uuid
    from soniqboom.core.data import path_hash
    root = env.lib
    for rel in rel_paths:
        p = f"{root}/{rel}"
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_bytes(b"\xff\xfb" + b"\0" * 64)
        env.store.upsert_track({
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, p)), "path": p, "title": Path(p).stem,
            "format": "MP3", "scan_root_hash": path_hash(root),
            "dir_hash": path_hash(str(Path(p).parent))})


def test_indexed_folders_browse_from_the_index_and_escapes_still_fail(env):
    _index(env, ["Indexed/A/one.mp3", "Indexed/B/two.mp3"])
    r = _get(env, "children", path=env.lib + "/Indexed", root=env.lib)
    assert r.status_code == 200 and [c["name"] for c in r.json()["children"]] == ["A", "B"]
    r = _get(env, "tracks-with-meta", path=env.lib + "/Indexed", recursive="true", limit="50")
    assert r.status_code == 200 and r.json()["total"] == 2
    for bad in (env.lib + "/Indexed/../../outside", env.out, "/"):
        assert _get(env, "children", path=bad, root=env.lib).status_code == 403


def test_path_lookups_never_run_on_the_event_loop(env, monkeypatch):
    """``realpath`` of a requested path (a stat per component — seconds on a
    stalled network mount) runs in a worker thread, never on the loop."""
    import asyncio
    on_loop: list[str] = []
    real = os.path.realpath

    def spy(p, *a, **kw):
        try:
            asyncio.get_running_loop()
            on_loop.append(os.fsdecode(p))
        except RuntimeError:
            pass
        return real(p, *a, **kw)
    monkeypatch.setattr(os.path, "realpath", spy)
    _index(env, ["Indexed/A/one.mp3"])
    for path in ("/", env.out, env.missing, env.lib, env.lib + "/Album", env.lib + "/Indexed",
                 env.lib + "/escape", env.lib + "/../outside"):
        for endpoint, extra in (("children", {}), ("tracks", {}), ("tracks-with-meta", {})):
            _get(env, endpoint, path=path, root=env.lib, **extra)
    monkeypatch.undo()
    assert on_loop == []
