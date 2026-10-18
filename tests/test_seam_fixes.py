# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where the sign-in durability, library-sync and folder-watcher work meet:

  * a backup import (and every other full library load) resets the web lists'
    aggregate cache, moves the library generation and tells open pages
    (``catalogue_changed``); a new server run in the same process gets a new
    run id and an empty cache;
  * stopping the folder watcher never blocks the event loop on an arm in
    progress, and a start after it arms every root again;
  * the journal is attached before anything that writes the store starts;
  * a scan that sends no progress (folder watcher, freshness poll) is marked
    ``silent`` in the connect snapshot, and the end of it sends the
    ``running: false`` frame a page needs (a page was shown it running, or it
    changed the library — a fast poll that only deletes included);
  * the source install's restart refuses requests and sign-ins first, quiesces
    the background work before sealing the journal, writes the sign-ins last,
    and the next start replays what a straggler wrote after the seal;
  * ``/api/auth/status`` shows the data folder only to an admin, or — while no
    admin exists — to the server's own host; the user-admin endpoints write
    users.json off the event loop, and two creates of one name can't both win;
  * a duration-only write moves the library generation (not the list cache).
"""
from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import os
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import library
from soniqboom.core import persistence, scanner
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore

sys.path.insert(0, str(Path(__file__).parent))


class FakeWS:
    """A connected page: what the library WebSocket broadcasts to it."""
    cookies: dict = {}

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        pass


@pytest.fixture
def page(monkeypatch):
    ws = FakeWS()
    monkeypatch.setattr(library, "_ws_clients", {ws})
    monkeypatch.setattr(library, "_ws_user_id", {})
    return ws


def _track(tid: str, artist: str) -> dict:
    return {"id": tid, "path": f"/m/{artist}/{tid}.mp3", "title": tid, "artist": artist,
            "album": f"{artist} Album", "format": "MP3", "duration": 60.0, "added_at": 1}


def _state(*tracks) -> dict:
    return {"tracks": {t["id"]: t for t in tracks}}


# ── F1: a backup import ─────────────────────────────────────────────────────

@pytest.fixture
def imported(tmp_data_dir, monkeypatch, page):
    """A library of A (3 tracks) + B (2), its counts cached; then the backup
    of A alone is imported through ``POST /api/admin/import``."""
    from soniqboom.api import admin
    from soniqboom.api import tracks as tracks_api
    monkeypatch.setattr(store_mod, "_store", None)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    a = [_track(f"a{i}", "AArtist") for i in range(3)]
    b = [_track(f"b{i}", "BArtist") for i in range(2)]
    persistence.populate_store(_state(*a, *b))
    app = FastAPI()
    for mod in (tracks_api, library, admin):
        app.include_router(mod.router, prefix="/api")
    app.dependency_overrides[admin._require_token] = lambda: "t"
    sbz = gzip.compress(json.dumps({"version": 2, "data": _state(*a)}).encode())

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            before = (await c.get("/api/library/counts")).json()
            gen0 = library.library_generation()
            r = await c.post("/api/admin/import",
                             files={"file": ("b.sbz", sbz, "application/octet-stream")})
            assert r.status_code == 200, r.text
            return {"before": before, "gen0": gen0, "import": r.json(),
                    "counts": (await c.get("/api/library/counts")).json(),
                    "artists": sorted((x.get("name") or x.get("artist")) for x in (await c.get("/api/library/artists")).json()
                                      if not x.get("label")),
                    "count": (await c.get("/api/tracks/count")).json(),
                    "gen1": library.library_generation()}
    out = asyncio.run(run())
    persistence.release_library()
    return out, page


def test_an_import_moves_the_generation_and_tells_open_pages(imported):
    out, page = imported
    assert out["before"]["tracks"] == 5
    assert out["import"]["imported"] == 3
    assert out["gen1"] != out["gen0"]                       # a reconnecting page re-reads
    assert out["count"]["count"] == 3
    assert out["counts"]["tracks"] == 3                     # not the cached 5
    ev = [m for m in page.sent if m.get("event") == "catalogue_changed"]
    assert ev and ev[-1]["lib_gen"] == out["gen1"]          # an open page re-reads now


def test_an_import_refreshes_the_lists_and_counts(imported):
    out, _ = imported
    assert out["artists"] == ["AArtist"]
    assert out["counts"]["artists"] == 1 and out["counts"]["albums"] == 1
    assert out["counts"]["visible"] == 3


def test_every_full_load_resets_the_list_cache(monkeypatch):
    monkeypatch.setattr(store_mod, "_store", None)
    monkeypatch.setattr(library, "_AGG_CACHE", {"artists": (0, [{"name": "stale"}])})
    monkeypatch.setattr(library, "_AGG_ETAGS", {"artists": ("e", b"[]", None)})
    epoch = library._AGG_EPOCH
    persistence.populate_store(_state(_track("n", "New")))
    assert library._AGG_CACHE == {} and library._AGG_ETAGS == {}
    assert library._AGG_EPOCH == epoch + 1


# ── F2: a new server run in the same process ────────────────────────────────

def test_a_new_server_run_gets_a_new_generation_and_an_empty_cache(monkeypatch):
    monkeypatch.setattr(library, "_AGG_CACHE", {"counts": (0, {"tracks": 5})})
    monkeypatch.setattr(library, "_AGG_ETAGS", {"counts": ("e", b"{}", None)})
    monkeypatch.setattr(library, "_BOOT_ID", library._BOOT_ID)
    g0 = library.library_generation()
    library.new_server_run()
    assert library.library_generation().split(".")[0] != g0.split(".")[0]
    assert library._AGG_CACHE == {} and library._AGG_ETAGS == {}
    from soniqboom import main
    src = inspect.getsource(main.startup)
    assert src.index("_run_epoch = begin_run()") < src.index("new_server_run()") \
        < src.index("init_persistence(data_dir)")


# ── F3: stopping the watcher during an arm ──────────────────────────────────

watchdog = pytest.importorskip("watchdog")


def _slow_observer_cls(arm_s: list):
    from watchdog.observers.api import BaseObserver, EventEmitter

    class SlowEmitter(EventEmitter):
        def on_thread_start(self):          # inotify: a watch per folder of a big tree
            time.sleep(arm_s[0])

        def queue_events(self, timeout):
            time.sleep(timeout)

    class SlowObserver(BaseObserver):
        def __init__(self, timeout=1.0):
            super().__init__(SlowEmitter, timeout=timeout)
    return SlowObserver


async def test_a_stop_during_an_arm_never_blocks_the_loop_and_a_start_rearms(tmp_path, monkeypatch):
    from soniqboom.core import watcher
    arm_s = [1.5]
    monkeypatch.setattr(watcher, "Observer", _slow_observer_cls(arm_s))
    root = tmp_path / "root"
    root.mkdir()
    await watcher.start([])
    gaps: list[float] = []

    async def heartbeat():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now
    hb = asyncio.create_task(heartbeat())
    arm = asyncio.create_task(watcher.add_root(str(root)))
    try:
        await asyncio.sleep(0.3)                          # the arm holds the observer's lock
        assert not arm.done()
        gaps.clear()
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(watcher.stop(), timeout=0.5)   # a short shutdown budget
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.1)
        assert max(gaps) < 0.1, f"event loop blocked {max(gaps):.2f}s by watcher.stop()"
        assert time.monotonic() - t0 < 1.0
        assert watcher._state.watches == {} and not watcher._state.enabled
        await arm                                          # the arm finds the watcher off
        assert watcher._state.watches == {}
        # A new run in the same process (the bundled app): the root is armed
        # on the new observer.
        arm_s[0] = 0.0
        await watcher.start([str(root)])
        rp = str(root.resolve())
        assert watcher.root_status(rp)["state"] == "watching"
        assert len(watcher._state.observer._emitters) == 1
    finally:
        hb.cancel()
        arm_s[0] = 0.0
        await watcher.stop()


# ── F4: the journal before the writers ──────────────────────────────────────

def test_the_journal_is_attached_before_anything_that_writes_the_store_starts():
    from soniqboom import main
    src = inspect.getsource(main.startup)
    attach = src.index("_get_store()._aof_append = _aof_writer.append")
    assert src.index("init_persistence(data_dir)") < attach
    for later in ("await watcher.start(", "await remote_freshness.start(",
                  "_start_share_health_monitor()", "await purge_junk_tracks()",
                  "await _init_network_shares()", "install_dup_regroup()",
                  "await modland_catalog.startup()", "retry_loop()"):
        assert attach < src.index(later), later


# ── F5 / F6: scans that send no progress ────────────────────────────────────

def _wav(path: Path, secs: float = 1.0) -> None:
    rate = 8000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", (i * 37) % 3000) for i in range(int(secs * rate))))


@pytest.fixture
def scans(tmp_path, monkeypatch, tmp_data_dir):
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())
    for name, val in (("_loud_running", 0), ("_busy_loud", False), ("_busy_changed", False),
                      ("_busy_shown", False), ("_busy_dirs", [])):
        monkeypatch.setattr(scanner, name, val, raising=False)

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _none)
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_schedule_folder_album_pass", lambda *a, **k: None)
    root = (tmp_path / "lib").resolve()
    (root / "A").mkdir(parents=True)
    _wav(root / "A" / "one.wav")
    return root


def _ends(page):
    return [m for m in page.sent if m.get("event") == "scan_progress" and not m.get("running")]


async def test_a_silent_scan_that_changed_the_library_ends_with_a_silent_frame(scans, page):
    root = scans
    assert await scanner._run_scan([str(root)]) is True     # no callback: the watcher's kind
    end = _ends(page)
    assert len(end) == 1
    assert end[0]["silent"] is True and end[0]["last_dirs"] == [str(root)]
    assert end[0]["lib_gen"] == library.library_generation()
    page.sent.clear()
    assert await scanner._run_scan([str(root)]) is False    # nothing changed, nobody shown
    assert _ends(page) == []


def test_the_snapshot_marks_a_silent_scan_and_its_end_is_sent(scans, page, monkeypatch):
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(library.router, prefix="/api")
    # a scan with no callback is running
    scanner._scan_began(None, ["/m"])
    monkeypatch.setattr(scanner, "_scan_count", 1)
    scanner._progress.running = True
    with TestClient(app).websocket_connect("/api/library/ws") as ws:
        snap = ws.receive_json()
    assert snap["snapshot"] is True and snap["running"] is True and snap["silent"] is True
    # it ends, changing nothing: the page that was told it runs gets the end
    monkeypatch.setattr(scanner, "_scan_count", 0)
    scanner._progress.running = False
    end = scanner._scan_ended(None, False)
    assert end == (True, ["/m"])
    asyncio.run(scanner._send_scan_end(end))
    frames = _ends(page)
    assert frames and frames[-1]["silent"] is True and frames[-1]["last_dirs"] == ["/m"]


def test_a_scan_with_progress_running_meanwhile_gets_a_plain_end(scans, page, monkeypatch):
    async def cb(_p):
        pass
    scanner._scan_began(cb, ["/loud"])
    scanner._scan_began(None, ["/quiet"])
    monkeypatch.setattr(scanner, "_scan_count", 2)
    scanner._progress.running = True
    assert scanner.get_progress().to_dict()["silent"] is False    # a badge is shown
    monkeypatch.setattr(scanner, "_scan_count", 1)
    assert scanner._scan_ended(cb, True) is None                  # its last frame said running
    assert scanner.get_progress().to_dict()["silent"] is True
    monkeypatch.setattr(scanner, "_scan_count", 0)
    scanner._progress.running = False
    end = scanner._scan_ended(None, False)
    assert end == (False, ["/quiet"])                             # → a plain completion frame
    asyncio.run(scanner._send_scan_end(end))
    end = _ends(page)[-1]
    assert not end.get("silent") and "lib_gen" in end


def test_an_end_sent_after_a_new_scan_began_is_folded_into_it(scans, page, monkeypatch):
    """A remote scan's end is sent after an await (its batch exit): a scan
    that started meanwhile must neither lose its folders to that frame nor
    be announced as finished by it."""
    scanner._scan_began(None, ["smb://share/A"])
    monkeypatch.setattr(scanner, "_scan_count", 0)
    end = scanner._scan_ended(None, True)                         # A changed the library
    assert end == (True, ["smb://share/A"])
    scanner._scan_began(None, ["/music/B"])                       # B starts during the await
    monkeypatch.setattr(scanner, "_scan_count", 1)
    scanner._progress.running = True
    asyncio.run(scanner._send_scan_end(end))
    assert _ends(page) == []                                      # no "running: false" while B runs
    monkeypatch.setattr(scanner, "_scan_count", 0)
    scanner._progress.running = False
    end = scanner._scan_ended(None, False)                        # B changed nothing itself
    assert end == (True, ["/music/B", "smb://share/A"]) or end == (True, ["smb://share/A", "/music/B"])
    asyncio.run(scanner._send_scan_end(end))
    frames = _ends(page)
    assert len(frames) == 1 and sorted(frames[0]["last_dirs"]) == ["/music/B", "smb://share/A"]


async def test_a_fast_poll_that_only_deletes_tells_open_pages(scans, page, tmp_path, monkeypatch):
    from _webdav_server import DavServer
    from soniqboom.core import remote_freshness as RF
    from soniqboom.core.filesource import register_source, remove_source
    from soniqboom.core.filesource_webdav import WebDAVFileSource
    try:
        from soniqboom.core import remote_dirstate
        with remote_dirstate._states_lock:
            remote_dirstate._states.clear()
    except ImportError:                                           # pragma: no cover
        pass
    lib = tmp_path / "share"
    old = time.time() - 7200
    for rel in ("Artist/Album/x1.mod", "Artist/Album/x2.mod", "Other/y1.mod", "Other/y2.mod"):
        p = lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_mod(rel.encode()))
    for p in sorted(lib.rglob("*"), key=lambda q: -len(q.parts)) + [lib]:
        os.utime(p, (old, old))
    srv = DavServer(lib).start()
    root = srv.url
    src = WebDAVFileSource(root + "/")
    register_source(root, src)
    monkeypatch.setattr(RF._reg, "source_lookup", lambda r: src)
    monkeypatch.setattr(RF._reg, "on_new_tracks", None)
    monkeypatch.setattr(RF._reg, "states", {root: RF.ShareState(scan_root=root)})
    monkeypatch.setattr(RF, "_save_state", lambda: None)
    try:
        for _ in range(3):                                        # full, full, fast
            await RF._poll_share(root, reason="test")
        n0 = store_mod._store.track_count()
        page.sent.clear()
        (lib / "Other" / "y2.mod").unlink()
        plan = await RF._poll_share(root, reason="test")
        assert plan["walk"]["mode"] == "fast" and plan["ghosts"] == 1 and not plan.get("new")
        assert store_mod._store.track_count() == n0 - 1
        end = _ends(page)
        assert len(end) == 1 and end[0]["silent"] is True and end[0]["last_dirs"] == [root]
        assert end[0]["lib_gen"] == library.library_generation()
        page.sent.clear()
        await RF._poll_share(root, reason="test")                 # nothing changed: no frame
        assert _ends(page) == []
    finally:
        remove_source(root)
        srv.stop()


def _mod(title: bytes) -> bytes:
    samples = b"square".ljust(22, b"\0") + struct.pack(">HBBHH", 512, 0, 64, 0, 512)
    samples += (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
    pat = bytearray(1024)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, 0])
    return (title[:20].ljust(20, b"\0") + samples + bytes([1, 127]) + bytes(128)
            + b"M.K." + bytes(pat) + bytes(([100] * 16 + [156] * 16) * 32))


# ── F7: the source install's restart ────────────────────────────────────────

def test_during_the_exec_restart_requests_get_503(monkeypatch):
    from fastapi.testclient import TestClient
    from soniqboom import main
    c = TestClient(main.app)
    assert c.get("/api/health").status_code == 200
    monkeypatch.setattr(main, "_exec_restarting", True)
    r = c.get("/api/health")
    assert r.status_code == 503 and r.headers["retry-after"] == "2"
    assert c.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 503
    # the Subsonic API: a Subsonic error envelope, in the format asked for
    r = c.get("/rest/ping.view")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/xml")
    assert b'status="failed"' in r.content and b'code="0"' in r.content
    assert r.headers["retry-after"] == "2"
    j = c.get("/rest/ping.view", params={"f": "json"}).json()["subsonic-response"]
    assert j["status"] == "failed" and j["error"]["code"] == 0
    assert "restarting" in j["error"]["message"]
    jp = c.get("/rest/stream", params={"f": "jsonp", "callback": "cb", "id": "x"})
    assert jp.text.startswith("cb(") and '"status":"failed"' in jp.text
    fp = c.post("/rest/ping.view", data={"u": "a", "p": "b", "f": "json"})      # formPost
    assert fp.json()["subsonic-response"]["error"]["code"] == 0
    assert c.get("/assets/js/lib_sync.js").status_code == 200       # the page itself loads


def test_a_sign_in_after_the_close_is_refused_and_every_issued_one_is_written(tmp_path, monkeypatch):
    from soniqboom.api import users as users_api
    from soniqboom.core import users as core_users
    st = core_users.UserStore(tmp_path, persist_sessions=True)
    st.create("admin", "pw-correct-horse", "admin")
    monkeypatch.setattr(core_users, "_instance", st)
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            ok = await c.post("/api/auth/login",
                              json={"username": "admin", "password": "pw-correct-horse"})
            st.close_sign_ins()
            late = await c.post("/api/auth/login",
                                json={"username": "admin", "password": "pw-correct-horse"})
            return ok, late
    ok, late = asyncio.run(run())
    assert ok.status_code == 200 and late.status_code == 503
    assert "sb_session" not in late.cookies
    with pytest.raises(core_users.SignInsClosed):
        st.issue_session(st.get_by_username("admin").id)
    assert st.flush_sessions()
    rows = json.loads((tmp_path / "sessions.json").read_text())["sessions"]
    assert len(rows) == 1                                   # the one issued before the close


def test_the_exec_restart_quiesces_first_and_writes_sign_ins_last(tmp_path, monkeypatch):
    from soniqboom import main
    from soniqboom.api import admin
    from soniqboom.core import game_titles, users as core_users, watcher
    order: list[str] = []

    class _Writer:
        late_tag = "late"

        def stop(self):
            order.append("aof")

        def close_intake(self):
            order.append("intake")

        def seal(self, budget=0.0):
            order.append(f"seal:{self.late_tag}")

    class _Users:
        def flush_sessions(self):
            order.append("sessions")
            return True

    class _Merger:
        def is_alive(self):
            return True

        def terminate(self):
            order.append("merger")

        def join(self, timeout=None):
            pass
    monkeypatch.setattr(main, "_aof_writer", _Writer())
    monkeypatch.setattr(main, "_merger_proc", _Merger())
    monkeypatch.setattr(core_users, "get_user_store", lambda: _Users())
    monkeypatch.setattr(game_titles, "cancel_downloads", lambda *a, **k: order.append("downloads") or [])
    monkeypatch.setattr(scanner, "shutdown_worker_pools", lambda *a, **k: order.append("pools") or True)
    root = tmp_path / "w"
    root.mkdir()

    async def run():
        await watcher.start([str(root)])
        health = main._start_share_health_monitor()
        assert watcher._state.enabled and not health.done()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, admin._graceful_pre_exec_flush, loop)
        await asyncio.sleep(0)
        return health
    try:
        health = asyncio.run(run())
    finally:
        main._health_stopping = False
    assert not watcher._state.enabled and watcher._state.observer is None
    assert health.done()
    assert order == ["downloads", "pools", "aof", "intake", "seal:late-exec", "merger", "sessions"]


def _lib_with(data: Path, *tids) -> None:
    (data / "library.json").write_text(json.dumps(
        {"tracks": {t: {"id": t, "path": f"/m/{t}.mp3", "title": t} for t in tids}}))


def test_records_written_after_the_seal_are_replayed_by_the_next_start(tmp_path, monkeypatch):
    from soniqboom.core.aof import AOFWriter
    data = tmp_path / "data"
    data.mkdir()
    _lib_with(data, "t1", "t2")

    async def old_image():
        w = AOFWriter(data / "library.aof")
        await w.start_auto_flush()
        w.append("record_play", id="t1")                  # journaled
        w.late_tag = "late-exec"
        w.cancel_flush_task()
        w.stop()
        w.close_intake()
        w.seal()
        w.append("record_play", id="t2")                  # a straggler after the seal
        return w
    asyncio.run(old_image())
    late = list(data.glob("library.aof.late-exec-*"))
    assert len(late) == 1
    with open(late[0], "ab") as f:                       # the exec cut a write in half
        f.write(b'{"op": "record_pl')
    saved = late[0].read_bytes()

    def boot() -> dict:
        state = persistence.load_snapshot(data)
        persistence.replay_aof(state, data)
        return {k: v.get("count") for k, v in (state.get("play_stats") or {}).items()}
    assert persistence.adopt_exec_late_records(data) == 1
    assert not late[0].exists()
    assert boot() == {"t1": 1, "t2": 1}
    # a start that stopped between the append and the removal: nothing twice
    late[0].write_bytes(saved)
    assert persistence.adopt_exec_late_records(data) == 1
    assert boot() == {"t1": 1, "t2": 1}
    # the journal is still well-formed for the next writer
    assert (data / "library.aof").read_bytes().endswith(b"\n")


def test_a_failed_exec_keeps_its_set_aside_records_in_the_journal(tmp_path, monkeypatch):
    """The exec fails and the usual stop follows: what was set aside after the
    seal is in the AOF before that stop (replayed once next start), and later
    stragglers go to a plain set-aside file, never replayed."""
    from soniqboom import main
    from soniqboom.api import admin
    from soniqboom.config import settings
    from soniqboom.core.aof import AOFWriter
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setenv("SONIQBOOM_DATA_DIR", str(tmp_path))
    _lib_with(tmp_path, "x", "y")
    w = AOFWriter(tmp_path / "library.aof")
    monkeypatch.setattr(main, "_aof_writer", w)
    w.late_tag = "late-exec"
    w.close_intake()
    w.seal()
    w.append("record_play", id="x")                      # between the seal and the exec
    admin._exec_failed_keep_records()                     # os.execv raised
    w.append("record_play", id="y")                      # during the stop that follows
    assert list(tmp_path.glob("library.aof.late-exec-*")) == []
    assert persistence.adopt_exec_late_records(tmp_path) == 0
    state = persistence.load_snapshot(tmp_path)
    persistence.replay_aof(state, tmp_path)
    assert {k: v.get("count") for k, v in state["play_stats"].items()} == {"x": 1}
    plain = list(tmp_path.glob("library.aof.late-*"))
    assert len(plain) == 1 and '"y"' in plain[0].read_text()
    # … which is why the stop that follows always writes its snapshot (it holds
    # "y", in memory): the journal had ended before it began
    assert main._snapshot_skippable(True, False, 1000, 32) is True
    assert main._snapshot_skippable(True, True, 1000, 32) is False
    src = inspect.getsource(main.shutdown)
    assert 'journal_ended = bool(_aof_writer) and bool(getattr(_aof_writer, "sealed", False))' in src
    assert src.index("journal_ended =") < src.index('_step("aof-flush"') \
        < src.index("_snapshot_skippable(aof_ok, journal_ended")


async def test_after_a_failed_exec_the_stop_snapshot_holds_the_late_writes(tmp_path, monkeypatch):
    """The snapshot the stop writes with an ended journal: what was written
    after the seal (in memory, set aside, never replayed) is in it, and nothing
    is counted twice at the next start."""
    from soniqboom.core.aof import AOFWriter
    monkeypatch.setattr(store_mod, "_store", None)
    _lib_with(tmp_path, "x", "y")
    persistence.init_persistence(tmp_path)
    store = store_mod.get_store()
    w = AOFWriter(tmp_path / "library.aof")
    store._aof_append = w.append
    store.record_play("x")                                 # journaled
    w.flush_sync()
    w.late_tag = "late-exec"
    w.close_intake()
    w.seal()
    store.record_play("y")                                 # after the seal
    w.retire_late_exec()                                   # the exec failed
    store.record_play("y")                                 # during the stop
    assert persistence.adopt_exec_late_records(tmp_path) == 1
    loop = asyncio.get_running_loop()
    assert await asyncio.to_thread(persistence.write_snapshot_sync, tmp_path, True, w, loop)
    state = persistence.load_snapshot(tmp_path)
    persistence.replay_aof(state, tmp_path)
    assert {k: v.get("count") for k, v in state["play_stats"].items()} == {"x": 1, "y": 2}
    persistence.release_library()


def test_the_next_start_loads_what_a_straggler_wrote_after_the_seal(tmp_path, monkeypatch):
    """The whole path: ``init_persistence`` (the exec'd image's start) adopts
    the set-aside records before it loads."""
    from soniqboom.core.aof import AOFWriter
    monkeypatch.setattr(store_mod, "_store", None)
    _lib_with(tmp_path, "s1")
    w = AOFWriter(tmp_path / "library.aof")
    w.late_tag = "late-exec"
    w.close_intake()
    w.seal()
    w.append("set_rating", id="s1", rating=4)
    persistence.init_persistence(tmp_path)
    assert store_mod.get_store().get_rating("s1") == 4
    assert list(tmp_path.glob("library.aof.late-exec-*")) == []


def test_the_exec_restart_refuses_requests_before_it_quiesces(monkeypatch):
    """``_do_restart``: requests and sign-ins are refused before the flush
    runs, and the flush runs before the exec."""
    from soniqboom import main
    from soniqboom.api import admin
    seen = []
    monkeypatch.setenv(main._EXEC_LEFTOVERS_ENV, "")      # (removed again afterwards)
    monkeypatch.setattr(main, "_exec_restarting", False)
    monkeypatch.setattr(admin, "_detect_app_bundle", lambda: None)
    monkeypatch.setattr(admin, "_graceful_pre_exec_flush",
                        lambda loop=None: seen.append(("flush", main._exec_restarting)))
    monkeypatch.setattr(admin.os, "execv", lambda *a: seen.append(("exec", main._exec_restarting)))
    closed = []
    monkeypatch.setattr(main._core_users, "get_user_store",
                        lambda: type("S", (), {"close_sign_ins": lambda self: closed.append(1)})())
    admin._do_restart(None)
    assert seen == [("flush", True), ("exec", True)] and closed == [1]


def test_an_import_refreshes_the_folder_views_and_the_subsonic_catalogue(tmp_path, monkeypatch):
    """Caches keyed on the store's write counters or on per-root track counts
    (folder browse, the Subsonic catalogue) follow an import that keeps the
    paths and counts but changes the tags."""
    from soniqboom.api import fstree, subsonic
    from soniqboom.core.data import path_hash
    monkeypatch.setattr(store_mod, "_store", None)
    monkeypatch.setattr(subsonic, "_ALBUM_LIST_CACHE", {})
    lib = tmp_path / "lib"
    (lib / "A").mkdir(parents=True)
    h = path_hash(str(lib))

    def state(title, album):
        tracks = {}
        for i, n in enumerate(("x1.mod", "x2.mod")):
            (lib / "A" / n).write_bytes(b"x")
            tracks[f"t{i}"] = {"id": f"t{i}", "path": str(lib / "A" / n), "title": f"{title}{i}",
                               "artist": "Art", "album": album, "format": "MOD", "duration": 60.0,
                               "added_at": 1, "scan_root_hash": h}
        return {"tracks": tracks, "scan_dirs": {h: {"path": str(lib), "path_hash": h}}}
    persistence.populate_store(state("Old", "OldAlbum"))
    s = store_mod.get_store()
    assert sorted(r.get("title") for r in fstree._store_recursive_tracks_under(s, lib / "A")) \
        == ["Old0", "Old1"]
    assert "oldalbum" in str(sorted(subsonic._catalogue(s).by_key)).lower()
    persistence.populate_store(state("New", "NewAlbum"))          # what admin_import does
    s.rebuild_indexes()
    assert sorted(r.get("title") for r in fstree._store_recursive_tracks_under(s, lib / "A")) \
        == ["New0", "New1"]
    albums = str(sorted(subsonic._catalogue(s).by_key)).lower()
    assert "newalbum" in albums and "oldalbum" not in albums


# ── /api/auth/status: the data folder ───────────────────────────────────────

def _status_app():
    from soniqboom.api import users as users_api
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    return app


async def _status(app, client_ip: str, cookie: str | None = None) -> dict:
    t = httpx.ASGITransport(app=app, client=(client_ip, 50000))
    async with httpx.AsyncClient(transport=t, base_url="http://t",
                                 cookies={"sb_session": cookie} if cookie else None) as c:
        return (await c.get("/api/auth/status")).json()


def test_the_data_folder_is_shown_to_an_admin_or_this_host_while_no_admin_exists(
        tmp_path, monkeypatch, tmp_data_dir):
    from soniqboom.core import users as core_users
    st = core_users.UserStore(tmp_path)
    monkeypatch.setattr(core_users, "_instance", st)
    app = _status_app()
    # first run: no admin — this host sees it, a LAN visitor doesn't
    assert asyncio.run(_status(app, "127.0.0.1"))["data_dir"] == str(tmp_data_dir.absolute())
    assert asyncio.run(_status(app, "192.168.1.20"))["data_dir"] is None
    admin = st.create("admin", "pw-correct-horse", "admin")
    reader = st.create("reader", "pw-correct-horse", "readonly")
    assert asyncio.run(_status(app, "127.0.0.1"))["data_dir"] is None      # anonymous
    tok_r, _ = st.issue_session(reader.id)
    assert asyncio.run(_status(app, "192.168.1.20", tok_r))["data_dir"] is None
    tok_a, _ = st.issue_session(admin.id)
    assert asyncio.run(_status(app, "192.168.1.20", tok_a))["data_dir"] == str(tmp_data_dir.absolute())


# ── users.json writes off the event loop ────────────────────────────────────

def test_user_admin_writes_run_off_the_event_loop(tmp_path, monkeypatch):
    from soniqboom.api import users as users_api
    from soniqboom.core import users as core_users
    st = core_users.UserStore(tmp_path)
    admin = st.create("admin", "pw-correct-horse", "admin")
    monkeypatch.setattr(core_users, "_instance", st)
    monkeypatch.setattr(users_api, "_registration_open", lambda store: True)
    on_loop: list[str] = []
    real_save = core_users.UserStore._save

    def spy_save(self):
        try:
            asyncio.get_running_loop()
            on_loop.append(threading.current_thread().name)
        except RuntimeError:
            pass
        return real_save(self)
    monkeypatch.setattr(core_users.UserStore, "_save", spy_save)
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    tok, _ = st.issue_session(admin.id)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     cookies={"sb_session": tok}) as c:
            r = await c.post("/api/users", json={"username": "bob", "password": "pw-correct-horse",
                                                 "role": "readonly"})
            assert r.status_code == 200, r.text
            uid = r.json()["user"]["id"]
            for req in (c.patch(f"/api/users/{uid}", json={"display_name": "Bob"}),
                        c.post(f"/api/users/{uid}/password", json={"new_password": "pw-other-horse"}),
                        c.put("/api/me/tokens", json={"listenbrainz_token": "x"}),
                        c.put("/api/me/subsonic-password", json={"password": "app-pw-1"}),
                        c.delete(f"/api/users/{uid}")):
                r = await req
                assert r.status_code == 200, r.text
            k = await c.post("/api/me/api-keys", json={"name": "app"})
            assert k.status_code == 200, k.text
            for req in (c.delete(f"/api/me/api-keys/{k.json()['id']}"),):
                r = await req
                assert r.status_code == 200, r.text
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as anon:
            r = await anon.post("/api/auth/register",
                                json={"username": "carol", "password": "pw-correct-horse"})
            assert r.status_code == 200, r.text
    asyncio.run(run())
    assert on_loop == [], f"users.json written on the event loop ({len(on_loop)}x)"


def test_two_creates_of_one_name_in_two_threads_make_one_account(tmp_path):
    from soniqboom.core import users as core_users
    st = core_users.UserStore(tmp_path)
    start = threading.Barrier(2)
    results: list = []

    def make():
        start.wait()
        try:
            results.append(st.create("dup", "pw-correct-horse", "readonly").id)
        except ValueError as e:
            results.append(str(e))
    ts = [threading.Thread(target=make) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(1 for u in st.list_users() if u.username == "dup") == 1
    assert sum(1 for r in results if "already taken" in str(r)) == 1


# ── lib_gen: duration-only writes ───────────────────────────────────────────

def test_a_duration_only_write_moves_the_generation_not_the_list_cache(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    s.upsert_track(_track("d1", "A"))
    seq0, g0 = library._current_agg_seq(), library.library_generation()
    s.update_track_fields("d1", {"duration": 123.0})
    assert library._current_agg_seq() == seq0               # no aggregate reads a duration
    assert library.library_generation() != g0               # the lists show it


def test_browse_responses_say_the_generation_they_were_served_at(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from soniqboom import main
    from soniqboom.core import users as core_users
    monkeypatch.setattr(core_users, "_instance", core_users.UserStore(tmp_path))  # no account: open
    c = TestClient(main.app)
    r = c.get("/api/library/counts")
    assert r.status_code == 200
    assert r.headers.get("x-sb-lib-gen") == library.library_generation()
    assert "x-sb-lib-gen" not in c.get("/api/health").headers


async def test_a_retry_waiting_when_its_root_is_removed_leaves_it_removed(tmp_path, monkeypatch):
    """A supervisor retry of a root that can't be watched, waiting (for the
    arm lock / its off-loop resolve) when the root is removed, must not add
    the root back — it would be polled again for good."""
    from soniqboom.core import watcher
    monkeypatch.setattr(watcher, "Observer", _slow_observer_cls([0.0]))
    root = tmp_path / "gone"
    root.mkdir()
    await watcher.start([])
    try:
        rp = str(root.resolve())
        watcher._state.roots.add(rp)
        watcher._set_failed(rp, "limit")                  # retrying, as after a refused arm
        lock = watcher._state.arm_lock
        await lock.acquire()                              # an arm in progress elsewhere
        retry = asyncio.create_task(watcher._arm(rp, retry=True))
        await asyncio.sleep(0.2)                          # the retry waits for the lock
        await watcher.remove_root(rp)
        lock.release()
        await retry
        assert rp not in watcher._state.roots and watcher.root_status(rp) is None
        assert rp not in watcher._state.watches
    finally:
        await watcher.stop()


def test_the_data_folder_in_recovery_only_to_this_host(tmp_path, monkeypatch, tmp_data_dir):
    """Recovery (users.json unreadable on an install that had accounts): the
    sign-in screen's hint shows the folder on the server's own host only."""
    from soniqboom.core import users as core_users
    core_users.UserStore(tmp_path).create("admin", "pw-correct-horse", "admin")
    for name in ("users.json", "users.json.bak"):
        (tmp_path / name).write_text("{ not json")
    st = core_users.UserStore(tmp_path)
    assert st.recovery_required
    monkeypatch.setattr(core_users, "_instance", st)
    app = _status_app()
    here = asyncio.run(_status(app, "127.0.0.1"))
    assert here["recovery_required"] is True and here["data_dir"] == str(tmp_data_dir.absolute())
    assert asyncio.run(_status(app, "10.0.0.7"))["data_dir"] is None


def test_creating_a_key_for_an_account_deleted_meanwhile_leaves_nothing(tmp_path):
    from soniqboom.core import users as core_users
    st = core_users.UserStore(tmp_path)
    st.create("admin", "pw-correct-horse", "admin")
    bob = st.create("bob", "pw-correct-horse", "readonly")
    st.delete(bob.id)
    with pytest.raises(KeyError):
        st.create_api_key(bob.id, "x")
    assert not any(uid == bob.id for uid, _k in st._by_api_key_hash.values())


def test_during_the_exec_restart_a_websocket_is_closed_service_restart(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from soniqboom import main
    monkeypatch.setattr(main, "_exec_restarting", True)
    with pytest.raises(WebSocketDisconnect) as e:
        with TestClient(main.app).websocket_connect("/api/library/ws") as ws:
            ws.receive_json()
    assert e.value.code == 1012
