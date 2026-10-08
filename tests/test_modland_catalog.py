# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Modland catalogue (core/modland_catalog.py, core/catalogue.py).

Built from a fixture subset of Modland's ``allmods.txt`` index and served by
a local FTP server (``_ftp_server``) — never the network.  Covers: index
parsing and the music / companion / foreign-format classification, the scope
presets, the build (fields, stable ids), the delta of a refresh, switching
off and on (ids and user data back), Subsonic / DLNA visibility, the
zero-fetch invariant over every non-play reader, the first-play metadata
merge, companions, prewarm budget, rate limit and an offline Modland.
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
import uuid
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402

from soniqboom.core import catalogue, remote_cache  # noqa: E402
from soniqboom.core import modland_catalog as mc  # noqa: E402
from soniqboom.core import store as store_mod  # noqa: E402
from soniqboom.core.store import TrackStore  # noqa: E402

ROOT = mc.ROOT


# ── fixture files ────────────────────────────────────────────────────────────

def _mod_bytes(title: bytes = b"tiny test song") -> bytes:
    """A minimal 4-channel ProTracker module (7.68 s at the default speed)."""
    head = title.ljust(20, b"\x00")
    smp = b"lead".ljust(22, b"\x00") + struct.pack(">HBBHH", 16, 0, 64, 0, 1)
    head += smp + (b"\x00" * 30) * 30
    head += bytes([1, 127]) + bytes([0]) + b"\x00" * 127 + b"M.K."
    pat = bytearray(1024)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, 0x00])
    return bytes(head) + bytes(pat) + bytes(32)


def _psid_bytes(songs: int = 1, start: int = 1) -> bytes:
    h = bytearray(0x7C)
    h[0:4] = b"PSID"
    struct.pack_into(">HHHHHHH", h, 4, 2, 0x7C, 0, 0x1000, 0x1003, songs, start)
    h[0x16:0x16 + 8] = b"Commando"
    h[0x36:0x36 + 11] = b"Rob Hubbard"
    h[0x56:0x56 + 4] = b"1985"
    return bytes(h) + b"\x60" * 64


# rel → bytes served by the fake Modland (sizes in the index match).
SERVED = {
    "Protracker/Test Artist/tiny song.mod": _mod_bytes(),
    "Protracker/Test Artist/other song.mod": _mod_bytes(b"other song"),
    "Protracker/- unknown/nameless.mod": _mod_bytes(b""),
    "TFMX/Chris Huelsbeck/mdat.turrican 2-intro": b"TFMX-SONG " + bytes(range(256)) * 8,
    "TFMX/Chris Huelsbeck/smpl.turrican 2-intro": bytes(range(256)) * 32,
    "TFMX/Chris Huelsbeck/mdat.turrican 2-level 1": b"TFMX-SONG " + bytes(range(255)) * 8,
    "TFMX/Chris Huelsbeck/smpl.turrican 2-level 1": bytes(range(255)) * 32,
    "Playstation Sound Format/Some Composer/Some Game/01 opening.minipsf":
        b"PSF\x01" + b"\x00" * 12 + b"[TAG]_lib=some game.psflib\n",
    "Playstation Sound Format/Some Composer/Some Game/02 stage.minipsf":
        b"PSF\x01" + b"\x00" * 12 + b"[TAG]_lib=some game.psflib\n",
    "Playstation Sound Format/Some Composer/Some Game/some game.psflib":
        b"PSF\x01" + b"\x00" * 4000,
    "HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid": _psid_bytes(),
    "Fasttracker 2/Someone/big tune.xm": b"Extended Module: big" + b"\x00" * 400,
    "Sidplayer/Someone/stray.mus": b"\x00\x01" * 100,
    "Protracker/Robyn Powell/Robyn Powell.zip": b"PK\x05\x06" + b"\x00" * 18,
    "Protracker/Test Artist/readme.txt": b"hello",
    "Protracker/Test Artist/cover.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 200,
}


class _Hvsc:
    """A configured HVSC (the apply then really runs over SID tracks)."""
    def is_configured(self):
        return True

    def reload(self):
        pass

    def lookup_durations_by_md5(self, md5):
        return [123.0]

    def lookup_stil(self, path):
        return None

    def stil_key_for(self, path):
        return None

    def lookup_stil_by_relpath(self, key):
        return None


def _index_text(files: dict) -> str:
    return "".join(f"{len(data)}\t{rel}\n" for rel, data in files.items())


def _zip(path: Path, files: dict) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("allmods.txt", _index_text(files))
    return path


def _drop_ftp_pools(port: int) -> None:
    from soniqboom.core import filesource
    with filesource._FTP_POOLS_LOCK:
        keys = [k for k in filesource._FTP_POOLS if k[1] == port]
        pools = [filesource._FTP_POOLS.pop(k) for k in keys]
    for p in pools:
        p.close_all()


@pytest.fixture
def env(tmp_data_dir, tmp_path, monkeypatch):
    """A fresh store, remote cache, conversion cache and a local "Modland"."""
    from soniqboom.config import settings
    from soniqboom.api import stream
    store = TrackStore()
    monkeypatch.setattr(store_mod, "_store", store)
    cache = remote_cache.RemoteCache(tmp_data_dir / "remote-cache", 512)
    monkeypatch.setattr(remote_cache, "_cache", cache)
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    ftp_root = tmp_path / "ftp"
    for rel, data in SERVED.items():
        p = ftp_root / "pub" / "modules" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    srv = FtpServer(ftp_root).start()
    monkeypatch.setattr(mc, "CONNECT_HOST", "127.0.0.1")
    monkeypatch.setattr(mc, "CONNECT_PORT", srv.port)
    monkeypatch.setattr(mc, "_MIN_INDEX_ROWS", 1)
    monkeypatch.setattr(mc, "_MIN_MUSIC_ROWS", 1)
    monkeypatch.setattr(mc, "_db_gen", mc._db_gen + 1)
    monkeypatch.setattr(mc, "_siblings", None)
    monkeypatch.setattr(mc, "_status", dict(mc._status))
    monkeypatch.setattr(mc, "_job_lock", None)
    monkeypatch.setattr(mc, "_refresh_task", None)
    zpath = _zip(tmp_path / "allmods.zip", SERVED)
    yield {"store": store, "cache": cache, "srv": srv, "zip": zpath, "tmp": tmp_path,
           "ftp_root": ftp_root}
    for t in list(mc._tasks):
        t.cancel()
    from soniqboom.core.filesource import remove_source
    remove_source(ROOT)
    _drop_ftp_pools(srv.port)
    srv.stop()
    if cache._dl_pool is not None:
        cache._dl_pool.shutdown(wait=False, cancel_futures=True)


def _tid(rel: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ROOT}:/{rel}"))


async def _enable(env, scope: str = "standard") -> dict:
    env["store"].set_config(mc.CONFIG_SCOPE, scope)
    st = await mc.enable(local_zip=env["zip"])
    for t in list(mc._tasks):              # the refresh loop sleeps a day
        if "refresh-loop" in (t.get_name() or ""):
            t.cancel()
    return st


def _retrs(env) -> list:
    return [p for p, _off in env["srv"].retrs]


# ── index, classification, scope ─────────────────────────────────────────────

def test_index_classifies_music_companions_archives_and_foreign_formats(tmp_path, tmp_data_dir,
                                                                         monkeypatch):
    monkeypatch.setattr(mc, "_MIN_INDEX_ROWS", 1)
    monkeypatch.setattr(mc, "_MIN_MUSIC_ROWS", 1)
    out = tmp_path / "idx.sqlite"
    info = mc.build_index_db(_zip(tmp_path / "a.zip", SERVED), out)
    assert info["rows"] == len(SERVED)
    monkeypatch.setattr(mc, "db_path", lambda: out)
    monkeypatch.setattr(mc, "_db_gen", mc._db_gen + 1)
    kinds = {rel: mc.entry(rel)[1] for rel in SERVED}
    assert kinds["Protracker/Test Artist/tiny song.mod"] == mc.KIND_MUSIC
    assert kinds["TFMX/Chris Huelsbeck/mdat.turrican 2-intro"] == mc.KIND_MUSIC
    assert kinds["TFMX/Chris Huelsbeck/smpl.turrican 2-intro"] == mc.KIND_OTHER   # companion half
    assert kinds["Playstation Sound Format/Some Composer/Some Game/some game.psflib"] == mc.KIND_OTHER
    assert kinds["Sidplayer/Someone/stray.mus"] == mc.KIND_OTHER                  # C64, not uade UFO
    assert kinds["Protracker/Robyn Powell/Robyn Powell.zip"] == mc.KIND_ARCHIVE
    assert kinds["Protracker/Test Artist/readme.txt"] == mc.KIND_OTHER
    assert kinds["HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid"] == mc.KIND_MUSIC
    std = {r for r, *_ in mc._wanted_rows(mc.SCOPE_STANDARD)}
    every = {r for r, *_ in mc._wanted_rows(mc.SCOPE_ALL)}
    assert "Fasttracker 2/Someone/big tune.xm" in every - std
    assert std <= every and len(every - std) == 1
    # Listing / stat answer from the index (paths root-relative, slash first).
    names = {e.name: e for e in mc.list_dir("TFMX/Chris Huelsbeck")}
    assert names["smpl.turrican 2-intro"].path == "/TFMX/Chris Huelsbeck/smpl.turrican 2-intro"
    assert {e.name for e in mc.list_dir("") if e.is_dir} >= {"Protracker", "TFMX", "HVSC"}


def test_bad_lines_are_skipped_and_an_implausible_index_is_refused(tmp_path, tmp_data_dir):
    lines = "12\tok/fine.mod\nnot-a-size\tx.mod\n5\t../escape.mod\n7\tbad\rname.mod\n"
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("allmods.txt", lines)
    with pytest.raises(ValueError, match="implausible"):
        mc.build_index_db(z, tmp_path / "x.sqlite")          # real thresholds: far too small
    assert not (tmp_path / "x.sqlite").exists()


def test_format_title_and_credit_derivation():
    assert mc.format_for("HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid") == "SID"
    assert mc.format_for("RealSID/Someone/x.rsid") == "SID"
    assert mc.format_for("Protracker/A/b.mod") == "ProTracker"
    assert mc.format_for("Dreamcast Sound Format/A/G/x.dsf") == "DSF (Dreamcast)"
    assert mc.format_for("Ad Lib/Apogee/A/x.imf") == "AdLib IMF"
    assert mc.title_for("TFMX/Chris Huelsbeck/mdat.turrican 2-intro") == "turrican 2-intro"
    assert mc.title_for("HVSC/MUSICIANS/H/Hubbard_Rob/Thing_on_a_Spring.sid") == "Thing on a Spring"
    assert mc.title_for("Protracker/A/space debris.mod") == "space debris"
    assert mc.credit_for("Protracker/- unknown/x.mod", "ProTracker") == ""
    assert mc.credit_for("Protracker/4-Mat/x.mod", "ProTracker") == "4-Mat"
    assert mc.credit_for("HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid", "SID") == "Hubbard Rob"
    assert mc.credit_for("HVSC/GAMES/A-F/Commando.sid", "SID") == ""


# ── build, fields, stable ids ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_lists_playable_files_with_path_derived_fields(env):
    st = await _enable(env)
    assert st["state"] == "ready", st
    store = env["store"]
    ids = store.get_track_ids_for_scan_root(mc.root_hash())
    expect = {"Protracker/Test Artist/tiny song.mod", "Protracker/Test Artist/other song.mod",
              "Protracker/- unknown/nameless.mod",
              "TFMX/Chris Huelsbeck/mdat.turrican 2-intro",
              "TFMX/Chris Huelsbeck/mdat.turrican 2-level 1",
              "Playstation Sound Format/Some Composer/Some Game/01 opening.minipsf",
              "Playstation Sound Format/Some Composer/Some Game/02 stage.minipsf",
              "HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid"}
    assert ids == {_tid(r) for r in expect}                 # FT2 out (scope), no companions
    t = store.get_track(_tid("Protracker/Test Artist/tiny song.mod"))
    assert t["path"] == f"{ROOT}:/Protracker/Test Artist/tiny song.mod"
    assert t["title"] == "tiny song" and t["artist"] == "Test Artist"
    assert t["format"] == "ProTracker" and t["file_size"] == len(SERVED["Protracker/Test Artist/tiny song.mod"])
    assert t["added_at"] == 1 and not t.get("duration") and not t.get("mtime")
    assert t["scene_path"] == "Protracker/Test Artist/tiny song.mod"
    psf = store.get_track(_tid("Playstation Sound Format/Some Composer/Some Game/01 opening.minipsf"))
    assert psf["album"] == "Some Game" and psf["album_source"] == "modland"
    assert "Game" in (psf.get("genre_derived") or []) and "PlayStation" in psf["genre_derived"]
    tfmx = store.get_track(_tid("TFMX/Chris Huelsbeck/mdat.turrican 2-intro"))
    assert tfmx["title"] == "turrican 2-intro" and tfmx["artist"] == "Chris Huelsbeck"
    assert store.get_track(_tid("Protracker/- unknown/nameless.mod")).get("artist", "") == ""
    sd = {d["path"]: d for d in store.list_scan_dirs()}
    assert sd[ROOT]["catalogue"] == "modland" and sd[ROOT]["label"] == "Modland"
    assert sd[ROOT]["track_count"] == len(expect)
    assert _retrs(env) == []                                # the build fetched nothing


