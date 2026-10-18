# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Big packages of an online collection (scene.org here): a ZIP of 4 MB or
more is listed from its central directory by ranged reads, not fetched
whole — and must be named and inspected as a fetched copy would be: members
only their bytes name (``Antom`` → ``Antom.ftm``), archives and disk images
inside it.  An inner archive SoniqBoom couldn't open makes no "no tunes"
claim.  A member named by its bytes plays from a ranged subset
(``remote_zip.build_subset``).  Served by the scene.org test server (ranged
reads)."""
from __future__ import annotations

import io
import os
import struct
import threading
import zipfile
from pathlib import Path

import pytest

import test_online_sceneorg as SO
from test_online_empty_packages import _adf, _ftm, _mod
from soniqboom.core import catalogue, metadata, online_catalog, online_sceneorg, remote_cache
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore

needs_ftm = pytest.mark.skipif(".ftm" not in getattr(metadata, "OPENMPT_EXTRA_EXTS", {".ftm"}),
                               reason="the installed libopenmpt doesn't read Face The Music")
PAD = os.urandom(4_300_000)                      # (stored: the ZIP is over 4 MB)


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


FILES = {
    "disks/big/nested.zip": _zip({"pad.bin": PAD, "tunes.zip": _zip({"mod.Inner": _mod(b"Inner")})}),
    "disks/big/plain.zip": _zip({"pad/pad.bin": PAD, "Antom": _ftm(b"Big Antom")}),
    "disks/big/adf.zip": _zip({"pad.bin": PAD, "music.adf": _adf(b"mod.tune", _mod(b"Disk tune"))}),
    "disks/big/unopened.zip": _zip({"pad.bin": PAD, "inner.zip": _zip({"x.txt": b"x" * 5000})}),
}


@pytest.fixture
def env(tmp_data_dir, tmp_path, monkeypatch):
    from soniqboom.api import library, stream
    from soniqboom.config import settings
    store = TrackStore()
    monkeypatch.setattr(store_mod, "_store", store)
    cache = remote_cache.RemoteCache(tmp_data_dir / "remote-cache", 512)
    monkeypatch.setattr(remote_cache, "_cache", cache)
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)

    async def _left(*a, **k):
        pass
    monkeypatch.setattr(library, "broadcast_catalogue_tracks_left", _left, raising=False)
    web = tmp_path / "web"
    SO._write(web, FILES)
    SO.DATES.clear()
    H = SO._Handler
    monkeypatch.setattr(H, "log", [])
    monkeypatch.setattr(H, "web", web)
    for attr in ("broken", "cut", "no_ranges", "star_total"):
        monkeypatch.setattr(H, attr, set())
    srv = __import__("http.server").server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/pub/music"
    cat = online_sceneorg.SCENEORG_ONLINE
    monkeypatch.setattr(online_sceneorg, "BASE", base)
    monkeypatch.setattr(online_sceneorg, "CRAWL_GAP_S", 0.0)
    monkeypatch.setattr(cat, "crawl_path", lambda: tmp_path / "crawl.json")
    monkeypatch.setattr(cat, "root", base)
    monkeypatch.setattr(cat, "min_rows", 1)
    monkeypatch.setattr(cat, "_status", dict(cat._status, state="off", error=None))
    for attr, val in (("_job_lock", None), ("_tasks", set()), ("_job_tasks", set()),
                      ("_refresh_task", None), ("_local", threading.local()),
                      ("_pkg_locks", {}), ("_expanded_at", {}), ("_merge_tasks", {}),
                      ("_merging", set()), ("_run_stop", None), ("_empty_ids", None)):
        monkeypatch.setattr(cat, attr, val, raising=False)
    yield {"store": store, "cat": cat}
    srv.shutdown()
    catalogue.unregister_root(base)
    from soniqboom.core.filesource import remove_source
    remove_source(base)


async def _enable(env):
    st = await env["cat"].enable()
    assert st["state"] == "ready", st


def _whole_gets(rel):
    """GETs of the whole package (no Range)."""
    return [p for m, p, r in SO._Handler.log if m == "GET" and p.endswith(rel) and not r]


@pytest.mark.asyncio
async def test_a_big_zip_s_inner_archive_is_listed_and_plays(env):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("disks/big/nested.zip")
    path, ext = await SO._play(tid)
    assert ext == ".mod" and Path(path).read_bytes() == _mod(b"Inner")
    assert store.get_track(tid)["path"].endswith("nested.zip::tunes.zip::mod.Inner.mod")
    assert _whole_gets("nested.zip") == []                       # (by ranges, never whole)


@pytest.mark.asyncio
async def test_a_big_zip_s_disk_image_is_listed_and_plays(env):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("disks/big/adf.zip")
    path, ext = await SO._play(tid)
    assert ext == ".mod" and Path(path).read_bytes() == _mod(b"Disk tune")
    assert "adf.zip::music.adf::" in store.get_track(tid)["path"]


@needs_ftm
@pytest.mark.asyncio
async def test_a_big_zip_names_a_member_by_its_bytes_and_plays_it_from_a_subset(env):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("disks/big/plain.zip")
    path, ext = await SO._play(tid)
    assert ext == ".ftm" and Path(path).read_bytes() == _ftm(b"Big Antom")
    assert store.get_track(tid)["path"].endswith("plain.zip::Antom.ftm")
    assert _whole_gets("plain.zip") == []                         # (the subset, by ranges)


@pytest.mark.asyncio
async def test_an_inner_archive_that_wasnt_opened_makes_no_no_tunes_claim(env, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(online_catalog, "_NESTED_MAX", 100)     # (too big to open)
    await _enable(env)
    cat = env["cat"]
    with pytest.raises(HTTPException) as ei:
        await SO._play(cat.track_id("disks/big/unopened.zip"))
    assert ei.value.status_code == 410
    assert "a ZIP archive SoniqBoom didn't open" in ei.value.detail
    assert "no tunes in it" not in ei.value.detail
