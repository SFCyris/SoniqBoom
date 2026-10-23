# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Online collections (HVSC served locally): a tune HVSC moved keeps its
user data; one file the archive refuses leaves the collection online; a
refused index waits a day (doubling, kept over a restart); the refresh
schedule survives an index that can't be opened; a STIL that fails to
download doesn't refuse the refresh; the HVSC join loads its documents off
the event loop."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from fastapi import HTTPException

from soniqboom.core import online_catalog
from tests.test_online_hvsc import (LENS, SIDS, _Handler, _md5, _write_tree,  # noqa: F401
                                    env)


@pytest.mark.asyncio
async def test_a_tune_hvsc_moved_keeps_its_rating_plays_and_playlist_entry(env):
    cat, store = env["cat"], env["store"]
    st = await cat.enable()
    assert st["state"] == "ready" and st["tunes"] == 3, st
    old_rel, new_rel = "GAMES/A-F/Arkanoid.sid", "MUSICIANS/X/Xyz/Arkanoid.sid"
    old_id = cat.track_id(old_rel)
    added = store.get_track(old_id)["added_at"]
    store.set_rating(old_id, 5)
    store.record_play(old_id, uid="")
    pl = store.create_playlist("qa-pl", "QA", [old_id])
    time.sleep(1.1)
    docs = env["web"] / "C64Music" / "DOCUMENTS"
    out = ["[Database]"]
    for r in SIDS:
        if r != old_rel:
            out += [f"; /{r}", f"{_md5(r)}={LENS[r]}"]
    out += [f"; /{new_rel}", f"{_md5(old_rel)}={LENS[old_rel]}"]
    (docs / "Songlengths.md5").write_text("\n".join(out) + "\n", "latin-1")
    p = env["web"] / "C64Music" / new_rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(SIDS[old_rel])
    st = await cat.refresh(manual=True)
    new_id = cat.track_id(new_rel)
    assert store.get_track(old_id) is None and store.get_track(new_id) is not None, st
    assert store.get_rating(new_id) == 5
    assert store.get_play_stats(new_id, uid="").get("count") == 1
    ids = store.get_playlist(pl["id"])["track_ids"]
    assert ids in ([new_id], [{"id": new_id}]), ids
    assert store.get_track(new_id)["added_at"] == added        # not "Recently added"


@pytest.mark.parametrize("code", [403, 410, 451])
@pytest.mark.asyncio
async def test_one_file_the_archive_refuses_leaves_the_collection_online(env, monkeypatch, code):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    from soniqboom.core.filesource import get_source
    cat = env["cat"]
    st = await cat.enable()
    assert st["state"] == "ready", st
    bad, good = "MUSICIANS/H/Hubbard_Rob/Commando.sid", "GAMES/A-F/Arkanoid.sid"
    real_get = _Handler.do_GET

    def do_GET(self):
        if self.path.endswith(bad):
            type(self).log.append(("GET", self.path))
            self.send_error(code)
            return
        return real_get(self)
    monkeypatch.setattr(_Handler, "do_GET", do_GET)
    answers = []
    for rel in (bad, good, bad):
        tid = cat.track_id(rel)
        try:
            await stream._resolve_play_source(tid, await get_track(tid))
            answers.append(200)
        except HTTPException as exc:
            answers.append(exc.status_code)
    src = get_source(cat.root)
    assert answers[1] == 200, answers
    assert answers[0] != 200 and answers[2] != 200
    assert src is not None and not src.is_offline()
    # The refused file was asked for once, not again on its second play.
    assert sum(1 for m, p in _Handler.log if m == "GET" and p.endswith(bad)) == 1


def _shrink(env):
    docs = env["web"] / "C64Music" / "DOCUMENTS"
    for f in docs.iterdir():
        f.unlink()
    _write_tree(env["web"], ["GAMES/A-F/Arkanoid.sid"])