@pytest.mark.asyncio
async def test_scope_change_and_refresh_delta(env, monkeypatch):
    await _enable(env)
    store = env["store"]
    ft2 = _tid("Fasttracker 2/Someone/big tune.xm")
    assert store.get_track(ft2) is None
    st = await mc.set_scope(mc.SCOPE_ALL)
    assert st["last_result"]["added"] == 1 and store.get_track(ft2) is not None
    assert store.get_track(ft2)["added_at"] == 1        # listed from the start: not "new"
    # Upstream: one file added, one removed, one resized.
    files = dict(SERVED)
    files["Protracker/Test Artist/new one.mod"] = _mod_bytes(b"new one")
    del files["Protracker/Test Artist/other song.mod"]
    files["Protracker/Test Artist/tiny song.mod"] = _mod_bytes() + b"\x00" * 10
    z2 = _zip(env["tmp"] / "allmods2.zip", files)
    tiny = _tid("Protracker/Test Artist/tiny song.mod")
    store.update_track_fields(tiny, {"user_edited": ["title"], "title": "My Title"})
    st = await mc.refresh(local_zip=z2)
    res = st["last_result"]
    assert (res["added"], res["removed"], res["changed"]) == (1, 1, 1), res
    assert store.get_track(_tid("Protracker/Test Artist/other song.mod")) is None
    new = store.get_track(_tid("Protracker/Test Artist/new one.mod"))
    assert new is not None and new["added_at"] > 1          # appeared after the first build
    t = store.get_track(tiny)
    assert t["file_size"] == len(files["Protracker/Test Artist/tiny song.mod"])
    assert t["title"] == "My Title"                          # a user edit survives the rewrite
    assert _retrs(env) == []


@pytest.mark.asyncio
async def test_switch_off_and_on_brings_ids_and_user_data_back(env):
    await _enable(env)
    store = env["store"]
    tiny = _tid("Protracker/Test Artist/tiny song.mod")
    store.set_rating(tiny, 5)
    store.record_play(tiny)
    pl = store.create_playlist("pl-cat", "cat", [tiny])
    from soniqboom.core.subsonic_state import get_state
    st = get_state()
    await st.star("u1", "song", [tiny])
    before = set(store.get_track_ids_for_scan_root(mc.root_hash()))
    off = await mc.disable()
    assert off["enabled"] is False and off["tunes"] == 0
    assert store.get_track(tiny) is None
    assert ROOT not in {d["path"] for d in store.list_scan_dirs()}
    assert not catalogue.is_catalogue_root(ROOT)
    on = await _enable(env)
    assert on["state"] == "ready"
    assert set(store.get_track_ids_for_scan_root(mc.root_hash())) == before
    assert store.get_rating(tiny) == 5
    assert store.get_play_stats(tiny).get("count", 0) >= 1
    assert tiny in st.starred("u1", "song")
    assert tiny in [e if isinstance(e, str) else e.get("id")
                    for e in store.get_playlist(pl["id"])["track_ids"]]


@pytest.mark.asyncio
async def test_a_share_on_the_same_root_blocks_the_catalogue(env, monkeypatch):
    monkeypatch.setattr(mc, "conflicting_share", lambda: "modland-share")
    st = await mc.enable(local_zip=env["zip"])
    assert "remove that share first" in (st["error"] or "")
    assert not env["store"].get_track_ids_for_scan_root(mc.root_hash())


# ── Subsonic / DLNA visibility ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subsonic_view_hides_the_catalogue_until_switched_on(env):
    await _enable(env)
    from soniqboom.core.catalogue_view import subsonic_store
    store = env["store"]
    mine = {"id": "local-1", "path": "/music/a.mod", "title": "tiny local", "artist": "Test Artist",
            "format": "ProTracker", "added_at": 5, "dir_hash": "d", "scan_root_hash": "r",
            "genre": ["Tracker"]}
    store.upsert_track(mine)
    tiny = _tid("Protracker/Test Artist/tiny song.mod")
    view = subsonic_store(store)
    assert view is not store
    assert view.get_track(tiny) is None and view.get_track("local-1") is not None
    assert [t["id"] for t in view.filter_tracks(query="tiny", limit=50)] == ["local-1"]
    assert tiny not in view.filter_track_ids(query="tiny")
    assert view._tracks.get(tiny) is None and "local-1" in view._tracks
    assert all(r["path"] != ROOT for r in view.list_scan_dirs())
    assert view._tag_artist.get("test artist") == {"local-1"}
    assert view.track_count() == 1
    genres = {r["genre"]: r["count"] for r in view.aggregate_genres()}
    assert "PlayStation" not in genres
    mc.set_subsonic(True)
    assert subsonic_store(store) is store
    mc.set_subsonic(False)
    assert subsonic_store(store) is not store


@pytest.mark.asyncio
async def test_subsonic_api_refuses_a_hidden_catalogue_id(env, monkeypatch):
    await _enable(env)
    from soniqboom.api import subsonic
    tiny = _tid("Protracker/Test Artist/tiny song.mod")
    store = subsonic.get_store()
    assert store.get_track(tiny) is None                    # stream / download → "Song not found"
    folders = subsonic._music_folders(store)
    assert all(f["name"] != "Modland" for f in folders)
    mc.set_subsonic(True)
    store = subsonic.get_store()
    assert store.get_track(tiny) is not None
    assert any(f["name"] == "Modland" for f in subsonic._music_folders(store))


# ── the zero-fetch invariant ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_reader_but_a_play_fetches_a_catalogue_file(env, monkeypatch):
    """Every background / non-play reader of remote bytes, driven over
    uncached catalogue tunes: no RETR reaches the server and the catalogue's
    file source records no network read."""
    import httpx
    from fastapi import FastAPI
    from soniqboom.api import art, fstree, stream, tracks as tracks_api
    from soniqboom.core import (art_backfill, cast_render, hvsc_apply, repair, scanner,
                                source_bytes, remote_freshness, duplicates)
    from soniqboom.core.filesource import find_source_for_path, get_source
    await _enable(env)
    store = env["store"]
    rels = ["Protracker/Test Artist/tiny song.mod", "TFMX/Chris Huelsbeck/mdat.turrican 2-intro",
            "Playstation Sound Format/Some Composer/Some Game/01 opening.minipsf",
            "HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid"]
    tids = [_tid(r) for r in rels]
    paths = [f"{ROOT}:/{r}" for r in rels]
    catalogue.reset_stats()

    async def _noauth(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    app = FastAPI()
    for r in (stream.router, tracks_api.router, fstree.router, art.router):
        app.include_router(r, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/api/stream/probe-durations", json={"track_ids": tids})
        assert r.status_code == 200 and r.json() == {}
        for tid in tids:
            # (``/extended`` is left out: Track Info opening on a tune is a
            # listener's intent and reads that one file — tests/
            # test_catalogue_parity.py.)
            for url in (f"/api/tracks/{tid}/waveform", f"/api/tracks/{tid}/vu",
                        f"/api/tracks/{tid}/patterns", f"/api/tracks/{tid}/sid",
                        f"/api/art/{tid}?size=sm",
                        f"/api/tracks/{tid}/lyrics", f"/api/tracks/{tid}/chapters"):
                await c.get(url)
            await c.post(f"/api/stream/{tid}/prewarm?priority=ahead&pw=p1")
        # Folder browsing: children and listings come from RAM / the index.
        r = await c.get("/api/fstree/children", params={"path": ROOT, "root": ROOT})
        assert {ch["name"] for ch in r.json()["children"]} >= {"Protracker", "TFMX", "HVSC"}
        r = await c.get("/api/fstree/tracks-with-meta",
                        params={"path": f"{ROOT}/Protracker", "recursive": "true",
                                "offset": 0, "limit": 50})
        body = r.json()
        assert body["total"] == 3 and body["tracks"][0]["title"]
        assert all(t["path"].startswith(ROOT + ":/Protracker/") for t in body["tracks"])
        r = await c.get("/api/fstree/tracks-with-meta",
                        params={"path": f"{ROOT}/TFMX/Chris Huelsbeck", "recursive": "false",
                                "offset": 0, "limit": 50})
        assert r.json()["total"] == 2
        r = await c.get("/api/fstree/tracks-with-meta",
                        params={"path": ROOT, "recursive": "true", "offset": 0, "limit": 5,
                                "shuffle_seed": 7})
        assert r.json()["total"] == 8 and len(r.json()["tracks"]) == 5
    for p in paths:
        assert source_bytes.read_source_bytes(p, lane="stream") is None
        assert source_bytes.read_source_bytes(p, lane="scan") is None
        assert await cast_render.materialize_source(p, "x", lane="scan") is None
    art_backfill.request_backfill(type("T", (), {"id": tids[0], "path": paths[0],
                                                  "cover_art": "/api/art/x", "file_size": 9})())
    monkeypatch.setattr(repair, "start_repair", repair.start_repair)
    assert await repair.start_repair([store.get_track(t) for t in tids], kind="repair")
    for _ in range(100):
        if not repair._busy():
            break
        await asyncio.sleep(0.05)
    assert repair._progress.total == 0                   # catalogue tunes never queued
    # ... and the repair's own remote re-read refuses one if it ever got one.
    ok, applied, err = await repair._process_remote(store.get_track(tids[0]), None)
    assert not applied
    monkeypatch.setattr("soniqboom.core.hvsc.get_hvsc", lambda: _Hvsc())
    res = await hvsc_apply.apply_hvsc_to_library(ids=set(tids))
    assert res.get("updated", 0) == 0
    await hvsc_apply.apply_hvsc_to_library()
    # Scans, freshness polls and walks refuse a catalogue root.
    src = get_source(ROOT)
    assert await scanner.start_remote_scan("x", ROOT, src) == {}
    await scanner.start_scan([ROOT])
    assert await remote_freshness.check_now(ROOT) == {}
    assert await remote_freshness.check_now(f"{ROOT}/Protracker") == {}
    assert await remote_freshness._poll_share(ROOT, reason="test") == {}
    monkeypatch.setattr(remote_freshness._reg, "enabled", True)
    await remote_freshness.add_share(ROOT)                 # never armed
    assert ROOT not in remote_freshness._reg.tasks
    with pytest.raises(catalogue.CatalogueFetchRefused):
        list(src.walk_with_stat("/"))
    assert src.is_dir("/") and src.is_dir("/TFMX") and not src.is_dir("/Nope")
    assert src.stat("/Protracker/Test Artist/tiny song.mod").size == len(
        SERVED["Protracker/Test Artist/tiny song.mod"])
    assert find_source_for_path(f"{ROOT}/TFMX")[2] is src
    # Every reader above skipped on its own guard — none even reached the
    # source's refusal backstop (but the repair's direct re-read above, which
    # the backstop refused: exactly one).
    assert catalogue.stats()["refused"] == 1, catalogue.stats()["recent_refused"]
    # A direct read outside a play's window is refused by the source itself.
    with pytest.raises(catalogue.CatalogueFetchRefused):
        src.read_file("/Protracker/Test Artist/tiny song.mod")
    with pytest.raises(catalogue.CatalogueFetchRefused):
        src.open_stream("/Protracker/Test Artist/tiny song.mod")
    assert duplicates.group_key_for(store.get_track(tids[0])) == ""
    await asyncio.sleep(0.2)
    assert _retrs(env) == [], env["srv"].retrs
    assert catalogue.stats()["reads"] == 0


# ── plays: fetch, companions, merge ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_play_fetches_one_file_and_merges_its_metadata(env, have_openmpt123):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)
    store = env["store"]
    rel = "Protracker/Test Artist/tiny song.mod"
    tid = _tid(rel)
    track = await get_track(tid)
    path, ext, _named, pin = await stream._resolve_play_source(tid, track)
    if pin:
        stream._zip_unpin(pin)
    assert Path(path).read_bytes() == SERVED[rel]
    assert _retrs(env) == [f"/pub/modules/{rel}"]
    for _ in range(100):                                     # the merge runs in the background
        if (store.get_track(tid).get("mtime") or 0) > 0:
            break
        await asyncio.sleep(0.05)
    t = store.get_track(tid)
    assert t["mtime"] > 0 and t["file_md5"]                  # merged
    assert t["id"] == tid and t["path"] == f"{ROOT}:/{rel}"
    assert t["artist"] == "Test Artist"                      # the catalogue credit stays
    if have_openmpt123:
        assert t["duration"] > 5
    # A replay is a cache hit: no second RETR.
    track = await get_track(tid)
    await stream._resolve_play_source(tid, track)
    assert _retrs(env) == [f"/pub/modules/{rel}"]
    # Now cached, the background readers may read it (from the cache).
    from soniqboom.core import source_bytes
    assert source_bytes.read_source_bytes(t["path"], lane="scan") == SERVED[rel]
    assert len(_retrs(env)) == 1


@pytest.mark.asyncio
async def test_a_tfmx_play_fetches_the_sample_half_too(env):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)
    rel = "TFMX/Chris Huelsbeck/mdat.turrican 2-intro"
    track = await get_track(_tid(rel))
    path, _ext, named, pin = await stream._resolve_play_source(_tid(rel), track)
    try:
        assert named
        assert Path(path).name == "mdat.turrican 2-intro"
        assert (Path(path).parent / "smpl.turrican 2-intro").read_bytes() == SERVED[
            "TFMX/Chris Huelsbeck/smpl.turrican 2-intro"]
    finally:
        if pin:
            stream._zip_unpin(pin)
    got = sorted(_retrs(env))
    assert got == sorted([f"/pub/modules/{rel}",
                          "/pub/modules/TFMX/Chris Huelsbeck/smpl.turrican 2-intro"]), got


@pytest.mark.asyncio
async def test_a_minipsf_play_fetches_its_psflib(env):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)
    rel = "Playstation Sound Format/Some Composer/Some Game/01 opening.minipsf"
    lib = "Playstation Sound Format/Some Composer/Some Game/some game.psflib"
    track = await get_track(_tid(rel))
    path, _ext, _named, pin = await stream._resolve_play_source(_tid(rel), track)
    try:
        assert (Path(path).parent / "some game.psflib").read_bytes() == SERVED[lib]
    finally:
        if pin:
            stream._zip_unpin(pin)
    assert sorted(_retrs(env)) == sorted([f"/pub/modules/{rel}", f"/pub/modules/{lib}"])
    # The second tune of the game shares the cached lib: one more RETR only.
    rel2 = "Playstation Sound Format/Some Composer/Some Game/02 stage.minipsf"
    track = await get_track(_tid(rel2))
    _p, _e, _n, pin = await stream._resolve_play_source(_tid(rel2), track)
    if pin:
        stream._zip_unpin(pin)
    assert len(_retrs(env)) == 3 and _retrs(env)[-1] == f"/pub/modules/{rel2}"


