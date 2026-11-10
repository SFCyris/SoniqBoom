# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Removing a folder whose mount can't be reached takes effect at once: no
realpath of a listed folder, and the tracks purged in slices (the loop keeps
serving) — stopping when the folder is added again."""
from __future__ import annotations

import asyncio
import pathlib
import threading
import time

import pytest

from soniqboom.core import data, scanner, watcher
from soniqboom.core import store as store_mod
from soniqboom.core.data import path_hash

ROOT = "/Volumes/Gone/Music - purchases"


@pytest.fixture()
def store(monkeypatch):
    s = store_mod.TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    return s


@pytest.fixture()
def stalled_mount(monkeypatch):
    """Every realpath blocks — as on a hung SMB mount."""
    calls = []

    def _hang(self, *a, **k):
        calls.append(str(self))
        time.sleep(30)
        raise AssertionError("realpath of a stalled mount")
    monkeypatch.setattr(pathlib.Path, "resolve", _hang)
    return calls


@pytest.mark.asyncio
async def test_a_listed_folder_is_found_without_touching_the_disk(store, stalled_mount):
    store.upsert_scan_dir(ROOT, status="unavailable")
    t0 = time.perf_counter()
    assert await data.scan_dir_key(ROOT) == ROOT
    assert await data.scan_dir_key(ROOT + "/") == ROOT          # spelled differently
    assert time.perf_counter() - t0 < 0.5
    assert stalled_mount == []


@pytest.mark.asyncio
async def test_an_unlisted_folder_is_resolved_off_the_loop_and_bounded(store, monkeypatch):
    monkeypatch.setattr(data, "_SCAN_DIR_RESOLVE_S", 0.3)
    release = threading.Event()

    def _slow(self, *a, **k):
        release.wait(10)
        return self
    monkeypatch.setattr(pathlib.Path, "resolve", _slow)
    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1
    tk = asyncio.create_task(_tick())
    t0 = time.perf_counter()
    try:
        key = await data.scan_dir_key("/Volumes/Gone/other")
    finally:
        tk.cancel()
        release.set()
    assert key == "/Volumes/Gone/other"                           # the plain absolute path
    assert 0.25 < time.perf_counter() - t0 < 2
    assert ticks >= 5                                             # the loop kept running


@pytest.mark.asyncio
async def test_a_remote_url_passes_through(store, stalled_mount):
    assert await data.scan_dir_key("ftp://10.0.0.1/Music") == "ftp://10.0.0.1/Music"
    assert stalled_mount == []


@pytest.mark.asyncio
async def test_forgetting_and_unwatching_a_root_never_resolve_it(store, stalled_mount, monkeypatch):
    scanner._scan_queue.append((frozenset({ROOT, "/data/keep"}), None, None, False))
    try:
        scanner.forget_root(ROOT)
        assert scanner._scan_queue[-1][0] == frozenset({"/data/keep"})
    finally:
        scanner._scan_queue.pop()
    monkeypatch.setattr(watcher._state, "enabled", True)
    monkeypatch.setattr(watcher._state, "observer", object())
    monkeypatch.setattr(watcher._state, "roots", {ROOT})
    await watcher.remove_root(ROOT)
    assert ROOT not in watcher._state.roots
    assert stalled_mount == []


@pytest.mark.asyncio
async def test_a_big_root_is_purged_in_slices(store):
    h, other = path_hash(ROOT), path_hash("/data/keep")
    rows = [{"id": f"t{i}", "path": f"{ROOT if i % 2 else '/data/keep'}/A{i % 40}/{i}.flac",
             "title": f"Song {i}", "artist": f"Artist {i % 40}", "album": f"Album {i % 90}",
             "year": 1980 + i % 30, "duration": 60 + i % 200, "format": "flac",
             "scan_root_hash": h if i % 2 else other, "added_at": 1_700_000_000 + i}
            for i in range(8000)]
    store.upsert_tracks_batch(rows)
    store.rebuild_indexes()
    calls = []
    real = store.delete_track_ids

    def _spy(ids):
        calls.append((len(ids), store._batch_mode))
        return real(ids)
    store.delete_track_ids = _spy
    assert await data.delete_tracks_by_scan_root(ROOT) == 4000
    assert len(calls) > 1 and max(n for n, _ in calls) <= 100     # slices
    assert all(batch for _, batch in calls)                       # sorted lists rebuilt once
    assert not store.get_track_ids_for_scan_root(h)
    assert len(store.get_track_ids_for_scan_root(other)) == 4000
    assert store._batch_depth == 0 and not store._batch_mode
    assert store.verify_indexes()["index_ok"]


def _admin_app():
    import fastapi
    from soniqboom.api import admin, library
    app = fastapi.FastAPI()
    for mod in (library, admin):
        app.include_router(mod.router, prefix="/api")
    app.dependency_overrides[admin._require_token] = lambda: "t"
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["/api/admin/dirs", "/api/library/dirs"])
async def test_the_remove_request_answers_while_the_mount_hangs(
        store, tmp_data_dir, monkeypatch, url):
    import httpx
    from soniqboom.api import library
    monkeypatch.setenv("SONIQBOOM_CONF", str(tmp_data_dir / "SoniqBoom.conf"))
    app = _admin_app()
    app.dependency_overrides[library.require_admin] = lambda: None
    store.upsert_scan_dir(ROOT, status="unavailable")
    calls = []

    def _hang(self, *a, **k):                                     # a hung SMB mount
        calls.append(str(self))
        time.sleep(0.2)
        return self
    monkeypatch.setattr(pathlib.Path, "resolve", _hang)
    t0 = time.perf_counter()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.request("DELETE", url, json={"path": ROOT})
    assert r.status_code == 200, r.text
    assert time.perf_counter() - t0 < 2
    assert r.json()["dirs"] == [] and ROOT not in store._scan_dirs
    assert calls == [], calls[:10]


@pytest.mark.asyncio
async def test_a_root_added_again_while_purging_keeps_its_tracks(store, monkeypatch):
    from soniqboom.core import modland_catalog
    monkeypatch.setattr(modland_catalog, "_DELETE_STEP", 10)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)          # a slice per step
    h = path_hash(ROOT)
    store.upsert_tracks_batch([{"id": f"t{i}", "path": f"{ROOT}/{i}.flac", "title": f"S{i}",
                                "scan_root_hash": h, "added_at": 1_700_000_000 + i}
                               for i in range(200)])
    store.rebuild_indexes()
    real = store.delete_track_ids

    def _delete(ids):
        if len(store._tracks) <= 150:                             # re-added mid-purge
            store.upsert_scan_dir(ROOT)
        return real(ids)
    store.delete_track_ids = _delete
    deleted = await data.delete_tracks_by_scan_root(ROOT)
    assert deleted == 50 or deleted == 60
    assert len(store.get_track_ids_for_scan_root(h)) == 200 - deleted
    assert store._batch_depth == 0 and store.verify_indexes()["index_ok"]