@pytest.mark.asyncio
async def test_a_refused_index_waits_a_day_doubling_and_over_a_restart(env):
    cat = env["cat"]
    st = await cat.enable()
    assert st["state"] == "ready" and st["tunes"] == 3, st
    time.sleep(1.1)
    _shrink(env)
    waits = []
    for _ in range(3):
        st = await cat.refresh()                              # scheduled: refused
        assert st.get("shrink_refused"), st
        waits.append(round((cat._next_due() - time.time()) / 3600))
    assert waits == [24, 48, 96], waits
    # A restart: the in-memory status is gone, the wait is not.
    cat._status.update(error=None, last_attempt=None, shrink_refused=False)
    assert round((cat._next_due() - time.time()) / 3600) == 96
    # "Refresh now" takes the shrunk index (the refusal is remembered), and a
    # success clears the record.
    st = await cat.refresh(manual=True)
    assert st["state"] == "ready" and st["tunes"] == 1, st
    assert cat._failure() == {}


def test_retry_after_and_refusals_are_read_from_answers():
    import httpx
    req = httpx.Request("GET", "http://x/")

    def err(code, **hdr):
        return httpx.HTTPStatusError("x", request=req,
                                     response=httpx.Response(code, headers=hdr, request=req))
    assert online_catalog.retry_after_of(err(429, **{"Retry-After": "600"})) == 600
    when = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 3600))
    assert 3500 < online_catalog.retry_after_of(err(503, **{"Retry-After": when})) <= 3600
    assert online_catalog.retry_after_of(err(503)) is None
    assert online_catalog.is_refusal(err(403)) and online_catalog.is_refusal(err(410))
    assert not online_catalog.is_refusal(err(429)) and not online_catalog.is_refusal(err(503))
    assert online_catalog.is_refusal(online_catalog.IndexShrank("x"))


@pytest.mark.asyncio
async def test_a_retry_after_holds_the_next_try(env, monkeypatch):
    import httpx
    cat = env["cat"]
    st = await cat.enable()
    req = httpx.Request("GET", "http://x/")
    exc = httpx.HTTPStatusError("busy", request=req, response=httpx.Response(
        503, headers={"Retry-After": str(5 * 3600)}, request=req))

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(cat, "fetch_index", boom)
    st = await cat.refresh()
    assert 4.9 * 3600 < cat._next_due() - time.time() <= 5 * 3600 + 5, st


class _FastAsyncio:
    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(s, *a, **kw):
        await asyncio.sleep(min(s, 0.02))


@pytest.mark.asyncio
async def test_the_refresh_schedule_survives_an_index_that_cannot_be_opened(env, monkeypatch):
    monkeypatch.setattr(online_catalog, "asyncio", _FastAsyncio())
    cat = env["cat"]
    st = await cat.enable()
    assert st["state"] == "ready", st
    loop_task = cat._refresh_task
    assert loop_task is not None and not loop_task.done()
    real_connect = sqlite3.connect
    state = {"fail": True}

    class _Sqlite:
        Error = sqlite3.Error
        OperationalError = sqlite3.OperationalError

        def connect(self, *a, **kw):
            if state["fail"] and "mode=ro" in str(a[0] if a else ""):
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(*a, **kw)
    monkeypatch.setattr(online_catalog, "sqlite3", _Sqlite())
    _Handler.log = []
    cat._db_gen += 1
    if cat._refresh_wake is not None:
        cat._refresh_wake.set()
    for _ in range(30):
        await asyncio.sleep(0.02)
    assert not loop_task.done()
    # No refresh (no download) because the index couldn't be read for a moment.
    assert not [p for m, p in _Handler.log if m == "GET"], _Handler.log
    assert cat._next_due() - time.time() > 200
    state["fail"] = False
    cat._db_gen += 1
    assert cat._next_due() - time.time() > 3600        # read again: the interval
    cat._refresh_task.cancel()