@pytest.mark.asyncio
async def test_prewarm_budget_is_one_track_per_listener(env):
    await _enable(env)
    a = f"{ROOT}:/Protracker/Test Artist/tiny song.mod"
    b = f"{ROOT}:/Protracker/Test Artist/other song.mod"
    root, ra = ROOT, "Protracker/Test Artist/tiny song.mod"
    assert catalogue.permit_prewarm(a, "listener-1")
    assert catalogue.allowed(root, ra)
    assert catalogue.permit_prewarm(b, "listener-1")       # replaces the first
    assert not catalogue.allowed(root, ra)
    assert catalogue.allowed(root, "Protracker/Test Artist/other song.mod")
    assert catalogue.permit_prewarm(a, "listener-2")       # another listener: its own
    assert catalogue.allowed(root, ra)


@pytest.mark.asyncio
async def test_a_next_track_prewarm_fetches_that_one_tune(env, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from soniqboom.api import stream

    async def _noauth(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    monkeypatch.setattr(stream, "_prewarm_allowed", lambda *a, **k: True)
    await _enable(env)
    app = FastAPI()
    app.include_router(stream.router, prefix="/api")
    rel_a = "Protracker/Test Artist/tiny song.mod"
    rel_b = "Protracker/Test Artist/other song.mod"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post(f"/api/stream/{_tid(rel_b)}/prewarm?priority=ahead&pw=p1")
        assert r.json().get("reason") == "remote lookahead"           # further ahead: never
        r = await c.post(f"/api/stream/{_tid(rel_a)}/prewarm?priority=next&pw=p1")
        assert r.json().get("status") in ("queued", "already_running"), r.json()
    for _ in range(100):
        if env["cache"].peek_cached(ROOT, "/" + rel_a):
            break
        await asyncio.sleep(0.05)
    assert env["cache"].peek_cached(ROOT, "/" + rel_a)
    assert _retrs(env) == [f"/pub/modules/{rel_a}"]


@pytest.mark.asyncio
async def test_new_downloads_are_rate_limited(env, monkeypatch):
    await _enable(env)
    monkeypatch.setattr(catalogue, "RATE_MAX", 2)
    p = [f"{ROOT}:/{r}" for r in ("Protracker/Test Artist/tiny song.mod",
                                  "Protracker/Test Artist/other song.mod",
                                  "Protracker/- unknown/nameless.mod")]
    assert catalogue.permit_play(p[0]) and catalogue.permit_play(p[1])
    assert not catalogue.permit_play(p[2])
    assert catalogue.permit_play(p[0])                     # a window already open: fine


@pytest.mark.asyncio
async def test_offline_modland_plays_cached_tunes_only(env):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    from soniqboom.core.filesource import _active_sources, get_source
    await _enable(env)
    store = env["store"]
    cached_rel = "Protracker/Test Artist/tiny song.mod"
    track = await get_track(_tid(cached_rel))
    await stream._resolve_play_source(_tid(cached_rel), track)        # now cached
    env["srv"].stop()
    _drop_ftp_pools(env["srv"].port)
    other = "Protracker/Test Artist/other song.mod"
    track = await get_track(_tid(other))
    with pytest.raises(HTTPException) as ei:
        await stream._resolve_play_source(_tid(other), track)
    assert ei.value.status_code == 503 and "can't be reached" in ei.value.detail
    src = _active_sources[ROOT]
    assert src.is_offline() and get_source(ROOT) is None
    assert src._offline_streak == 1                          # one failure, no retry storm
    await asyncio.sleep(0.05)
    assert {d["path"]: d for d in store.list_scan_dirs()}[ROOT]["status"] == "unavailable"
    with pytest.raises(HTTPException) as ei:                  # held offline: no new attempt
        await stream._resolve_play_source(_tid(other), track)
    assert ei.value.status_code == 503
    track = await get_track(_tid(cached_rel))
    path, *_ = await stream._resolve_play_source(_tid(cached_rel), track)
    assert Path(path).read_bytes() == SERVED[cached_rel]      # the cached copy still plays
    assert store.get_track(_tid(other)) is not None           # nothing deleted


# ── the admin API ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_endpoints_switch_scope_subsonic_refresh_and_off(env, monkeypatch, tmp_path):
    import httpx
    import shutil
    from fastapi import FastAPI
    from soniqboom.api import admin
    from soniqboom.core import users

    def _fake_download(dest):                     # the fixture index, no network
        shutil.copyfile(env["zip"], dest)
        return {"bytes": dest.stat().st_size, "last_modified": None}
    monkeypatch.setattr(mc, "_download_index", _fake_download)
    (tmp_path / "users").mkdir()
    ustore = users.UserStore(tmp_path / "users")
    monkeypatch.setattr(users, "_instance", ustore)
    u = ustore.create("adm", "Password123!x", "admin")
    tok = ustore.issue_session(u.id)[0]
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 cookies={"sb_session": tok}) as c:
        async def settle() -> dict:
            for _ in range(200):
                s = (await c.get("/api/admin/catalog/modland")).json()
                if s["state"] not in ("building", "removing"):
                    return s
                await asyncio.sleep(0.05)
            raise AssertionError("never settled")
        s = (await c.get("/api/admin/catalog/modland")).json()
        assert s["enabled"] is False and s["state"] == "off" and s["scope"] == "standard"
        r = await c.post("/api/admin/catalog/modland", json={"enabled": True})
        assert r.status_code == 200 and r.json()["enabled"] is True
        s = await settle()
        assert s["state"] == "ready" and s["tunes"] == 8, s
        assert s["index_rows"] == len(SERVED) and s["scope_counts"]["all"] == 9
        assert s["index_date"] and s["last_refresh"] and s["next_refresh"]
        r = await c.post("/api/admin/catalog/modland", json={"scope": "bogus"})
        assert r.status_code == 400
        await c.post("/api/admin/catalog/modland", json={"scope": "all"})
        s = await settle()
        assert s["scope"] == "all" and s["tunes"] == 9
        s = (await c.post("/api/admin/catalog/modland", json={"subsonic": True})).json()
        assert s["subsonic"] is True
        r = await c.post("/api/admin/catalog/modland/refresh")
        assert r.status_code == 200
        s = await settle()
        assert s["state"] == "ready" and s["error"] is None
        await c.post("/api/admin/catalog/modland", json={"enabled": False})
        s = await settle()
        assert s["enabled"] is False and s["tunes"] == 0 and s["state"] == "off"
        r = await c.post("/api/admin/catalog/modland/refresh")
        assert r.status_code == 409
        # A network share on Modland's own root can't be added beside it.
        await c.post("/api/admin/catalog/modland", json={"enabled": True})
        await settle()
        r = await c.post("/api/admin/shares", json={"protocol": "ftp", "host": "ftp.modland.com",
                                                    "remote_path": "/pub/modules"})
        assert r.status_code == 409
    assert _retrs(env) == []


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_catalogue(env, monkeypatch):
    await _enable(env)
    store = env["store"]
    n = len(store.get_track_ids_for_scan_root(mc.root_hash()))
    monkeypatch.setattr(mc, "_MIN_INDEX_ROWS", 10)            # the shrunken index fails
    tiny = _zip(env["tmp"] / "tiny.zip", {"Protracker/A/x.mod": _mod_bytes()})
    st = await mc.refresh(local_zip=tiny)
    assert "implausible" in (st["error"] or "")
    assert st["state"] == "ready"
    assert len(store.get_track_ids_for_scan_root(mc.root_hash())) == n
    # A big shrink (more than a quarter of the rows gone) is refused too.
    monkeypatch.setattr(mc, "_MIN_INDEX_ROWS", 1)
    half = {k: v for i, (k, v) in enumerate(SERVED.items()) if i % 2}
    st = await mc.refresh(local_zip=_zip(env["tmp"] / "half.zip", half))
    assert "the current catalogue stays" in (st["error"] or "") and st["shrink_refused"]
    assert len(store.get_track_ids_for_scan_root(mc.root_hash())) == n
    # The daily refresh keeps refusing it; "Refresh now" after that takes it.
    st = await mc.refresh(local_zip=_zip(env["tmp"] / "half.zip", half))
    assert st["shrink_refused"] and len(store.get_track_ids_for_scan_root(mc.root_hash())) == n
    st = await mc.refresh(local_zip=_zip(env["tmp"] / "half.zip", half), manual=True)
    assert st["error"] is None and not st["shrink_refused"]
    assert len(store.get_track_ids_for_scan_root(mc.root_hash())) < n


# ── ownership, jobs, offline flag (review follow-ups) ────────────────────────

def test_instrument_and_icon_files_are_not_tunes():
    assert mc._classify("IFF-SMUS/Allister Brimble/ACE II/Instruments/Monty1.instr") == mc.KIND_OTHER
    assert mc._classify("PokeyNoise/Someone/pn.title.info") == mc.KIND_OTHER
    assert mc._classify("Protracker/Test Artist/tiny song.mod") == mc.KIND_MUSIC


@pytest.mark.asyncio
async def test_a_network_share_on_the_root_is_never_touched(env, monkeypatch):
    """A plain network share already on Modland's /pub/modules owns the root:
    start-up, switch-off, refresh and scope never act on its tracks."""
    store = env["store"]
    rel = "Protracker/Test Artist/tiny song.mod"
    sid = _tid(rel)
    store.upsert_scan_dir(ROOT, status="ok")
    store.upsert_track({"id": sid, "path": f"{ROOT}:/{rel}", "title": "shared",
                        "format": "ProTracker", "file_size": 9, "added_at": 5,
                        "dir_hash": "d", "scan_root_hash": mc.root_hash()})
    monkeypatch.setattr(mc, "conflicting_share", lambda: "modland-share")
    mc.register_at_startup()
    await mc.startup()                                    # catalogue off
    await asyncio.sleep(0.05)
    assert not catalogue.is_catalogue_root(ROOT)
    assert store.get_track(sid) is not None and ROOT in store._scan_dirs
    assert mc.status()["tunes"] == 0
    await mc.disable()                                    # the admin's "off"
    assert store.get_track(sid) is not None and ROOT in store._scan_dirs
    store.set_config(mc.CONFIG_ENABLED, True)             # on, but the share is there
    await mc.startup()
    st = mc.status()
    assert st["state"] == "error" and "Refresh now" in st["error"]
    st = await mc.refresh(local_zip=env["zip"])
    assert "remove that share first" in (st["error"] or "")
    st = await mc.set_scope(mc.SCOPE_ALL)
    assert "remove that share first" in (st["error"] or "")
    assert store.get_track_ids_for_scan_root(mc.root_hash()) == {sid}
    assert store.get_track(sid)["title"] == "shared"
    assert not catalogue.is_catalogue_root(ROOT)
    assert _retrs(env) == []


@pytest.mark.asyncio
async def test_a_share_below_the_catalogue_root_is_not_catalogue(env, monkeypatch):
    from soniqboom.core import filesource
    await _enable(env)
    nested = f"{ROOT}/SNDH"
    monkeypatch.setitem(filesource._active_sources, nested, object())
    assert not catalogue.is_catalogue_path(f"{nested}:/Someone/x.sndh")
    assert catalogue.root_of(f"{nested}:/Someone/x.sndh") is None
    assert not catalogue.is_catalogue_path(nested)
    assert catalogue.is_catalogue_path(f"{ROOT}:/TFMX/Chris Huelsbeck/mdat.turrican 2-intro")
    assert catalogue.is_catalogue_path(f"{ROOT}/TFMX")
    from soniqboom.core import duplicates
    shared = {"id": "n1", "path": f"{nested}:/Someone/x.sndh", "file_size": 5, "title": "x"}
    assert catalogue.subsonic_hidden_path(shared["path"]) is False
    assert duplicates.group_key_for(shared) is not None


@pytest.mark.asyncio
async def test_switching_off_cancels_the_startup_repair_and_waiting_jobs(env):
    await _enable(env)
    store = env["store"]
    gone = [_tid("Protracker/Test Artist/other song.mod"), _tid("Protracker/- unknown/nameless.mod")]
    store.delete_track_ids(gone)                          # an interrupted first build
    assert mc._tune_count() == mc._expected_count() - 2
    await mc.startup()                                    # → a repair in 20 s
    heal = [t for t in mc._tasks if t.get_name() == "modland-catalog.startup"]
    assert heal
    mc.start_disable()
    for _ in range(200):
        if not [t for t in mc._job_tasks if not t.done()]:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    assert heal[0].done()
    assert not mc.enabled() and not store.get_track_ids_for_scan_root(mc.root_hash())
    assert not catalogue.is_catalogue_root(ROOT)
    ran = []

    async def body():
        ran.append(1)
        return {}
    await mc._run_job("refresh", body)                    # got the lock after the off
    assert ran == [] and not store.get_track_ids_for_scan_root(mc.root_hash())


@pytest.mark.asyncio
async def test_the_startup_repair_completes_an_interrupted_build_as_old_tunes(env, monkeypatch):
    await _enable(env)
    store = env["store"]
    gone = _tid("Protracker/Test Artist/other song.mod")
    store.delete_track_ids([gone])
    assert mc._tune_count() != mc._expected_count()
    res = await mc.apply_index()
    assert res["added"] == 1 and store.get_track(gone)["added_at"] == 1
    assert mc._tune_count() == mc._expected_count()


@pytest.mark.asyncio
async def test_the_offline_flag_works_after_a_restart_and_clears_after_the_hold(env, monkeypatch):
    from soniqboom.core.filesource import _active_sources
    await _enable(env)
    store = env["store"]
    monkeypatch.setattr(mc, "_job_lock_loop", None)       # as after a restart: no job ran
    monkeypatch.setattr(mc, "_loop", None)
    monkeypatch.setattr(mc, "_OFFLINE_HOLD_S", 0.3)
    mc.register_at_startup()
    src = _active_sources[ROOT]
    loop = asyncio.get_running_loop()
    status = lambda: {d["path"]: d for d in store.list_scan_dirs()}[ROOT]["status"]  # noqa: E731
    # Our own pool being busy is not Modland being down.
    await loop.run_in_executor(None, src._note_network_failure, TimeoutError(
        "FTP pool 'ftp.modland.com' acquire timed out after 60.0s (max=3, lane=stream)"))
    assert not src.is_offline()
    await loop.run_in_executor(None, src._note_network_failure,
                               ConnectionRefusedError("connection refused"))
    await asyncio.sleep(0.05)
    assert src.is_offline() and status() == "unavailable"
    await asyncio.sleep(0.8)                              # hold 0.3 s (+0.25)
    assert not src.is_offline() and status() == "ok"


@pytest.mark.asyncio
async def test_an_offline_uncached_play_spends_no_download_slot(env):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    from soniqboom.core.filesource import _active_sources
    await _enable(env)
    src = _active_sources[ROOT]
    src._offline_until = 1e18
    before = (catalogue.stats()["permits"], len(catalogue._new_fetches))
    rel = "Protracker/Test Artist/other song.mod"
    with pytest.raises(HTTPException) as ei:
        await stream._resolve_play_source(_tid(rel), await get_track(_tid(rel)))
    assert ei.value.status_code == 503 and "can't be reached" in ei.value.detail
    assert (catalogue.stats()["permits"], len(catalogue._new_fetches)) == before
    src._offline_until = 0.0
    assert _retrs(env) == []


@pytest.mark.asyncio
async def test_playlist_entries_survive_edits_while_hidden_or_switched_off(env, monkeypatch):
    import httpx
    import types
    from fastapi import FastAPI
    from soniqboom.api import subsonic
    from soniqboom.core import subsonic_state
    await _enable(env)
    store = env["store"]
    for i in (1, 2):
        store.upsert_track({"id": f"local-{i}", "path": f"/music/l{i}.mod", "title": f"l{i}",
                            "format": "ProTracker", "added_at": 5, "dir_hash": "d",
                            "scan_root_hash": "r", "file_size": 3})
    tiny = _tid("Protracker/Test Artist/tiny song.mod")
    other = _tid("Protracker/Test Artist/other song.mod")
    store.create_playlist("pl", "mix", ["local-1", tiny, "local-2", other])
    store.create_playlist("smart", "smart", query="tiny")
    user = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: user)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", lambda *a, **k: user)
    subsonic_state.reset_state(env["tmp"] / "sub_state.json")
    app = FastAPI()
    app.include_router(subsonic.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        async def ok(method, **params):
            r = await c.get(f"/rest/{method}", params={"f": "json", **params})
            body = r.json()["subsonic-response"]
            assert body["status"] == "ok", body
            return body
        pls = {p["id"]: p for p in (await ok("getPlaylists.view"))["playlists"]["playlist"]}
        assert pls["pl"]["songCount"] == 2                      # the hidden tunes don't count
        got = (await ok("getPlaylist.view", id="pl"))["playlist"]["entry"]
        assert [e["id"] for e in got] == ["local-1", "local-2"]
        # A Subsonic app replaces the list (reordered): the hidden entries stay,
        # each after the entry it followed.
        await ok("createPlaylist.view", playlistId="pl", songId=["local-2", "local-1"])
        assert store.get_playlist("pl")["track_ids"] == ["local-2", other, "local-1", tiny]
        await ok("updatePlaylist.view", playlistId="pl", songIndexToRemove=["0"])
        assert store.get_playlist("pl")["track_ids"] == [other, "local-1", tiny]
        smart = (await ok("getPlaylist.view", id="smart"))["playlist"]["entry"]
        assert tiny not in [e["id"] for e in smart]
    # Switched off: a web save of the visible entries keeps the catalogue's.
    await mc.disable()
    assert store.get_track(tiny) is None
    out = store.update_playlist("pl", {"track_ids": ["local-1"]})
    assert out["track_ids"] == [other, "local-1", tiny]
    await _enable(env)
    assert [store.get_track(t) is not None for t in (other, tiny)] == [True, True]
    # Back on and visible: a save that leaves an entry out removes it.
    out = store.update_playlist("pl", {"track_ids": ["local-1", tiny]})
    assert out["track_ids"] == ["local-1", tiny]


@pytest.mark.asyncio
async def test_what_a_play_read_survives_new_rules_and_switching_off(env, monkeypatch):
    await _enable(env)
    store = env["store"]
    rel = "Protracker/Test Artist/tiny song.mod"
    tid = _tid(rel)
    merged = mc.merged_track(dict(store.get_track(tid)),
                             {"title": "Real Title", "duration": 123.0, "file_md5": "abc",
                              "subsongs": 2, "format": "ProTracker", "file_size": 999},
                             "tiny song")
    store.upsert_tracks_batch([merged])
    t = store.get_track(tid)
    assert mc.is_merged(t) and t["file_size"] == len(SERVED[rel])   # the index's size

    def check():
        t = store.get_track(tid)
        assert mc.is_merged(t), t
        assert (t["title"], t["duration"], t["file_md5"], t["subsongs"]) == (
            "Real Title", 123.0, "abc", 2)
        assert t["artist"] == "Test Artist" and t["added_at"] == 1
    monkeypatch.setattr(mc, "RULES_VERSION", 99)          # new derivation rules: all rewritten
    st = await mc.refresh(local_zip=env["zip"])
    assert st["last_result"]["changed"] == 8, st
    check()
    await mc.disable()
    assert mc._parked_path().exists()
    await _enable(env)
    check()
    assert not mc._parked_path().exists()
    assert _retrs(env) == []


@pytest.mark.asyncio
async def test_the_subsonic_view_is_one_object(env):
    await _enable(env)
    from soniqboom.core.catalogue_view import subsonic_store
    store = env["store"]
    assert subsonic_store(store) is subsonic_store(store)
    hidden = subsonic_store(store)._h
    assert _tid("Protracker/Test Artist/tiny song.mod") in hidden
    store.delete_track_ids([_tid("Protracker/Test Artist/tiny song.mod")])
    assert _tid("Protracker/Test Artist/tiny song.mod") not in subsonic_store(store)._h


# ── second review follow-ups ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_unavailable_mark_from_before_a_restart_clears(env, monkeypatch):
    from soniqboom.core.filesource import _active_sources
    await _enable(env)
    store = env["store"]
    status = lambda: store._scan_dirs[ROOT]["status"]            # noqa: E731
    store.set_scan_dir_status(ROOT, "unavailable")              # left by the old process
    await mc.startup()
    assert status() == "ok"
    store.set_scan_dir_status(ROOT, "unavailable")
    src = _active_sources[ROOT]
    assert src._offline_streak == 0
    await asyncio.get_running_loop().run_in_executor(None, src._note_network_ok)
    await asyncio.sleep(0.05)
    assert status() == "ok"                                      # a good fetch clears it
    for t in list(mc._tasks):
        t.cancel()


def test_replacing_the_index_leaves_other_threads_connections_open(env, tmp_path):
    import threading
    mc.build_index_db(env["zip"], mc.db_path())
    held = {}

    def reader():
        held["con"] = mc._ro()
    th = threading.Thread(target=reader)
    th.start()
    th.join()
    mc._bump_db_gen()
    # Another thread's connection is never closed under it.
    assert held["con"].execute("SELECT count(*) FROM files").fetchone()[0] == len(SERVED)


def test_two_index_builds_at_once_use_their_own_files(env):
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(2) as ex:
        futs = [ex.submit(mc._refresh_index_files, download=False, local_zip=env["zip"])
                for _ in range(2)]
        infos = [f.result() for f in futs]
    assert all(i["rows"] == len(SERVED) for i in infos)
    left = [p.name for p in mc._dir().iterdir() if p.name.endswith((".building", ".part", ".copy"))]
    assert left == []


def test_a_repeated_index_line_is_one_file(tmp_path, tmp_data_dir, monkeypatch):
    monkeypatch.setattr(mc, "_MIN_INDEX_ROWS", 1)
    monkeypatch.setattr(mc, "_MIN_MUSIC_ROWS", 1)
    z = tmp_path / "dup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("allmods.txt", "12\tProtracker/A/x.mod\n12\tProtracker/A/x.mod\n"
                                   "9\tFasttracker 2/B/y.xm\n")
    info = mc.build_index_db(z, tmp_path / "d.sqlite")
    assert (info["rows"], info["music_rows"], info["standard_rows"]) == (2, 2, 1)


