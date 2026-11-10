# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A removed music folder / share is gone for every listener at once
(``core/removals``): hidden from the lists, searches, auto-play, Subsonic
and playlists while it waits for its undo window, then dropped and purged;
Undo, re-adding it and a restart in between."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from soniqboom.core import availability, catalogue_view, removals
from soniqboom.core import store as store_mod
from soniqboom.core.data import list_scan_dirs, path_hash

ROOT = "/Volumes/Gone/Music - purchases"
KEEP = "/data/keep"


@pytest.fixture()
def lib(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("SONIQBOOM_CONF", str(tmp_data_dir / "SoniqBoom.conf"))
    s = store_mod.TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    removals.reset_for_tests()
    availability.reset_for_tests()
    for root in (ROOT, KEEP):
        s.upsert_scan_dir(root, status="unavailable" if root == ROOT else "ok")
    s.upsert_tracks_batch([
        {"id": f"{'g' if i % 2 else 'k'}{i}", "path": f"{ROOT if i % 2 else KEEP}/A/{i}.flac",
         "title": f"Song {i}", "artist": "Artist", "album": "Album", "duration": 100,
         "scan_root_hash": path_hash(ROOT if i % 2 else KEEP), "added_at": 1_700_000_000 + i}
        for i in range(40)])
    s.rebuild_indexes()
    yield s
    removals.reset_for_tests()
    availability.reset_for_tests()


def _gone(s):
    return set(s.get_track_ids_for_scan_root(path_hash(ROOT)))


@pytest.mark.asyncio
async def test_a_removed_folder_is_hidden_at_once(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    gone = _gone(lib)
    token0 = availability.token(lib)
    await removals.begin(ROOT)
    assert availability.hidden(lib) >= gone                       # lists, counts, searches
    assert availability.unplayable(lib) >= gone                   # auto-play
    assert availability.token(lib) != token0                      # clients re-read
    assert ROOT not in [d["path"] for d in await list_scan_dirs()]
    assert ROOT in lib._scan_dirs and _gone(lib) == gone           # nothing dropped yet
    view = catalogue_view.subsonic_store(lib)                      # Subsonic / DLNA
    assert view.track_count() == 20 and not any(t in view._tracks for t in gone)
    assert ROOT not in view._scan_dirs
    assert ROOT not in [r["path"] for r in availability.state(lib)["roots"]]   # no offline banner
    assert availability.offline_kinds(gone, lib)                   # (still offline inside: never played)
    [row] = removals.status(lib)
    assert row["state"] == "waiting" and row["total"] == 20 and row["undo_s"] > 0


@pytest.mark.asyncio
async def test_undo_puts_everything_back(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    gone = _gone(lib)
    await removals.begin(ROOT)
    assert removals.undo(ROOT)
    assert removals.hidden_ids()[0] is None and not removals.registry(lib)
    assert ROOT in [d["path"] for d in await list_scan_dirs()] and _gone(lib) == gone
    assert catalogue_view.subsonic_store(lib) is lib
    assert not removals.undo(ROOT)                                 # nothing left to undo


@pytest.mark.asyncio
async def test_after_the_undo_window_it_is_dropped_and_purged(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 0.05)
    await removals.begin(ROOT)
    for _ in range(100):
        if not removals.registry(lib):
            break
        await asyncio.sleep(0.05)
    assert not removals.registry(lib) and removals.hidden_ids()[0] is None
    assert ROOT not in lib._scan_dirs and not _gone(lib)
    assert len(lib.get_track_ids_for_scan_root(path_hash(KEEP))) == 20
    assert lib.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_a_restart_finishes_a_waiting_removal(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    gone = _gone(lib)
    await removals.begin(ROOT)
    removals.reset_for_tests()                                     # the process ends
    assert removals.registry(lib)                                  # (store config: kept)
    removals.resume(lib)                                           # the next start
    assert removals.hidden_ids()[0] >= gone                        # hidden again at once
    for _ in range(100):
        if not removals.registry(lib):
            break
        await asyncio.sleep(0.05)
    assert ROOT not in lib._scan_dirs and not _gone(lib)


@pytest.mark.asyncio
async def test_adding_the_folder_again_cancels_its_removal(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    await removals.begin(ROOT)
    removals.cancel(ROOT)
    assert not removals.registry(lib) and ROOT in lib._scan_dirs


@pytest.mark.asyncio
async def test_a_playlist_leaves_out_a_removed_folders_tracks(lib, monkeypatch):
    from soniqboom.api import playlist as pl
    monkeypatch.setattr(removals, "UNDO_S", 30.0)

    async def _get(_pid):
        return {"id": "p", "name": "P", "owner": "u", "track_ids": ["k0", "g1", "k2", "g3"]}
    monkeypatch.setattr(pl, "get_playlist", _get)
    monkeypatch.setattr(pl, "_can_read", lambda *_: True)
    monkeypatch.setattr(pl, "_can_write", lambda *_: True)
    monkeypatch.setattr(pl, "_shown", lambda p, _uid: p)
    await removals.begin(ROOT)
    out = await pl.read_playlist("p", uid="u")
    ids = [t["id"] if isinstance(t, dict) else t.id for t in out["tracks"]]
    assert ids == ["k0", "k2"]


def _app():
    import fastapi
    from soniqboom.api import admin, library
    app = fastapi.FastAPI()
    for mod in (library, admin):
        app.include_router(mod.router, prefix="/api")
    app.dependency_overrides[admin._require_token] = lambda: "t"
    return app


@pytest.mark.asyncio
async def test_remove_and_undo_through_the_admin_api(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as c:
        r = await c.request("DELETE", "/api/admin/dirs",
                            json={"path": ROOT, "purge_tracks": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["undo_s"] == 30.0 and [x["path"] for x in body["removing"]] == [ROOT]
        assert ROOT not in [d["path"] for d in body["dirs"]]
        listed = (await c.get("/api/admin/dirs")).json()
        assert [x["state"] for x in listed["removing"]] == ["waiting"]
        assert ROOT not in [d["path"] for d in (await c.get("/api/library/dirs")).json()["dirs"]]
        r = await c.post("/api/admin/dirs/undo", json={"path": ROOT})
        assert r.status_code == 200 and r.json()["removing"] == []
        assert ROOT in [d["path"] for d in r.json()["dirs"]]
        r = await c.post("/api/admin/dirs/undo", json={"path": ROOT})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_undo_is_refused_once_the_removal_runs(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    await removals.begin(ROOT)
    removals._purging.add(ROOT)                                    # (it has started)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as c:
        r = await c.post("/api/admin/dirs/undo", json={"path": ROOT})
    assert r.status_code == 409
    assert removals.status(lib)[0]["state"] == "removing"


@pytest.mark.asyncio
async def test_disconnecting_a_share_waits_then_drops_its_config(lib, tmp_data_dir, monkeypatch):
    from soniqboom.config import load_local_conf, save_local_conf
    share_root = "ftp://10.0.0.1/Music"
    lib.upsert_scan_dir(share_root, network_share_id="ftp-x")
    conf = load_local_conf()
    conf["network_shares"] = {"ftp-x": {"protocol": "ftp", "host": "10.0.0.1",
                                        "remote_path": "/Music"}}
    save_local_conf(conf)
    from soniqboom.api import admin
    monkeypatch.setattr(admin, "_scan_root_for_share", lambda _s: share_root)
    monkeypatch.setattr(removals, "UNDO_S", 0.05)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),
                                 base_url="http://t") as c:
        r = await c.request("DELETE", "/api/admin/shares",
                            json={"id": "ftp-x", "purge_tracks": True})
    assert r.status_code == 200, r.text
    assert r.json()["removing"][0]["share"] is True
    assert "ftp-x" in load_local_conf().get("network_shares", {})   # (waits for undo)
    for _ in range(100):
        if not removals.registry(lib):
            break
        await asyncio.sleep(0.05)
    assert "ftp-x" not in load_local_conf().get("network_shares", {})
    assert share_root not in lib._scan_dirs


async def _settle(lib):
    for _ in range(200):
        if not removals.registry(lib) or any(r.get("error") for r in removals.registry(lib).values()):
            return
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_a_shutdown_mid_purge_keeps_the_removal_for_the_next_start(lib, monkeypatch):
    from soniqboom.core import modland_catalog
    monkeypatch.setattr(modland_catalog, "_DELETE_STEP", 2)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    real = lib.delete_track_ids
    started = asyncio.Event()

    def _slow(ids):
        started.set()
        return real(ids)
    lib.delete_track_ids = _slow
    await removals.begin(ROOT)
    await asyncio.wait_for(started.wait(), 2)
    for t in removals.tasks():
        t.cancel()                                                 # the shutdown
    await asyncio.sleep(0.05)
    assert ROOT in removals.registry(lib)                          # kept
    assert 0 < len(_gone(lib)) < 20
    removals.reset_for_tests()
    removals.resume(lib)                                           # the next start
    assert removals.hidden_ids()[0] >= _gone(lib)                  # hidden again at once
    await _settle(lib)
    assert not removals.registry(lib) and not _gone(lib)
    assert lib._batch_depth == 0 and lib.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_a_share_at_a_servers_root_is_hidden_from_subsonic(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    root = "ftp://10.0.0.1/"
    lib.upsert_scan_dir(root, network_share_id="ftp-root")
    lib.upsert_tracks_batch([{"id": f"f{i}", "path": f"{root}A/{i}.mod", "title": f"F{i}",
                              "scan_root_hash": path_hash(root), "added_at": 1} for i in range(5)])
    await removals.begin(root, share_id="ftp-root")
    view = catalogue_view.subsonic_store(lib)
    assert not any(f"f{i}" in view._tracks for i in range(5))
    assert root not in view._scan_dirs and view.track_count() == 40


@pytest.mark.asyncio
async def test_the_players_next_page_skips_a_removed_folder(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    rows = [{"id": tid} for tid in ["k0", "g1", "k2", "g3"]]
    await removals.begin(ROOT)
    page = availability.player_page(lambda off, n: rows[off:off + n], 0, 4, lib)
    assert [r["id"] for r in page["tracks"]] == ["k0", "k2"]


@pytest.mark.asyncio
async def test_a_smart_playlist_leaves_out_a_removed_folders_tracks(lib, monkeypatch):
    from soniqboom.api import playlist as pl
    from soniqboom.api import search
    monkeypatch.setattr(removals, "UNDO_S", 30.0)

    async def _get(_pid):
        return {"id": "p", "name": "P", "owner": "u", "query": "song"}

    async def _run(q, limit=500, uid=None):
        return [lib.get_track(t) for t in ("k0", "g1", "k2")]
    monkeypatch.setattr(pl, "get_playlist", _get)
    monkeypatch.setattr(pl, "_can_read", lambda *_: True)
    monkeypatch.setattr(pl, "_can_write", lambda *_: True)
    monkeypatch.setattr(pl, "_shown", lambda p, _uid: p)
    monkeypatch.setattr(search, "run_search", _run)
    await removals.begin(ROOT)
    out = await pl.read_playlist("p", uid="u")
    assert [t["id"] for t in out["tracks"]] == ["k0", "k2"]


@pytest.mark.asyncio
async def test_a_track_added_while_the_removal_waits_is_hidden_too(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    await removals.begin(ROOT)
    lib.upsert_tracks_batch([{"id": "late", "path": f"{ROOT}/B/late.flac", "title": "Late",
                              "scan_root_hash": path_hash(ROOT), "added_at": 2}])
    assert "late" in removals.hidden_ids()[0]
    assert "late" in availability.hidden(lib)


@pytest.mark.asyncio
async def test_a_scan_listing_the_root_again_does_not_stop_the_purge(lib, monkeypatch):
    from soniqboom.core import modland_catalog
    monkeypatch.setattr(modland_catalog, "_DELETE_STEP", 2)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    real = lib.delete_track_ids
    calls = []

    def _delete(ids):
        calls.append(1)
        if len(calls) == 3:
            lib.upsert_scan_dir(ROOT)                              # a scan ending meanwhile
        return real(ids)
    lib.delete_track_ids = _delete
    await removals.begin(ROOT)
    await _settle(lib)
    assert not _gone(lib) and ROOT not in lib._scan_dirs


@pytest.mark.asyncio
async def test_adding_the_folder_again_mid_purge_stops_it(lib, monkeypatch):
    from soniqboom.core import modland_catalog
    monkeypatch.setattr(modland_catalog, "_DELETE_STEP", 2)
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    real = lib.delete_track_ids
    calls = []

    def _delete(ids):
        calls.append(1)
        if len(calls) == 3:
            removals.cancel(ROOT)                                  # POST /admin/dirs again
            lib.upsert_scan_dir(ROOT)
        return real(ids)
    lib.delete_track_ids = _delete
    await removals.begin(ROOT)
    await _settle(lib)
    assert not removals.registry(lib) and ROOT in lib._scan_dirs
    assert len(_gone(lib)) == 20 - 2 * 3                          # the rest kept for its scan
    assert removals.hidden_ids()[0] is None


@pytest.mark.asyncio
async def test_removing_the_same_folder_twice_keeps_the_first_removal(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    first = await removals.begin(ROOT)
    task = removals._tasks[ROOT]
    assert await removals.begin(ROOT) == first and removals._tasks[ROOT] is task


@pytest.mark.asyncio
async def test_a_failed_removal_is_kept_and_shown(lib, monkeypatch):
    monkeypatch.setattr(removals, "UNDO_S", 0.0)

    async def _boom(_path):
        raise OSError("disk full")
    monkeypatch.setattr(removals, "_drop_folder", _boom)
    await removals.begin(ROOT)
    await _settle(lib)
    [row] = removals.status(lib)
    assert row["state"] == "failed" and "disk full" in row["error"]
    assert removals.hidden_ids()[0]                                # still hidden


@pytest.mark.asyncio
async def test_editing_a_playlist_keeps_the_hidden_entries(lib, monkeypatch):
    from soniqboom.api import playlist as pl
    monkeypatch.setattr(removals, "UNDO_S", 30.0)
    saved = {}

    async def _get(_pid):
        return {"id": "p", "name": "P", "owner": "u", "track_ids": ["k0", "g1", "k2", "g3"]}

    async def _update(_pid, updates):
        saved.update(updates)
        return {"id": "p", **updates}
    monkeypatch.setattr(pl, "get_playlist", _get)
    monkeypatch.setattr(pl, "update_playlist", _update)
    monkeypatch.setattr(pl, "_can_read", lambda *_: True)
    monkeypatch.setattr(pl, "_can_write", lambda *_: True)
    monkeypatch.setattr(pl, "_shown", lambda p, _uid: p)
    await removals.begin(ROOT)
    body = pl.PlaylistUpdate(track_ids=["k2", "k0"])               # reordered, as listed
    await pl.update_existing_playlist("p", body, uid="u", user=None)
    assert saved["track_ids"] == ["k2", "k0", "g1", "g3"]


def test_the_availability_state_names_the_removal_version(lib):
    v0 = availability.state(lib)["removing"]
    asyncio.run(_begin_in_loop(lib))
    assert availability.state(lib)["removing"] != v0


async def _begin_in_loop(lib):
    import soniqboom.core.removals as r
    r.UNDO_S, keep = 30.0, r.UNDO_S
    try:
        await r.begin(ROOT)
    finally:
        r.UNDO_S = keep
