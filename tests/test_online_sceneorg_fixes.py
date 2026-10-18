# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Polite crawling of volunteer archives: scene.org's crawl stops when the
server keeps refusing and honours ``Retry-After``; a folder answering 404
keeps its tunes and is named; folders that keep failing wait a day; a
package opened from a big ZIP whose HEAD tells no length is still read by
ranges; one index run at a start without an index; member rows written in
slices; Aminet's INDEX download pauses and gives up on a dead server;
Modland's oversized index is not downloaded three times."""
from __future__ import annotations

import asyncio
import http.server
import importlib.util
import threading
import time
import time as _real_time

import pytest

from soniqboom.core import online_catalog, online_sceneorg
from tests.test_online_sceneorg import _Handler, _file_gets, _listings, _play, env  # noqa: F401


class _Clock:
    """``time`` for online_sceneorg: sleeps are counted, not slept."""

    def __init__(self):
        self.slept = 0.0

    def sleep(self, s):
        self.slept += s

    def __getattr__(self, name):
        return getattr(_real_time, name)


def _refuse_after(monkeypatch, n: int, status: int, retry_after=None):
    real = _Handler._serve
    count = {"n": 0}

    def _serve(self, body):
        count["n"] += 1
        if count["n"] > n:
            type(self).log.append((self.command, self.path, None))
            self.send_response(status)
            if retry_after:
                self.send_header("Retry-After", str(retry_after))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return real(self, body)
    monkeypatch.setattr(_Handler, "_serve", _serve)


@pytest.mark.parametrize("status", [503, 429, 403])
@pytest.mark.asyncio
async def test_the_crawl_stops_when_scene_org_keeps_refusing(env, monkeypatch, status):
    cat = env["cat"]
    clock = _Clock()
    monkeypatch.setattr(online_sceneorg, "time", clock)
    _refuse_after(monkeypatch, 4, status)          # root + 3 sections answered
    _Handler.log = []
    st = await cat.enable()
    refused = len(_Handler.log) - 4
    # At most ``_BREAKER_FOLDERS`` folders' tries, never every folder twice over.
    assert refused <= online_sceneorg._BREAKER_FOLDERS * 3, (refused, st)
    assert "in a row" in (st.get("error") or ""), st
    assert cat._next_due() - time.time() > 3000         # an hour, not at once


@pytest.mark.asyncio
async def test_a_long_retry_after_ends_the_crawl_and_holds_the_next_try(env, monkeypatch):
    cat = env["cat"]
    clock = _Clock()
    monkeypatch.setattr(online_sceneorg, "time", clock)
    _refuse_after(monkeypatch, 4, 429, retry_after=7200)
    _Handler.log = []
    st = await cat.enable()
    assert len(_Handler.log) == 5, _Handler.log            # one refused request, then stop
    assert clock.slept < 5
    assert 7100 < cat._next_due() - time.time() <= 7200 + 5, st


@pytest.mark.asyncio
async def test_a_short_retry_after_is_waited_out(env, monkeypatch):
    cat = env["cat"]
    clock = _Clock()
    monkeypatch.setattr(online_sceneorg, "time", clock)
    real = _Handler._serve
    seen = {"once": False}

    def _serve(self, body):
        if self.path.rstrip("/").endswith("/groups") and not seen["once"]:
            seen["once"] = True
            type(self).log.append((self.command, self.path, None))
            self.send_response(503)
            self.send_header("Retry-After", "30")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return real(self, body)
    monkeypatch.setattr(_Handler, "_serve", _serve)
    st = await cat.enable()
    assert st["state"] == "ready" and st["tunes"] == 6, st
    assert clock.slept >= 30


@pytest.mark.asyncio
async def test_a_listed_folder_answering_404_keeps_its_tunes_and_is_named(env, monkeypatch):
    cat, store = env["cat"], env["store"]
    st = await cat.enable()
    assert st["state"] == "ready" and st["tunes"] == 6, st
    tid = cat.track_id("groups/the_jazz_cats/jc-pack.zip")
    real = _Handler._serve

    def _serve(self, body):
        if self.path.rstrip("/").endswith("/pub/music/groups"):
            type(self).log.append((self.command, self.path, None))
            self.send_error(404)
            return
        return real(self, body)
    monkeypatch.setattr(_Handler, "_serve", _serve)
    st = await cat.refresh(manual=True)
    assert st["tunes"] == 6 and store.get_track(tid) is not None, st
    assert st["unavailable_folders"] == ["groups"], st
    monkeypatch.setattr(_Handler, "_serve", real)
    st = await cat.refresh(manual=True)
    assert st["unavailable_folders"] == [] and st["tunes"] == 6