@pytest.mark.asyncio
async def test_a_disconnected_share_below_the_root_keeps_its_tracks(env):
    await _enable(env)
    from soniqboom.core import filesource
    nested = f"{ROOT}/SNDH"
    assert nested not in filesource._active_sources               # not connected
    assert not catalogue.is_catalogue_path(f"{nested}:/Someone/x.sndh")
    assert catalogue.is_catalogue_path(f"{ROOT}/SNDH")             # the catalogue's folder


@pytest.mark.asyncio
async def test_an_offline_archive_takes_no_prewarm_slot(env, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from soniqboom.api import stream
    from soniqboom.core.filesource import _active_sources

    async def _noauth(*a, **k):
        return None
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    monkeypatch.setattr(stream, "_prewarm_allowed", lambda *a, **k: True)
    await _enable(env)
    _active_sources[ROOT]._offline_until = 1e18
    app = FastAPI()
    app.include_router(stream.router, prefix="/api")
    before = len(catalogue._new_fetches)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post(f"/api/stream/{_tid('Protracker/Test Artist/tiny song.mod')}"
                         "/prewarm?priority=next&pw=p1")
    assert r.json().get("reason") == "source offline"
    assert len(catalogue._new_fetches) == before
    _active_sources[ROOT]._offline_until = 0.0
    assert _retrs(env) == []


@pytest.mark.asyncio
async def test_only_tunes_a_narrower_scope_leaves_out_are_parked(env):
    from soniqboom.core.store import PARKED_PLAYLIST_IDS
    await _enable(env, scope="all")
    store = env["store"]
    ft2 = _tid("Fasttracker 2/Someone/big tune.xm")
    other = _tid("Protracker/Test Artist/other song.mod")
    store.create_playlist("pl", "p", [ft2, other])
    files = dict(SERVED)
    del files["Protracker/Test Artist/other song.mod"]          # gone from Modland
    await mc.refresh(local_zip=_zip(env["tmp"] / "gone.zip", files))
    assert store.get_track(other) is None
    assert other not in (store.get_config(PARKED_PLAYLIST_IDS) or [])
    await mc.set_scope(mc.SCOPE_STANDARD)                         # FT2 left out: parked
    assert ft2 in (store.get_config(PARKED_PLAYLIST_IDS) or [])
    out = store.update_playlist("pl", {"track_ids": [other, ft2]})   # (a stale client)
    assert out["track_ids"] == [ft2] and out["dropped_ids"] == [other]   # ft2 is kept
    await mc.set_scope(mc.SCOPE_ALL)                              # back: no longer parked
    assert ft2 not in (store.get_config(PARKED_PLAYLIST_IDS) or [])


@pytest.mark.asyncio
async def test_refresh_now_arms_the_daily_refresh_and_an_unreadable_config_owns_the_root(
        env, monkeypatch):
    await _enable(env)
    for t in list(mc._tasks):
        t.cancel()
    await asyncio.sleep(0)
    monkeypatch.setattr(mc, "_refresh_task", None)
    await mc.refresh(local_zip=env["zip"])
    assert mc._refresh_task is not None and not mc._refresh_task.done()
    mc._refresh_task.cancel()
    import soniqboom.config as config

    def _broken():
        raise ValueError("bad json")
    monkeypatch.setattr(config, "load_local_conf", _broken)
    assert mc.conflicting_share()                                 # unknown: never "no share"


@pytest.mark.asyncio
async def test_offline_modland_marks_only_the_tunes_that_were_never_fetched(env):
    """Settings → "Tracks on unavailable drives": while Modland can't be reached,
    a tune played before (its file in the remote cache) still counts as
    playable; one never fetched is marked offline (kind ``catalogue``) — and
    both are playable again once the hold is over."""
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core import availability
    from soniqboom.core.data import get_track
    from soniqboom.core.filesource import _active_sources
    availability.reset_for_tests()
    await _enable(env)
    store = env["store"]
    cached_rel = "Protracker/Test Artist/tiny song.mod"
    other = "Protracker/Test Artist/other song.mod"
    assert not availability.snapshot(store).dead               # reachable: nothing marked
    await stream._resolve_play_source(_tid(cached_rel), await get_track(_tid(cached_rel)))
    env["srv"].stop()
    _drop_ftp_pools(env["srv"].port)
    with pytest.raises(HTTPException):
        await stream._resolve_play_source(_tid(other), await get_track(_tid(other)))
    await asyncio.sleep(0.05)
    snap = availability.snapshot(store)
    assert snap.dead.get(store._scan_dirs[ROOT]["path_hash"]) == "catalogue"
    assert snap.kind_of(store.get_track(_tid(cached_rel))) is None
    assert snap.kind_of(store.get_track(_tid(other))) == "catalogue"
    assert _tid(other) in snap.unavailable and _tid(cached_rel) not in snap.unavailable
    # The hold runs out (no status write needed): the next snapshot sees it.
    _active_sources[ROOT]._offline_until = 0.0
    store.set_scan_dir_status(ROOT, "ok")
    assert not availability.snapshot(store).unavailable
    availability.reset_for_tests()


# ── how often the index is refreshed (gear → External sources, beside Refresh now) ──

DAY = 24 * 3600


def _built_at(monkeypatch, ts, **extra):
    monkeypatch.setattr(mc, "index_info", lambda: {"built_at": str(ts), **extra})


@pytest.mark.parametrize("name,days", [("daily", 1), ("weekly", 7), ("biweekly", 14),
                                       ("monthly", 30)])
def test_the_next_refresh_is_the_last_good_one_plus_the_interval(env, monkeypatch, name, days):
    monkeypatch.setattr(mc, "_rearm_not_before", 0.0)
    built = time.time() - 3600
    _built_at(monkeypatch, built, fingerprint=mc._fingerprint())
    env["store"].set_config(mc.CONFIG_INTERVAL, name)
    assert mc.interval() == name and mc.interval_seconds() == days * DAY
    assert mc._next_due() == pytest.approx(built + days * DAY)
    monkeypatch.setattr(mc, "enabled", lambda: True)
    st = mc.status()
    assert st["interval"] == name and st["next_refresh"] == pytest.approx(built + days * DAY)
    assert st["intervals"] == {"daily": "Daily", "weekly": "Weekly",
                               "biweekly": "Every 2 weeks", "monthly": "Monthly"}
    # "fresh" (no download on a scope change / switch-on) follows the interval too
    _built_at(monkeypatch, time.time() - (days * DAY - 60), fingerprint=mc._fingerprint())
    assert mc._index_fresh()
    _built_at(monkeypatch, time.time() - (days * DAY + 60), fingerprint=mc._fingerprint())
    assert not mc._index_fresh()


def test_an_unknown_stored_interval_reads_as_daily(env):
    env["store"].set_config(mc.CONFIG_INTERVAL, "hourly")
    assert mc.interval() == "daily"
    with pytest.raises(ValueError):
        mc.set_interval("fortnightly")
    assert env["store"].get_config(mc.CONFIG_INTERVAL) == "hourly"     # untouched


def test_changing_the_interval_rearms_the_schedule_at_once(env, monkeypatch):
    monkeypatch.setattr(mc, "_rearm_not_before", 0.0)
    built = time.time() - 3 * DAY
    _built_at(monkeypatch, built)
    mc.set_interval("weekly")
    assert mc._next_due() == pytest.approx(built + 7 * DAY)            # 4 days from now
    t0 = time.time()
    mc.set_interval("daily")                                           # already 2 days past due
    due = mc._next_due()
    assert t0 + mc._REARM_GRACE_S - 1 <= due <= time.time() + mc._REARM_GRACE_S + 1   # soon, not now
    mc.set_interval("monthly")
    assert mc._next_due() == pytest.approx(built + 30 * DAY)
    # a no-op choice leaves the re-arm floor where it was
    floor = mc._rearm_not_before
    mc.set_interval("monthly")
    assert mc._rearm_not_before == floor


@pytest.mark.asyncio
async def test_a_change_wakes_the_sleeping_refresh_loop(env, monkeypatch):
    monkeypatch.setattr(mc, "_rearm_not_before", 0.0)
    monkeypatch.setattr(mc, "_refresh_wake", None)
    sleeper = asyncio.ensure_future(mc._sleep_or_wake(3600))
    await asyncio.sleep(0.01)
    assert not sleeper.done()
    mc.set_interval("weekly")
    await asyncio.wait_for(sleeper, 1.0)                               # woke up, re-reads the schedule


@pytest.mark.asyncio
async def test_the_loop_refreshes_when_due_and_a_change_reschedules_it(env, monkeypatch):
    monkeypatch.setattr(mc, "_rearm_not_before", 0.0)
    monkeypatch.setattr(mc, "_refresh_wake", None)
    monkeypatch.setattr(mc, "_REARM_GRACE_S", 0.05)
    monkeypatch.setattr(mc, "enabled", lambda: True)
    calls = []

    async def fake_refresh(**kw):
        calls.append(time.time())
        _built_at(monkeypatch, time.time())                            # a good refresh: clock reset
        return {}
    monkeypatch.setattr(mc, "refresh", fake_refresh)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(mc.asyncio, "sleep", lambda s, *a: real_sleep(min(s, 0.01)))
    env["store"].set_config(mc.CONFIG_INTERVAL, "weekly")
    _built_at(monkeypatch, time.time() - 3 * DAY)                      # due in 4 days
    task = asyncio.ensure_future(mc._refresh_loop())
    try:
        await real_sleep(0.1)
        assert calls == []                                             # not due
        mc.set_interval("daily")                                       # 2 days overdue → soon
        for _ in range(100):
            if calls:
                break
            await real_sleep(0.02)
        assert len(calls) == 1
        await real_sleep(0.1)
        assert len(calls) == 1                                         # next one a day after it
    finally:
        task.cancel()


def test_a_failed_refresh_is_retried_soon_whatever_the_interval(env, monkeypatch):
    monkeypatch.setattr(mc, "_rearm_not_before", 0.0)
    env["store"].set_config(mc.CONFIG_INTERVAL, "monthly")
    now = time.time()
    # overdue monthly refresh whose download failed 10 minutes ago → an hour after that
    _built_at(monkeypatch, now - 40 * DAY)
    monkeypatch.setitem(mc._status, "error", "timed out")
    monkeypatch.setitem(mc._status, "last_attempt", now - 600)
    monkeypatch.setitem(mc._status, "shrink_refused", False)
    assert mc._next_due() == pytest.approx(now - 600 + 3600)
    # an index refused as implausible → a day after the attempt
    monkeypatch.setitem(mc._status, "error", "implausible index: 12 files")
    assert mc._next_due() == pytest.approx(now - 600 + DAY)
    # a failed "Refresh now" two days after a good refresh: the clock keeps the
    # good one (never the interval counted from the failure)
    _built_at(monkeypatch, now - 2 * DAY)
    monkeypatch.setitem(mc._status, "error", "connection refused")
    assert mc._next_due() == pytest.approx(now - 2 * DAY + 30 * DAY)
    # no index at all yet and the first download failed → an hour after it
    monkeypatch.setattr(mc, "index_info", lambda: {})
    assert mc._next_due() == pytest.approx(now - 600 + 3600)


def test_the_interval_survives_a_restart(tmp_path, monkeypatch):
    from soniqboom.core import persistence
    from soniqboom.core.aof import AOFWriter
    monkeypatch.setattr(store_mod, "_store", None)
    persistence.init_persistence(tmp_path)
    st = store_mod.get_store()
    w = AOFWriter(tmp_path / "library.aof")
    st._aof_append = w.append
    mc.set_interval("biweekly")
    w.flush_sync()
    st._aof_append = None
    store_mod._store = None
    state = persistence.load_snapshot(tmp_path)
    persistence.replay_aof(state, tmp_path)
    persistence.populate_store(state)
    assert store_mod.get_store() is not st
    assert mc.interval() == "biweekly" and mc.interval_seconds() == 14 * DAY


@pytest.mark.asyncio
async def test_the_admin_api_takes_and_validates_the_interval(env, monkeypatch, tmp_path):
    import httpx
    from fastapi import FastAPI
    from soniqboom.api import admin
    from soniqboom.core import users
    monkeypatch.setattr(mc, "_rearm_not_before", 0.0)
    (tmp_path / "users").mkdir()
    ustore = users.UserStore(tmp_path / "users")
    monkeypatch.setattr(users, "_instance", ustore)
    u = ustore.create("adm", "Password123!x", "admin")
    tok = ustore.issue_session(u.id)[0]
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 cookies={"sb_session": tok}) as c:
        assert (await c.get("/api/admin/catalog/modland")).json()["interval"] == "daily"
        for bad in ("fortnightly", "", 7, None, ["weekly"]):
            r = await c.post("/api/admin/catalog/modland", json={"interval": bad})
            assert r.status_code == 400, bad
        assert env["store"].get_config(mc.CONFIG_INTERVAL) is None
        r = await c.post("/api/admin/catalog/modland", json={"interval": "monthly"})
        assert r.status_code == 200 and r.json()["interval"] == "monthly"
        assert env["store"].get_config(mc.CONFIG_INTERVAL) == "monthly"
        # (allowed while the catalogue is off — it applies once it is on)
        assert r.json()["enabled"] is False