@pytest.mark.asyncio
async def test_a_stil_that_fails_to_download_keeps_the_one_in_use(env, monkeypatch):
    cat = env["cat"]
    st = await cat.enable()
    assert st["state"] == "ready", st
    time.sleep(1.1)
    docs = env["web"] / "C64Music" / "DOCUMENTS"
    rels = list(SIDS)
    (docs / "Songlengths.md5").write_text(
        "\n".join(["[Database]"] + [x for r in rels for x in (f"; /{r}", f"{_md5(r)}=1:00")])
        + "\n", "latin-1")
    (docs / "STIL.txt").unlink()                          # 404 now
    st = await cat.refresh()
    assert st["state"] == "ready" and not st.get("error"), st
    t = env["store"].get_track(cat.track_id("GAMES/A-F/Arkanoid.sid"))
    assert t["duration"] == 60.0                          # the new lengths are in
    assert "Breakout" in (t.get("stil") or "")            # the STIL in use stayed
    assert cat._failure() == {}


@pytest.mark.asyncio
async def test_the_hvsc_join_reads_its_documents_off_the_event_loop(monkeypatch, tmp_path,
                                                                    tmp_data_dir):
    from soniqboom.core import folder_album as fa
    from soniqboom.core import hvsc as hvsc_mod
    from soniqboom.core import hvsc_apply
    from soniqboom.core.store import TrackStore
    docs = tmp_path / "C64Music" / "DOCUMENTS"
    docs.mkdir(parents=True)
    md5 = "0" * 32
    (docs / "Songlengths.md5").write_text(f"[Database]\n; /A/x.sid\n{md5}=1:11\n", "latin-1")
    (docs / "STIL.txt").write_text("/A/x.sid\n  COMMENT: hi\n", "latin-1")
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)

    async def _refresh(ids):
        pass
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    s.upsert_tracks_batch([{"id": "s1", "path": "/elsewhere/x.sid", "title": "t",
                            "format": "SID", "duration": 180.0, "sid_md5": md5}])
    hv = hvsc_mod.HVSC()
    hv.configure(str(docs))
    monkeypatch.setattr(hvsc_mod, "get_hvsc", lambda: hv)
    loader_threads = []
    real = hv._ensure_loaded

    def spy():
        loader_threads.append(threading.current_thread() is threading.main_thread())
        return real()
    monkeypatch.setattr(hv, "_ensure_loaded", spy)
    res = await hvsc_apply.apply_hvsc_to_library()
    assert res["updated"] == 1
    assert loader_threads and loader_threads[0] is False       # first load: a worker thread
    assert s._tracks["s1"]["duration"] == 71.0


@pytest.mark.asyncio
async def test_an_absurd_retry_after_is_capped(env, monkeypatch):
    import httpx
    cat = env["cat"]
    await cat.enable()
    req = httpx.Request("GET", "http://x/")
    exc = httpx.HTTPStatusError("busy", request=req, response=httpx.Response(
        429, headers={"Retry-After": str(10 * 365 * 86400)}, request=req))

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(cat, "fetch_index", boom)
    await cat.refresh()
    assert cat._next_due() - time.time() <= online_catalog._RETRY_MAX_S + 5


@pytest.mark.asyncio
async def test_a_start_inside_a_failure_wait_runs_no_index_job(env, monkeypatch):
    cat, store = env["cat"], env["store"]
    store.set_config(cat.cfg("enabled"), True)          # on, no index yet
    store.set_config(cat.cfg("failure"), {"at": time.time(), "streak": 1, "refused": True,
                                          "error": "x", "retry_at": 0.0})
    runs = []

    async def counting(**kw):
        runs.append(kw)
        return {}
    monkeypatch.setattr(cat, "_update_and_apply", counting)
    monkeypatch.setattr(online_catalog, "asyncio", _FastAsyncio())
    await cat.startup()
    for _ in range(20):
        await asyncio.sleep(0.02)
    await cat.shutdown()
    assert runs == []
    assert cat._next_due() - time.time() > 20 * 3600