@pytest.mark.asyncio
async def test_folders_that_keep_failing_wait_a_day_doubling(env, monkeypatch):
    cat = env["cat"]
    monkeypatch.setattr(online_sceneorg, "time", _Clock())
    _Handler.broken = {"artists/4-mat", "artists/a-man", "groups/the_jazz_cats",
                       "compos/20_min_chiptune_compo", "disks/alchemy", "compilations"}
    await cat.enable()                                     # new failures: an hour
    waits = []
    for _ in range(2):
        st = await cat.refresh()
        assert "could not be listed" in (st.get("error") or ""), st
        waits.append(round((cat._next_due() - time.time()) / 3600))
    assert waits[0] >= 24 and waits[1] == 2 * waits[0], waits


@pytest.mark.asyncio
async def test_a_big_zip_whose_head_has_no_length_is_read_by_ranges(env, monkeypatch):
    from soniqboom.core import remote_zip
    cat = env["cat"]
    await cat.enable()
    monkeypatch.setattr(remote_zip, "_SUBSET_MIN_ARCHIVE", 4096)
    real = _Handler._serve

    def _serve(self, body):
        if self.command == "HEAD" and self.path.endswith("alchemy.zip"):
            type(self).log.append(("HEAD", self.path, None))
            self.send_response(200)
            self.end_headers()
            return
        return real(self, body)
    monkeypatch.setattr(_Handler, "_serve", _serve)
    _Handler.log = []
    tid = cat.track_id("disks/alchemy/alchemy.zip")
    path, ext = await _play(tid)
    assert ext == ".mod"
    gets = [r for p, r in _file_gets() if p.endswith("alchemy.zip")]
    assert gets and all(gets), gets                        # ranged only, never whole


class _FastAsyncio:
    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(s, *a, **kw):
        await asyncio.sleep(min(s, 0.05))


@pytest.mark.asyncio
async def test_a_start_without_an_index_runs_one_index_job(env, monkeypatch):
    cat, store = env["cat"], env["store"]
    monkeypatch.setattr(online_catalog, "asyncio", _FastAsyncio())
    monkeypatch.setattr(cat, "_rearm_not_before", 0.0)
    store.set_config(cat.cfg("enabled"), True)
    runs = []
    real = cat._update_and_apply

    async def counting(**kw):
        runs.append(kw)
        return await real(**kw)
    monkeypatch.setattr(cat, "_update_and_apply", counting)
    await cat.startup()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if runs and not cat.busy():
            break
    for _ in range(10):
        await asyncio.sleep(0.05)
    await cat.shutdown()
    assert len(runs) == 1, runs


@pytest.mark.asyncio
async def test_an_opened_packages_member_rows_are_written_in_slices(env, monkeypatch):
    cat, store = env["cat"], env["store"]
    st = await cat.enable()
    assert st["state"] == "ready", st
    cat.prepare_build()
    sizes = []
    real = store.upsert_tracks_batch

    def spy(rows):
        sizes.append(len(rows))
        return real(rows)
    monkeypatch.setattr(store, "upsert_tracks_batch", spy)
    n = 1200
    pkg = "compilations/big.zip"
    rows = [(f"{pkg}::track_{i:05d}.mod", 4000 + i, 0,
             {"approx": 9_000_000, "m": "2020-Jan-01 00:00:00", "member": f"track_{i:05d}.mod",
              "real": f"track_{i:05d}.mod", "first": i == 0, "members": n,
              "pkg_size": 9_000_000}) for i in range(n)]
    await cat._write_package_rows(pkg, rows)
    assert sum(sizes) == n and max(sizes) <= 250, sizes


def test_aminet_index_download_pauses_and_gives_up_on_a_dead_server(monkeypatch, tmp_path):
    import httpx
    from soniqboom.core import online_aminet
    slept = []
    monkeypatch.setattr(online_aminet.time, "sleep", lambda s: slept.append(s))
    attempts = []
    with httpx.Client(timeout=2.0) as client:
        real = client.stream

        def counting(*a, **kw):
            attempts.append(1)
            return real(*a, **kw)
        client.stream = counting
        with pytest.raises(httpx.TransportError):
            online_aminet._download_resumable(client, "http://127.0.0.1:8489/INDEX",
                                              tmp_path / "INDEX", {})
    assert len(attempts) == 2 and slept == [2.0], (attempts, slept)