# ── changelog deltas instead of the full index ───────────────────────────────

import datetime as _dt  # noqa: E402


def _delta_ftp(env, monkeypatch, *, count: int | None = None):
    """Point the changelog refresh at the local FTP server (its banner
    announcing ``count`` modules), and make any full download a counted copy
    of the fixture zip (a fallback must be visible)."""
    import ftplib
    import shutil
    srv = env["srv"]
    if count is not None:
        srv.welcome = (f"Welcome to Modland - probably the largest module archive in the world.\n"
                       f"Currently there are {count} modules online in 9 different formats,")

    def _connect():
        f = ftplib.FTP(timeout=10)
        w = f.connect("127.0.0.1", srv.port)
        f.login("anonymous", "x@")
        return f, w
    monkeypatch.setattr(mc, "_delta_connect", _connect)
    fulls = []

    def _download(dest):
        fulls.append(dest)
        shutil.copyfile(env["zip"], dest)
        return {"bytes": dest.stat().st_size, "last_modified": None}
    monkeypatch.setattr(mc, "_download_index", _download)
    return fulls


def _changelog(env, day: _dt.date, text: str, *, root: bool = False) -> bytes:
    data = text.encode("utf-8")
    d = env["ftp_root"] if root else env["ftp_root"] / "pub" / "changelogs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"changelog_{day.isoformat()}.txt").write_bytes(data)
    return data


def _serve(env, rel: str, data: bytes | None) -> None:
    p = env["ftp_root"] / "pub" / "modules" / rel
    if data is None:
        p.unlink()
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _strict(env=None) -> None:
    """Changelog days after the full index's date: applied strictly (the
    fixture's index is dated today, so its days would be replays)."""
    mc._set_info(mc.db_path(), {"cl_replay_to": "2000-01-01"})
    mc._bump_db_gen()


def _lu(kind, changelogs, at_least=0, reason=None):
    """``status()["last_update"]`` matcher: what was downloaded counts the
    FTP control traffic too (~1-2 KB), so only a range is exact."""
    def ok(lu):
        return (lu["kind"] == kind and lu["changelogs"] == changelogs
                and at_least <= lu["bytes"] <= at_least + 4096 and lu["reason"] == reason)
    return ok


def _move_served(env, old: str, new: str) -> None:
    base = env["ftp_root"] / "pub" / "modules"
    (base / new).parent.mkdir(parents=True, exist_ok=True)
    (base / old).rename(base / new)


@pytest.mark.asyncio
async def test_a_refresh_applies_the_changelogs_and_keeps_user_data_of_moved_tunes(
        env, monkeypatch, tmp_data_dir):
    from soniqboom.core import subsonic_state
    st = subsonic_state.reset_state(tmp_data_dir / "subsonic_state.json")
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    _strict()
    store = env["store"]
    old_rel = "Protracker/Test Artist/other song.mod"
    new_rel = "Protracker/Test Artist/other song (fixed).mod"
    moved_old = "Protracker/- unknown/nameless.mod"
    moved_new = "Protracker/Someone Known/nameless.mod"
    store.set_rating(_tid(old_rel), 4)
    store.record_play(_tid(old_rel))
    store.record_play(_tid(old_rel))
    store.push_history({"track_id": _tid(old_rel), "title": "x", "artist": "", "ts": 5})
    store.create_playlist("pl1", "Mine", track_ids=[_tid(old_rel), {"id": _tid(moved_old)}])
    await st.star("u1", "song", [_tid(old_rel)])
    await st.save_play_queue("u1", [_tid(old_rel), _tid(moved_old)], current=_tid(old_rel),
                             position=0, changed_by="test")
    before_rows = int(mc.index_info()["rows"])
    # Modland's side of the day: a rename, a move, an add, a delete, an update.
    _move_served(env, old_rel, new_rel)
    _move_served(env, moved_old, moved_new)
    new_song = _mod_bytes(b"brand new")
    _serve(env, "Protracker/Test Artist/brand new.mod", new_song)
    _serve(env, "HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid", None)
    bigger = _mod_bytes() + b"\x00" * 100
    _serve(env, "Protracker/Test Artist/tiny song.mod", bigger)
    day = _dt.date.today() - _dt.timedelta(days=1)
    data = _changelog(env, day, (
        f"renamed: {old_rel}\n     to: other song (fixed).mod\n"
        f"  moved: {moved_old}\n     to: Protracker/Someone Known\n"
        "  added: Protracker/Test Artist/brand new.mod\n"
        "deleted: HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid\n"
        "updated: Protracker/Test Artist/tiny song.mod\n"
        "  added: software/trackers/DOS/Some Tracker.zip\n"))      # (outside the module index)
    res = await mc.refresh()
    assert res.get("error") is None, res
    assert fulls == [], "the refresh downloaded the full index"
    s = mc.status()
    assert _lu("delta", 1, len(data))(s["last_update"]), s["last_update"]
    assert s["index_bytes"] == s["last_update"]["bytes"]
    # index
    assert mc.entry(old_rel) is None and mc.entry(new_rel) is not None
    assert mc.entry(moved_new) is not None and mc.entry(moved_old) is None
    assert mc.entry("Protracker/Test Artist/brand new.mod")[0] == len(new_song)
    assert mc.entry("HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid") is None
    assert mc.entry("Protracker/Test Artist/tiny song.mod")[0] == len(bigger)
    assert int(mc.index_info()["rows"]) == before_rows        # +1 added −1 deleted
    # library
    assert store.get_track(_tid(old_rel)) is None
    t = store.get_track(_tid(new_rel))
    assert t is not None and t["path"].endswith("other song (fixed).mod")
    assert store.get_track(_tid(moved_new)) is not None and store.get_track(_tid(moved_old)) is None
    assert store.get_track(_tid("Protracker/Test Artist/brand new.mod")) is not None
    assert store.get_track(_tid("HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid")) is None
    assert store.get_track(_tid("Protracker/Test Artist/tiny song.mod"))["file_size"] == len(bigger)
    # the user data followed the renamed / moved tunes
    assert store.get_rating(_tid(new_rel)) == 4 and not store.get_rating(_tid(old_rel))
    assert store.get_play_stats(_tid(new_rel))["count"] == 2
    assert not (store.get_play_stats(_tid(old_rel)) or {}).get("count")
    assert any(h["track_id"] == _tid(new_rel) for h in store.get_history(10))
    entries = store.get_playlist("pl1")["track_ids"]
    assert entries == [_tid(new_rel), {"id": _tid(moved_new)}]
    assert _tid(new_rel) in st.starred("u1", "song") and _tid(old_rel) not in st.starred("u1", "song")
    q = st.play_queue("u1")
    assert q["ids"] == [_tid(new_rel), _tid(moved_new)] and q["current"] == _tid(new_rel)
    # Again, nothing new: the changelog is not read twice (same size) — nothing changes.
    rows = int(mc.index_info()["rows"])
    res2 = await mc.refresh()
    assert res2.get("error") is None and fulls == []
    assert _lu("delta", 0)(mc.status()["last_update"])
    assert int(mc.index_info()["rows"]) == rows and store.get_rating(_tid(new_rel)) == 4
    # The newest changelog grows during the day (the copy in the root): read
    # again, only what is new changes; the lines read before are no-ops.
    later = _mod_bytes(b"later")
    _serve(env, "Protracker/Test Artist/later.mod", later)
    today = _dt.date.today()
    _changelog(env, today, "  added: Protracker/Test Artist/brand new 2.mod\n", root=True)
    _serve(env, "Protracker/Test Artist/brand new 2.mod", _mod_bytes(b"bn2"))
    await mc.refresh()
    grown = _changelog(env, today, "  added: Protracker/Test Artist/brand new 2.mod\n"
                                   "  added: Protracker/Test Artist/later.mod\n", root=True)
    await mc.refresh()
    # (only the new line is applied; the file is downloaded whole)
    assert _lu("delta", 1, len(grown))(mc.status()["last_update"])
    assert mc.entry("Protracker/Test Artist/later.mod")[0] == len(later)
    assert store.get_track(_tid("Protracker/Test Artist/brand new 2.mod")) is not None
    assert int(mc.index_info()["rows"]) == rows + 2
    assert fulls == []


@pytest.mark.asyncio
async def test_deltas_honour_the_scope(env, monkeypatch):
    await _enable(env)                                           # standard scope
    _delta_ftp(env, monkeypatch)
    _serve(env, "Fasttracker 2/Someone/new tune.xm", b"Extended Module: new" + b"\x00" * 300)
    _changelog(env, _dt.date.today(), "  added: Fasttracker 2/Someone/new tune.xm\n", root=True)
    await mc.refresh()
    assert mc.entry("Fasttracker 2/Someone/new tune.xm") is not None     # in the index…
    assert env["store"].get_track(_tid("Fasttracker 2/Someone/new tune.xm")) is None  # …not listed
    await mc.set_scope(mc.SCOPE_ALL)
    assert env["store"].get_track(_tid("Fasttracker 2/Someone/new tune.xm")) is not None


@pytest.mark.parametrize("trigger", ["big", "unreadable", "lines", "monthly", "gap", "drift",
                                     "unplaceable"])
@pytest.mark.asyncio
async def test_each_fallback_downloads_the_full_index(env, monkeypatch, trigger):
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch, count=len(SERVED))
    _strict()
    today = _dt.date.today()
    _changelog(env, today - _dt.timedelta(days=1), "  added: Protracker/Test Artist/readme.txt\n")
    await mc.refresh()                                           # baseline: the banner's count
    assert fulls == [] and mc.index_info().get("count_ref") == "0"
    if trigger == "big":
        monkeypatch.setattr(mc, "_DELTA_MAX_FILE_BYTES", 10)
        _changelog(env, today, "  added: Protracker/Test Artist/a long line here.mod\n", root=True)
    elif trigger == "unreadable":
        _changelog(env, today, "hello world\n", root=True)
    elif trigger == "lines":
        monkeypatch.setattr(mc, "_DELTA_MAX_LINES", 2)
        _changelog(env, today, "deleted: Protracker/x/1.mod\ndeleted: Protracker/x/2.mod\n"
                               "deleted: Protracker/x/3.mod\n", root=True)
    elif trigger == "monthly":
        mc._set_info(mc.db_path(), {"full_at": time.time() - 31 * DAY})
        mc._bump_db_gen()
    elif trigger == "gap":
        mc._set_info(mc.db_path(), {"cl_since": (today - _dt.timedelta(days=60)).isoformat()})
        mc._bump_db_gen()
    elif trigger == "drift":
        env["srv"].welcome = "Currently there are 99999 modules online in 9 different formats,"
    elif trigger == "unplaceable":
        _changelog(env, today, "  moved: Protracker/nowhere/x.mod\n     to: Protracker/else\n",
                   root=True)
    res = await mc.refresh()
    assert res.get("error") is None, res
    assert len(fulls) == 1, f"{trigger}: no full download"
    s = mc.status()
    # (the download, plus the changelogs read to bring the new index forward)
    assert s["last_update"]["kind"] == "full" and s["last_update"]["bytes"] >= env["zip"].stat().st_size
    assert s["index_bytes"] >= env["zip"].stat().st_size


@pytest.mark.asyncio
async def test_first_enable_and_a_missing_index_take_the_full_index(env, monkeypatch):
    fulls = _delta_ftp(env, monkeypatch)
    assert mc._full_reason({}) == "no index yet"
    env["store"].set_config(mc.CONFIG_SCOPE, "standard")
    st = await mc.enable()                                       # downloads (no local zip)
    assert st["state"] == "ready" and len(fulls) == 1
    assert mc.status()["last_update"]["kind"] == "full"
    mc.db_path().unlink()
    mc._bump_db_gen()
    await mc.refresh()
    assert len(fulls) == 2


@pytest.mark.asyncio
async def test_an_unreachable_changelog_server_fails_the_refresh_without_a_full_download(
        env, monkeypatch):
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)

    def _down():
        raise ConnectionRefusedError("no route")
    monkeypatch.setattr(mc, "_delta_connect", _down)
    res = await mc.refresh()
    assert "no route" in (res.get("error") or "") and fulls == []
    # tried again an hour after the attempt at the soonest (here the index is
    # fresh, so its own interval decides)
    assert mc._next_due() >= mc._status["last_attempt"] + 3600 - 1


@pytest.mark.asyncio
async def test_a_move_the_library_missed_is_redone_by_the_next_refresh(env, monkeypatch):
    """The index records the moves the library still has to follow: a failed
    step after the index was updated loses no user data, and the retry comes
    an hour later, not an interval later."""
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    store = env["store"]
    old = "Protracker/- unknown/nameless.mod"
    new = "Protracker/Someone Known/nameless.mod"
    store.set_rating(_tid(old), 5)
    _move_served(env, old, new)
    _changelog(env, _dt.date.today(), f"  moved: {old}\n     to: Protracker/Someone Known\n",
               root=True)
    real = mc._move_tracks

    async def _boom(moves, changed=frozenset()):
        raise RuntimeError("interrupted")
    monkeypatch.setattr(mc, "_move_tracks", _boom)
    res = await mc.refresh()
    assert "interrupted" in (res.get("error") or "")
    assert mc.entry(new) is not None and mc.entry(old) is None           # the index moved on…
    assert mc._pending_moves(mc.index_info()) == {old: new}              # …and remembers
    assert store.get_rating(_tid(old)) == 5
    assert mc._next_due() == pytest.approx(mc._status["last_attempt"] + 3600, abs=2)
    res = await mc.refresh()                     # (still failing: tried first, blocks the rest)
    assert "interrupted" in (res.get("error") or "")
    # Moved on again in the index before the library caught up: one move,
    # old → newest.
    from soniqboom.core import modland_changelog as mcl
    newer = "Protracker/Someone Known/nameless (remix).mod"
    _move_served(env, new, newer)
    fetched = {"plan": mcl.plan([("moved", new, newer)], lambda r: mc.entry(r) is not None),
               "sizes": {}, "banner": None, "applied": {}, "dates": [], "bytes": 0}
    done = await asyncio.get_running_loop().run_in_executor(
        None, mc._apply_delta, fetched, time.time())
    mc._bump_db_gen()
    assert done["pending"] == {old: newer} == mc._pending_moves(mc.index_info())
    monkeypatch.setattr(mc, "_move_tracks", real)
    res = await mc.refresh()                                             # nothing new to read
    assert res.get("error") is None and fulls == []
    assert store.get_rating(_tid(newer)) == 5 and store.get_track(_tid(newer)) is not None
    assert store.get_track(_tid(old)) is None and store.get_track(_tid(new)) is None
    assert mc._pending_moves(mc.index_info()) == {}


def test_pending_moves_read_back():
    import json
    info = {"pending_moves": json.dumps({"A/a.mod": "B/a.mod"})}
    assert mc._pending_moves(info) == {"A/a.mod": "B/a.mod"}
    assert mc._pending_moves({"pending_moves": "not json"}) == {}
    assert mc._pending_moves({"pending_moves": "[1]"}) == {}
    assert mc._pending_moves({}) == {}