def test_modland_downloads_an_oversized_index_once(monkeypatch, tmp_path):
    from soniqboom.core import modland_catalog
    spec = importlib.util.spec_from_file_location("_mc_copy", modland_catalog.__file__)
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)
    hits = []

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            body = b"x" * 4096
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mc, "INDEX_URL", f"http://127.0.0.1:{srv.server_address[1]}/all.zip")
        monkeypatch.setattr(mc, "_MAX_INDEX_BYTES", 1000)
        monkeypatch.setattr(mc.time, "sleep", lambda s: None)
        with pytest.raises(Exception) as ei:
            mc._download_index(tmp_path / "all.zip")
        assert "implausible" in str(ei.value)
        assert len(hits) == 1, hits
    finally:
        srv.shutdown()


@pytest.mark.asyncio
async def test_folders_that_failed_before_never_trip_the_breaker(env, monkeypatch):
    """Four folders in a row that keep failing (the breaker's count) are those
    folders' trouble: once they failed a listing, the next listing goes on
    past them and finds what changed elsewhere."""
    cat, store = env["cat"], env["store"]
    monkeypatch.setattr(online_sceneorg, "time", _Clock())
    monkeypatch.setattr(online_sceneorg, "_BREAKER_FOLDERS", 2)
    _Handler.broken = {"artists/4-mat", "artists/a-man"}       # adjacent, both broken
    st = await cat.enable()
    assert st.get("error"), st                       # never listed: not taken yet
    from tests.test_online_sceneorg import _write, _zip, _mod
    _write(env["web"], {"groups/new_group/new.zip": _zip({"n.mod": _mod(b"New")})})
    st = await cat.refresh(manual=True)
    assert "in a row" not in (st.get("error") or ""), st
    assert store.get_track(cat.track_id("groups/new_group/new.zip")) is not None, st


@pytest.mark.asyncio
async def test_modlands_refresh_schedule_survives_an_error(monkeypatch):
    from soniqboom.core import modland_catalog as mc
    calls = {"n": 0}

    def flaky(now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("index unreadable for a moment")
        return time.time() + 3600

    real_sleep = asyncio.sleep

    class _Aio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(s, *a, **kw):
            await real_sleep(0)
    monkeypatch.setattr(mc, "enabled", lambda: True)
    monkeypatch.setattr(mc, "_next_due", flaky)
    monkeypatch.setattr(mc, "asyncio", _Aio())

    async def short(seconds):
        await real_sleep(0)
    monkeypatch.setattr(mc, "_sleep_or_wake", short)
    task = asyncio.ensure_future(mc._refresh_loop())
    for _ in range(50):
        await asyncio.sleep(0)
        if calls["n"] >= 3:
            break
    assert not task.done() and calls["n"] >= 3
    task.cancel()


def test_modland_honours_refusals_and_retry_after(monkeypatch, tmp_path):
    from soniqboom.core import modland_catalog
    spec = importlib.util.spec_from_file_location("_mc_copy2", modland_catalog.__file__)
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)
    hits = []
    answer = {"code": 403, "hdr": {}}

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            self.send_response(answer["code"])
            for k, v in answer["hdr"].items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mc, "INDEX_URL", f"http://127.0.0.1:{srv.server_address[1]}/all.zip")
        monkeypatch.setattr(mc.time, "sleep", lambda s: None)
        with pytest.raises(Exception) as ei:
            mc._download_index(tmp_path / "all.zip")
        assert "refused" in str(ei.value) and len(hits) == 1
        answer.update(code=503, hdr={"Retry-After": "7200"})
        hits.clear()
        with pytest.raises(Exception) as ei:
            mc._download_index(tmp_path / "all.zip")
        assert len(hits) == 1
        mc._note_retry_after(ei.value)
        mc._status.update(error=str(ei.value), last_attempt=time.time())
        monkeypatch.setattr(mc, "index_info", lambda: {"built_at": time.time()})
        monkeypatch.setattr(mc, "has_index", lambda: True)
        assert mc._next_due() - time.time() >= 7100
    finally:
        srv.shutdown()