@pytest.mark.asyncio
async def test_switching_on_again_catches_up_from_the_changelogs(env, monkeypatch):
    """An index kept from an earlier switch-on that is no longer fresh is
    brought up to date from the changelogs — no full download — and a tune
    moved meanwhile keeps its rating."""
    await _enable(env)
    store = env["store"]
    old = "Protracker/- unknown/nameless.mod"
    new = "Protracker/Someone Known/nameless.mod"
    store.set_rating(_tid(old), 3)
    await mc.disable()
    assert store.get_track(_tid(old)) is None and store.get_rating(_tid(old)) == 3
    fulls = _delta_ftp(env, monkeypatch)
    mc._set_info(mc.db_path(), {"built_at": time.time() - 2 * DAY})       # daily: stale
    mc._bump_db_gen()
    assert not mc._index_fresh()
    _move_served(env, old, new)
    added = _mod_bytes(b"while off")
    _serve(env, "Protracker/Test Artist/while off.mod", added)
    data = _changelog(env, _dt.date.today() - _dt.timedelta(days=1),
                      f"  moved: {old}\n     to: Protracker/Someone Known\n"
                      "  added: Protracker/Test Artist/while off.mod\n")
    st = await mc.enable()
    for t in list(mc._tasks):
        if "refresh-loop" in (t.get_name() or ""):
            t.cancel()
    assert st.get("error") is None, st
    assert fulls == []
    assert _lu("delta", 1, len(data))(mc.status()["last_update"])
    assert store.get_track(_tid("Protracker/Test Artist/while off.mod")) is not None
    assert store.get_track(_tid(new)) is not None and store.get_rating(_tid(new)) == 3
    # A fresh kept index is used as it is: no changelog read, no download.
    await mc.disable()
    reads = len(_retrs(env))
    await mc.enable()
    for t in list(mc._tasks):
        if "refresh-loop" in (t.get_name() or ""):
            t.cancel()
    assert fulls == [] and len(_retrs(env)) == reads


@pytest.mark.asyncio
async def test_changelogs_are_asked_for_by_name_not_by_listing_their_folder(env, monkeypatch):
    """A refresh asks for each day's changelog with SIZE (a day without one
    answers 550) — the 1,300-file changelog folder is never listed; a server
    without SIZE gets the listing instead."""
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    srv = env["srv"]
    today = _dt.date.today()
    _serve(env, "Protracker/Test Artist/by name.mod", _mod_bytes(b"by name"))
    _changelog(env, today - _dt.timedelta(days=1), "  added: Protracker/Test Artist/by name.mod\n")
    for k in range(40):                              # an old archive the refresh must not list
        _changelog(env, today - _dt.timedelta(days=100 + k), "  added: Protracker/x/old.mod\n")
    srv.listed.clear()
    res = await mc.refresh()
    assert res.get("error") is None and fulls == []
    assert mc.entry("Protracker/Test Artist/by name.mod") is not None
    assert srv.listed == [], srv.listed       # (nor the new tune's folder: its size by name)
    assert mc.entry("Protracker/Test Artist/by name.mod")[0] == len(_mod_bytes(b"by name"))
    # no SIZE: the folders are listed, the result is the same
    srv.size = False
    _serve(env, "Protracker/Test Artist/listed.mod", _mod_bytes(b"listed"))
    _changelog(env, today, "  added: Protracker/Test Artist/listed.mod\n", root=True)
    srv.listed.clear()
    res = await mc.refresh()
    assert res.get("error") is None and fulls == []
    assert mc.entry("Protracker/Test Artist/listed.mod") is not None
    assert any("changelogs" in p for p in srv.listed)


@pytest.mark.parametrize("order", ["chain", "swap"])
@pytest.mark.asyncio
async def test_rename_chains_and_swaps_keep_every_file_and_its_data(env, monkeypatch, order):
    """A rename onto a name another rename leaves (and a swap through a
    temporary name): every file stays in the index, and each tune's rating
    and plays land on its own new id."""
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    _strict()
    store = env["store"]
    a = "Protracker/Test Artist/other song.mod"
    b = "Protracker/Test Artist/tiny song.mod"
    b2 = "Protracker/Test Artist/tiny song_z.mod"
    store.set_rating(_tid(a), 5)
    store.record_play(_tid(a))
    store.set_rating(_tid(b), 2)
    rows = int(mc.index_info()["rows"])
    size_a, size_b = mc.entry(a)[0], mc.entry(b)[0]
    if order == "chain":                      # b → b2, then a → b's old name
        _move_served(env, b, b2)
        _move_served(env, a, b)
        text = (f"renamed: {b}\n     to: tiny song_z.mod\n"
                f"renamed: {a}\n     to: tiny song.mod\n")
        want = {b2: (size_b, 2), b: (size_a, 5)}
    else:                                     # a ↔ b through a temporary name
        tmp = "Protracker/Test Artist/tmp.mod"
        _move_served(env, a, tmp)
        _move_served(env, b, a)
        _move_served(env, tmp, b)
        text = (f"renamed: {a}\n     to: tmp.mod\n"
                f"renamed: {b}\n     to: other song.mod\n"
                f"renamed: {tmp}\n     to: tiny song.mod\n")
        want = {a: (size_b, 2), b: (size_a, 5)}
    _changelog(env, _dt.date.today(), text, root=True)
    res = await mc.refresh()
    assert res.get("error") is None and fulls == [], res
    assert int(mc.index_info()["rows"]) == rows                    # nothing lost
    for rel, (size, rating) in want.items():
        assert mc.entry(rel)[0] == size, rel
        assert store.get_track(_tid(rel)) is not None, rel
        assert store.get_rating(_tid(rel)) == rating, rel
    tune_a_now = next(r for r, (_s, rt) in want.items() if rt == 5)
    assert store.get_play_stats(_tid(tune_a_now))["count"] == 1


@pytest.mark.asyncio
async def test_a_grown_changelog_applies_only_its_new_lines(env, monkeypatch):
    """A chain read in the morning (add, then move) is not replayed when the
    day's file grows: only the new lines are read — no full download."""
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    _strict()
    today = _dt.date.today()
    _serve(env, "Protracker/Test Artist/fresh.mod", _mod_bytes(b"fresh"))
    _move_served(env, "Protracker/Test Artist/fresh.mod", "Protracker/Someone/fresh.mod")
    morning = ("  added: Protracker/Test Artist/fresh.mod\n"
               "  moved: Protracker/Test Artist/fresh.mod\n     to: Protracker/Someone\n")
    _changelog(env, today, morning, root=True)
    assert (await mc.refresh()).get("error") is None
    assert mc.entry("Protracker/Someone/fresh.mod") is not None
    _serve(env, "Protracker/Test Artist/later.mod", _mod_bytes(b"later"))
    _serve(env, "Protracker/Test Artist/half.mod", _mod_bytes(b"half"))
    # (a last line still being written is not applied until it ends: the
    # root's copy may grow)
    _changelog(env, today, morning + "  added: Protracker/Test Artist/later.mod\n"
                                      "  added: Protracker/Test Artist/half.mod", root=True)
    res = await mc.refresh()
    assert res.get("error") is None and fulls == [], res
    assert mc.entry("Protracker/Test Artist/later.mod") is not None
    assert mc.entry("Protracker/Test Artist/half.mod") is None
    assert mc.entry("Protracker/Someone/fresh.mod") is not None
    # Rewritten (its beginning changed): read again whole, in replay mode.
    _serve(env, "Protracker/Test Artist/zed.mod", _mod_bytes(b"zed"))
    _changelog(env, today, "deleted: Protracker/Test Artist/readme.txt\n" + morning
               + "  added: Protracker/Test Artist/later.mod\n"
               + "  added: Protracker/Test Artist/half.mod\n"
               + "  added: Protracker/Test Artist/zed.mod\n", root=True)
    res = await mc.refresh()
    assert res.get("error") is None and fulls == [], res
    assert mc.entry("Protracker/Test Artist/readme.txt") is None
    assert mc.entry("Protracker/Test Artist/zed.mod") is not None
    assert mc.entry("Protracker/Test Artist/half.mod") is not None


@pytest.mark.asyncio
async def test_days_the_full_index_holds_are_replayed_leniently(env, monkeypatch):
    """Right after a full index, the changelogs of the days it may already
    hold are read again: chains in them (add → move, move → move, move →
    delete) are taken as applied, not as a reason for another full index."""
    await _enable(env)                                   # index dated today: replay days
    fulls = _delta_ftp(env, monkeypatch)
    _changelog(env, _dt.date.today() - _dt.timedelta(days=1),
               "  added: Protracker/x/new.mod\n"
               "  moved: Protracker/x/new.mod\n     to: Protracker/- unknown\n     as: nameless.mod\n"
               "  moved: Protracker/y/a.mod\n     to: Protracker/z\n"
               "  moved: Protracker/z/a.mod\n     to: Protracker/w\n"
               "  moved: Protracker/q/gone.mod\n     to: Protracker/r\n"
               "deleted: Protracker/r/gone.mod\n")
    rows = int(mc.index_info()["rows"])
    res = await mc.refresh()
    assert res.get("error") is None and fulls == [], res
    assert int(mc.index_info()["rows"]) == rows
    assert mc.entry("Protracker/- unknown/nameless.mod") is not None


@pytest.mark.parametrize("trigger", ["big", "monthly", "drift"])
@pytest.mark.asyncio
async def test_a_full_index_keeps_the_user_data_of_moved_tunes(env, monkeypatch, trigger):
    """The moves the changelogs make are followed even when the full index
    is downloaded instead (a reorganisation day, the monthly check, a count
    that doesn't add up)."""
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch, count=len(SERVED))
    _strict()
    await mc.refresh()                                   # the banner's baseline
    store = env["store"]
    old, new = "Protracker/- unknown/nameless.mod", "Protracker/Someone Known/nameless.mod"
    store.set_rating(_tid(old), 4)
    store.create_playlist("pl", "Mine", track_ids=[_tid(old)])
    _move_served(env, old, new)
    served = dict(SERVED)
    served[new] = served.pop(old)
    _zip(env["zip"], served)                             # what the full download brings
    text = f"  moved: {old}\n     to: Protracker/Someone Known\n"
    if trigger == "big":
        monkeypatch.setattr(mc, "_DELTA_MAX_FILE_BYTES", 10)
    elif trigger == "monthly":
        mc._set_info(mc.db_path(), {"full_at": time.time() - 31 * DAY})
        mc._bump_db_gen()
    elif trigger == "drift":
        env["srv"].welcome = "Currently there are 99999 modules online in 9 different formats,"
    _changelog(env, _dt.date.today(), text, root=True)
    res = await mc.refresh()
    assert res.get("error") is None, res
    assert len(fulls) == 1, trigger
    assert store.get_track(_tid(new)) is not None and store.get_track(_tid(old)) is None
    assert store.get_rating(_tid(new)) == 4, trigger
    assert store.get_playlist("pl")["track_ids"] == [_tid(new)]
    assert mc.status()["last_update"]["reason"]


def test_the_monthly_check_never_follows_a_full_index():
    """With a monthly schedule every update would be the monthly check: it
    comes only after an update from the changelogs."""
    now = time.time()
    base = {"cl_since": _dt.date.today().isoformat(), "full_at": str(now - 31 * DAY)}
    import unittest.mock as um
    with um.patch.object(mc, "has_index", lambda: True):
        assert mc._full_reason({**base, "last_kind": "full"}, now) is None
        assert mc._full_reason({**base, "last_kind": "delta"}, now) == "the monthly full check"


@pytest.mark.asyncio
async def test_a_scope_change_follows_pending_moves_first(env, monkeypatch):
    await _enable(env)
    _delta_ftp(env, monkeypatch)
    _strict()
    store = env["store"]
    old, new = "Protracker/- unknown/nameless.mod", "Protracker/Someone Known/nameless.mod"
    store.set_rating(_tid(old), 3)
    _move_served(env, old, new)
    _changelog(env, _dt.date.today(), f"  moved: {old}\n     to: Protracker/Someone Known\n",
               root=True)
    real = mc._move_tracks

    async def _boom(moves, changed=frozenset()):
        raise RuntimeError("interrupted")
    monkeypatch.setattr(mc, "_move_tracks", _boom)
    await mc.refresh()
    assert mc._pending_moves(mc.index_info()) == {old: new}
    monkeypatch.setattr(mc, "_move_tracks", real)
    await mc.set_scope(mc.SCOPE_ALL)
    assert store.get_rating(_tid(new)) == 3 and store.get_track(_tid(new)) is not None
    assert mc._pending_moves(mc.index_info()) == {}


@pytest.mark.asyncio
async def test_pending_moves_are_given_up_after_a_few_failures(env, monkeypatch):
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    _strict()
    old, new = "Protracker/- unknown/nameless.mod", "Protracker/Someone Known/nameless.mod"
    _move_served(env, old, new)
    _changelog(env, _dt.date.today(), f"  moved: {old}\n     to: Protracker/Someone Known\n",
               root=True)

    async def _boom(moves, changed=frozenset()):
        raise RuntimeError("broken move")
    monkeypatch.setattr(mc, "_move_tracks", _boom)
    errors = [(await mc.refresh()).get("error") for _ in range(mc._PENDING_MOVE_TRIES)]
    assert all("broken move" in (e or "") for e in errors[:-1]), errors
    assert errors[-1] is None, errors                     # given up: the refresh goes on
    assert mc._pending_moves(mc.index_info()) == {}
    assert env["store"].get_track(_tid(new)) is not None and fulls == []


@pytest.mark.asyncio
async def test_a_move_keeps_hand_edits_and_refreshes_the_folder(env, monkeypatch):
    await _enable(env)
    _delta_ftp(env, monkeypatch)
    _strict()
    store = env["store"]
    old, new = "Protracker/- unknown/nameless.mod", "Protracker/Someone Known/nameless.mod"
    store.update_track_fields_batch([(_tid(old), {"title": "My Title", "album": "My Album",
                                                  "user_edited": ["album", "title"]})])
    calls = []
    real_after = mc._after_library_change

    async def _after():
        calls.append(1)
        await real_after()
    monkeypatch.setattr(mc, "_after_library_change", _after)
    _move_served(env, old, new)
    _changelog(env, _dt.date.today(), f"  moved: {old}\n     to: Protracker/Someone Known\n",
               root=True)
    assert (await mc.refresh()).get("error") is None
    t = store.get_track(_tid(new))
    assert t["title"] == "My Title" and t["album"] == "My Album"
    assert sorted(t["user_edited"]) == ["album", "title"]
    assert calls                                          # folder listings re-read


@pytest.mark.asyncio
async def test_an_updated_file_of_the_same_size_is_read_again(env, monkeypatch):
    """Modland replaced a file in place: its cached copy goes and the tune
    is listed afresh, so the next play reads the new file."""
    await _enable(env)
    _delta_ftp(env, monkeypatch)
    _strict()
    store = env["store"]
    rel = "Protracker/Test Artist/other song.mod"
    store.update_track_fields_batch([(_tid(rel), {"title": "read on a play",
                                                  "_merged": True, "merged": True})])
    dropped = []
    from soniqboom.core import remote_cache
    real_validate = remote_cache.RemoteCache.validate_size

    def _validate(self, share, path, size):
        dropped.append((path, size))
        return real_validate(self, share, path, size)
    monkeypatch.setattr(remote_cache.RemoteCache, "validate_size", _validate)
    monkeypatch.setattr(mc, "is_merged", lambda t: bool(t and t.get("title") == "read on a play"))
    _changelog(env, _dt.date.today(), f"updated: {rel}\n", root=True)
    assert (await mc.refresh()).get("error") is None
    assert any(p == "/" + rel for p, _s in dropped)
    assert store.get_track(_tid(rel))["title"] != "read on a play"


def test_an_index_date_that_cant_be_right_counts_as_the_build_day(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_MIN_INDEX_ROWS", 1)
    monkeypatch.setattr(mc, "_MIN_MUSIC_ROWS", 1)
    z = tmp_path / "old.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zi = zipfile.ZipInfo("allmods.txt", date_time=(1980, 1, 1, 0, 0, 0))
        zf.writestr(zi, _index_text(SERVED))
    info = mc.build_index_db(z, tmp_path / "idx.sqlite")
    today = _dt.date.today()
    assert info["cl_replay_to"] == today.isoformat()
    assert info["cl_since"] == (today - _dt.timedelta(days=mc._DELTA_OVERLAP_DAYS)).isoformat()


@pytest.mark.asyncio
async def test_switching_on_with_modland_unreachable_uses_the_kept_index(env, monkeypatch):
    await _enable(env)
    await mc.disable()
    fulls = _delta_ftp(env, monkeypatch)
    mc._set_info(mc.db_path(), {"built_at": time.time() - 2 * DAY})
    mc._bump_db_gen()

    def _down():
        raise ConnectionRefusedError("no route")
    monkeypatch.setattr(mc, "_delta_connect", _down)
    st = await mc.enable()
    for t in list(mc._tasks):
        if "refresh-loop" in (t.get_name() or ""):
            t.cancel()
    assert st["state"] == "ready" and st["tunes"] > 0 and fulls == []
    assert "no route" in (st.get("error") or "")
    assert mc._next_due() == pytest.approx(mc._status["last_attempt"] + 3600, abs=2)


@pytest.mark.asyncio
async def test_a_new_format_folder_at_modland_is_inside_the_index(env, monkeypatch):
    """A top folder the index doesn't know yet counts when the module tree has
    it (one SIZE); folders outside the module tree are still left out."""
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    _strict()
    _serve(env, "Brand New Format/Someone/first.bnf", b"\x00" * 64)
    _changelog(env, _dt.date.today(),
               "  added: Brand New Format/Someone/first.bnf\n"
               "  added: software/trackers/DOS/Some Tracker.zip\n", root=True)
    assert (await mc.refresh()).get("error") is None and fulls == []
    assert mc.entry("Brand New Format/Someone/first.bnf")[0] == 64
    assert mc.entry("software/trackers/DOS/Some Tracker.zip") is None


@pytest.mark.asyncio
async def test_an_archived_changelog_is_final_and_a_root_copy_archived_meanwhile_is_read(
        env, monkeypatch):
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    _strict()
    today = _dt.date.today()
    _serve(env, "Protracker/Test Artist/last line.mod", _mod_bytes(b"ll"))
    # an archived file is complete: its last line counts even without a newline
    _changelog(env, today - _dt.timedelta(days=1), "  added: Protracker/Test Artist/last line.mod")
    # the root's copy of today is archived between the SIZE and the RETR
    _serve(env, "Protracker/Test Artist/raced.mod", _mod_bytes(b"raced"))
    _changelog(env, today, "  added: Protracker/Test Artist/raced.mod\n")
    real = mc._changelog_files

    def _files(ftp, since="", until=""):
        got = real(ftp, since, until)
        d = today.isoformat()
        if d in got:
            got[d] = (f"/changelog_{d}.txt", got[d][1])         # (said to be in the root)
        return got
    monkeypatch.setattr(mc, "_changelog_files", _files)
    res = await mc.refresh()
    assert res.get("error") is None and fulls == [], res
    assert mc.entry("Protracker/Test Artist/last line.mod") is not None
    assert mc.entry("Protracker/Test Artist/raced.mod") is not None


@pytest.mark.asyncio
async def test_an_index_from_an_older_version_is_not_read_again(env, monkeypatch):
    """``cl_applied`` used to hold the size alone: a changelog of that size is
    not read again."""
    await _enable(env)
    _delta_ftp(env, monkeypatch)
    _strict()
    day = _dt.date.today().isoformat()
    data = _changelog(env, _dt.date.today(), "  added: Protracker/x/never.mod\n", root=True)
    mc._set_info(mc.db_path(), {"cl_applied": json.dumps({day: len(data)}), "cl_since": day})
    mc._bump_db_gen()
    before = len(_retrs(env))
    assert (await mc.refresh()).get("error") is None
    assert len(_retrs(env)) == before                     # nothing downloaded


@pytest.mark.asyncio
async def test_a_full_index_follows_only_the_moves_it_confirms(env, monkeypatch):
    """A move the changelogs announce but the downloaded index doesn't show
    (it is older) is not followed — the tune stays, with its data."""
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    _strict()
    store = env["store"]
    old, new = "Protracker/- unknown/nameless.mod", "Protracker/Someone Known/nameless.mod"
    store.set_rating(_tid(old), 4)
    _changelog(env, _dt.date.today(), f"  moved: {old}\n     to: Protracker/Someone Known\n",
               root=True)

    async def _no_changelogs(info):
        raise mc.NeedFullIndex("the changelogs can't be applied here")
    monkeypatch.setattr(mc, "_apply_changelogs", _no_changelogs)    # (nor roll the index forward)
    res = await mc.refresh()                                   # (the zip still has the old path)
    assert res.get("error") is None and len(fulls) == 1, res
    assert store.get_track(_tid(old)) is not None and store.get_rating(_tid(old)) == 4


@pytest.mark.asyncio
async def test_a_full_index_older_than_the_changes_is_brought_forward_at_once(env, monkeypatch):
    """Modland's full index can be a day behind the changelogs already
    applied: after downloading it, the changelogs bring it forward at once —
    a moved tune never goes back to its old place (nor loses its data)."""
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch)
    _strict()
    store = env["store"]
    old, new = "Protracker/- unknown/nameless.mod", "Protracker/Someone Known/nameless.mod"
    store.set_rating(_tid(old), 4)
    _move_served(env, old, new)
    _changelog(env, _dt.date.today(), f"  moved: {old}\n     to: Protracker/Someone Known\n",
               root=True)
    assert (await mc.refresh()).get("error") is None and fulls == []
    assert store.get_rating(_tid(new)) == 4
    mc._set_info(mc.db_path(), {"full_at": time.time() - 31 * DAY})   # → the monthly check
    mc._bump_db_gen()
    res = await mc.refresh()                                   # downloads the (older) fixture zip
    assert res.get("error") is None and len(fulls) == 1, res
    assert mc.entry(new) is not None and mc.entry(old) is None
    assert store.get_track(_tid(new)) is not None and store.get_track(_tid(old)) is None
    assert store.get_rating(_tid(new)) == 4
    assert mc.status()["last_update"]["kind"] == "full"


def test_reading_the_kept_zip_again_keeps_the_monthly_check_date(env, monkeypatch, tmp_path):
    monkeypatch.setattr(mc, "_MIN_INDEX_ROWS", 1)
    monkeypatch.setattr(mc, "_MIN_MUSIC_ROWS", 1)
    import shutil
    mc._refresh_index_files(download=False, local_zip=env["zip"])
    shutil.copyfile(env["zip"], mc.zip_path())
    old_full = time.time() - 20 * DAY
    mc._set_info(mc.db_path(), {"full_at": old_full})
    mc._bump_db_gen()
    mc._refresh_index_files(download=False)                     # (new classification rules)
    mc._bump_db_gen()
    assert float(mc.index_info()["full_at"]) == pytest.approx(old_full)


def test_leftover_half_written_files_are_removed_at_start(env):
    d = mc._dir()
    junk = [d / "modland_catalog.sqlite.abc123.delta", d / "modland_catalog.sqlite.abc.building",
            d / "modland_allmods.zip.abc.part", d / "modland_allmods.zip.abc.copy"]
    keep = d / "modland_played.json"
    for f in [*junk, keep]:
        f.write_bytes(b"x")
    mc._drop_leftovers()
    assert not any(f.exists() for f in junk) and keep.exists()


@pytest.mark.asyncio
async def test_a_banner_that_lags_a_busy_day_is_no_reason_for_a_full_index(env, monkeypatch):
    await _enable(env)
    fulls = _delta_ftp(env, monkeypatch, count=len(SERVED))
    _strict()
    await mc.refresh()                                         # baseline
    lines = ""
    for k in range(30):
        rel = f"Protracker/Busy Day/tune {k:02d}.mod"
        _serve(env, rel, _mod_bytes(f"busy {k}".encode()))
        lines += f"  added: {rel}\n"
    _changelog(env, _dt.date.today(), lines, root=True)        # the banner still says the old count
    res = await mc.refresh()
    assert res.get("error") is None and fulls == [], res
    assert mc.entry("Protracker/Busy Day/tune 29.mod") is not None


@pytest.mark.asyncio
async def test_a_subsonic_step_that_fails_keeps_the_moves_pending(env, monkeypatch, tmp_data_dir):
    from soniqboom.core import subsonic_state
    st = subsonic_state.reset_state(tmp_data_dir / "subsonic_state.json")
    await _enable(env)
    _delta_ftp(env, monkeypatch)
    _strict()
    old, new = "Protracker/- unknown/nameless.mod", "Protracker/Someone Known/nameless.mod"
    await st.star("u1", "song", [_tid(old)])
    env["store"].set_rating(_tid(old), 5)
    _move_served(env, old, new)
    _changelog(env, _dt.date.today(), f"  moved: {old}\n     to: Protracker/Someone Known\n",
               root=True)
    real = subsonic_state.SubsonicState.remap_track_ids

    async def _boom(self, mapping):
        raise OSError("disk full")
    monkeypatch.setattr(subsonic_state.SubsonicState, "remap_track_ids", _boom)
    assert "disk full" in ((await mc.refresh()).get("error") or "")
    assert mc._pending_moves(mc.index_info()) == {old: new}
    monkeypatch.setattr(subsonic_state.SubsonicState, "remap_track_ids", real)
    assert (await mc.refresh()).get("error") is None
    assert _tid(new) in st.starred("u1", "song")
    assert env["store"].get_rating(_tid(new)) == 5               # (redone: nothing lost)


@pytest.mark.asyncio
async def test_a_large_changelog_of_a_day_the_full_index_holds_is_read_once(env, monkeypatch):
    """A reorganisation day the downloaded index already holds is read (in
    replay mode) and recorded — never a reason for one full index after
    another."""
    await _enable(env)                                         # index dated today
    fulls = _delta_ftp(env, monkeypatch)
    monkeypatch.setattr(mc, "_DELTA_MAX_FILE_BYTES", 10)
    _changelog(env, _dt.date.today(), "  added: Protracker/Test Artist/tiny song.mod\n"
                                      "deleted: Protracker/x/gone long ago.mod\n", root=True)
    for _ in range(2):
        res = await mc.refresh()
        assert res.get("error") is None, res
    assert fulls == []


def test_the_banner_count():
    assert mc._banner_count("220-Currently there are 516143 modules online in 409 different") == 516143
    assert mc._banner_count("220-Currently there are 516,143 modules online") == 516143
    assert mc._banner_count("220 hello") is None
    assert mc._banner_count(None) is None
