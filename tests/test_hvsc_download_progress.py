# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the HVSC card shows of a check (``hvsc_download.progress``):
served by a local HTTP server that sends the two HVSC documents slowly (with
or without a Content-Length), stalls, refuses or is not there — never the
internet.  Covers the phases in order with the bytes growing to the
Content-Length, the library apply's matching counts and the end result, an
unknown size, a stall, an unreachable / refusing / failing mirror and offline
mode each worded on its own, already up to date, Check now in the
background with no double start, pending phases that never stay "under way",
the status endpoint carrying it, and Re-extract SID metadata reporting into
the same box."""
from __future__ import annotations

import asyncio
import http.server
import threading
import time

import pytest

from soniqboom.core import hvsc_download, netpolicy, online_hvsc
from test_online_hvsc import _md5, env  # noqa: F401  (fixture)


class _Mirror(http.server.BaseHTTPRequestHandler):
    """The two documents from ``web``, ``mode`` deciding how: "slow"
    (Content-Length, in ``chunks`` pieces ``delay`` s apart), "chunked" (no
    Content-Length, the connection closes at the end), "stall" (half, then
    nothing), or an HTTP status code to answer with."""
    web = None
    mode = "slow"
    chunks = 8
    delay = 0.05
    log: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).log.append(self.path)
        mode = type(self).mode
        if isinstance(mode, int):
            self.send_response(mode)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rel = self.path.split("/C64Music/", 1)[-1]
        p = type(self).web / "C64Music" / rel
        if not p.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = p.read_bytes()
        self.send_response(200)
        if mode != "chunked":
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", "Sat, 24 Oct 2026 10:00:00 GMT")
        self.end_headers()
        if mode == "stall":
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            time.sleep(3)
            return
        n = max(1, type(self).chunks)
        step = -(-len(body) // n)
        for i in range(0, len(body), step):
            self.wfile.write(body[i:i + step])
            self.wfile.flush()
            time.sleep(type(self).delay)


@pytest.fixture
def mirror(env, monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "hvsc_source", "online")
    for name, val in (("_task", None), ("_wake", None), ("_lock", None), ("_last_try", 0.0),
                      ("_running", False), ("_apply_runs", 0), ("_now_tasks", set()),
                      ("_progress", {"phase": "idle", "job": 0})):
        monkeypatch.setattr(hvsc_download, name, val)
    # A bigger song-length list than the fixture's three tunes, so the
    # download takes several chunks.
    docs = env["web"] / "C64Music" / "DOCUMENTS"
    sl = docs / "Songlengths.md5"
    sl.write_text(sl.read_text("latin-1") + "".join(
        f"; /MUSICIANS/X/Pad_{i}.sid\n{i:032x}=1:00\n" for i in range(4000)), "latin-1")
    _Mirror.web = env["web"]
    _Mirror.mode = "slow"
    _Mirror.chunks = 8
    _Mirror.delay = 0.05
    _Mirror.log = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Mirror)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(online_hvsc, "BASE", f"http://127.0.0.1:{srv.server_address[1]}/C64Music")
    yield {**env, "srv": srv, "songlengths_size": sl.stat().st_size}
    srv.shutdown()


class _Phases(dict):
    """The progress dict, noting every phase written (a sample every 10 ms
    misses one that lasts a moment)."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen = [self.get("phase")]

    def update(self, *a, **kw):
        super().update(*a, **kw)
        ph = dict(*a, **kw).get("phase")
        if ph and ph != self.seen[-1]:
            self.seen.append(ph)


async def _watch(coro) -> "tuple[object, list]":
    """Run ``coro``, sampling ``progress()`` every 10 ms: (result, samples)."""
    t = asyncio.ensure_future(coro)
    samples = []
    while not t.done():
        samples.append(hvsc_download.progress())
        await asyncio.sleep(0.01)
    samples.append(hvsc_download.progress())
    return t.result(), samples


def _phases(samples) -> list:
    out = []
    for s in samples:
        if not out or out[-1] != s["phase"]:
            out.append(s["phase"])
    return out


@pytest.mark.asyncio
async def test_a_download_reports_its_bytes_then_the_apply_then_the_end(mirror, monkeypatch):
    """The phases in order, the bytes growing to the Content-Length, the
    library apply's counts — and the result kept for after a restart."""
    store = mirror["store"]
    md5 = _md5("MUSICIANS/H/Hubbard_Rob/Commando.sid")
    store.upsert_tracks_batch([{"id": "own1", "path": "/m/Commando.sid", "title": "Commando",
                                "format": "SID", "duration": 180.0, "sid_md5": md5}])
    monkeypatch.setattr(online_hvsc.HVSC_ONLINE, "_loop", asyncio.get_running_loop())
    from soniqboom.core import folder_album

    async def _no_refresh(ids):                 # (the real one reads the data dir's caches)
        return None
    monkeypatch.setattr(folder_album, "refresh_album_caches", _no_refresh)
    noted = _Phases(phase="idle", job=0)
    monkeypatch.setattr(hvsc_download, "_progress", noted)
    res, samples = await _watch(hvsc_download.refresh(manual=True))
    assert res["tunes"] == 3 + 4000
    for _ in range(200):                        # the apply: a task on the loop
        samples.append(hvsc_download.progress())
        if samples[-1]["phase"] not in hvsc_download.ACTIVE:
            break
        await asyncio.sleep(0.01)
    samples = [s for s in samples if s["job"]]               # (the first: before it began)
    # Every phase, in order (written, not sampled).
    assert noted.seen == ["idle", "connecting", "downloading", "checking", "connecting",
                          "downloading", "installing", "loading", "applying", "done"], noted.seen
    sl = [s for s in samples if s["phase"] == "downloading" and s.get("doc") == "song lengths"]
    assert sl and all(s["total"] == mirror["songlengths_size"] for s in sl)
    dones = [s["done"] for s in sl]
    assert dones == sorted(dones) and len(set(dones)) >= 3            # it grew, chunk by chunk
    assert {s.get("step") for s in samples if s["phase"] == "downloading"} == {1, 2}
    end = samples[-1]
    assert len({s["job"] for s in samples}) == 1                       # one job throughout
    r = end["result"]
    assert r["kind"] == "download" and not r["unchanged"]
    assert r["scanned"] == 1 and r["updated"] == 1
    assert r["songlengths"] == 3 + 4000 and r["stil"] == 2
    assert store.get_track("own1")["duration"] == 279.0
    st = hvsc_download.status()
    assert st["last_result"]["scanned"] == 1 and st["progress"]["phase"] == "done"
    assert st["mirror"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_without_a_content_length_the_bytes_still_grow(mirror):
    _Mirror.mode = "chunked"
    res, samples = await _watch(hvsc_download.refresh(manual=True))
    assert res["tunes"] == 3 + 4000
    dl = [s for s in samples if s["phase"] == "downloading" and s.get("step") == 1]
    assert dl and all(s["total"] is None for s in dl)
    assert len({s["done"] for s in dl}) >= 3
    assert samples[-1]["phase"] == "done"                               # (no loop: no apply)


@pytest.mark.asyncio
async def test_a_stalled_download_fails_in_plain_words_and_is_retried_later(mirror, monkeypatch):
    monkeypatch.setattr(hvsc_download, "_STALL_S", 0.5)
    _Mirror.mode = "stall"
    t0 = time.monotonic()
    res, samples = await _watch(hvsc_download.refresh(manual=True))
    assert time.monotonic() - t0 < 2.5                                  # not the 3 s the mirror hangs
    assert "error" in res
    end = samples[-1]
    assert end["phase"] == "error" and end["error"] == "stalled"
    assert "stopped sending data" in end["message"]
    assert any(s["phase"] == "downloading" and s["done"] for s in samples)  # the half that came
    assert hvsc_download.state()["failure"]["streak"] == 1
    assert not (mirror["cat"].dir() / "DOCUMENTS" / "Songlengths.md5").exists()


@pytest.mark.asyncio
async def test_a_trickle_past_the_time_limit_is_stopped(mirror, monkeypatch):
    monkeypatch.setattr(hvsc_download, "_MAX_S", 0.15)
    _Mirror.delay = 0.1
    res, samples = await _watch(hvsc_download.refresh(manual=True))
    end = samples[-1]
    assert end["phase"] == "error" and end["error"] == "stalled" and "too slow" in end["message"]


@pytest.mark.parametrize("code, kind, words", [
    (404, "refused", "refused the download (HTTP 404)"),
    (503, "server", "could not answer right now (HTTP 503)"),
])
@pytest.mark.asyncio
async def test_an_answer_refusing_or_failing_is_worded_on_its_own(mirror, code, kind, words):
    _Mirror.mode = code
    await hvsc_download.refresh(manual=True)
    p = hvsc_download.progress()
    assert p["phase"] == "error" and p["error"] == kind and words in p["message"]
    assert p["kind"] == "download"                       # (the job's kind stays)


@pytest.mark.asyncio
async def test_an_unreachable_mirror_says_so(mirror, monkeypatch):
    s = __import__("socket").socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                           # nothing listens there
    monkeypatch.setattr(online_hvsc, "BASE", f"http://127.0.0.1:{port}/C64Music")
    await hvsc_download.refresh(manual=True)
    p = hvsc_download.progress()
    assert p["phase"] == "error" and p["error"] == "network"
    assert p["message"].startswith("Could not reach 127.0.0.1")


@pytest.mark.asyncio
async def test_offline_mode_is_its_own_end_state(mirror, monkeypatch):
    def refuse(target=None):
        raise netpolicy.Offline("offline")
    monkeypatch.setattr(netpolicy, "check", refuse)
    await hvsc_download.refresh(manual=True)
    p = hvsc_download.progress()
    assert p["phase"] == "offline" and p["error"] == "offline" and p["kind"] == "download"
    assert "Offline mode is on" in p["message"]
    assert _Mirror.log == []


@pytest.mark.asyncio
async def test_already_up_to_date(mirror):
    await hvsc_download.refresh(manual=True)
    online_hvsc._fallback_active = True                 # (in use: no apply again)
    from soniqboom.core.hvsc import get_hvsc
    get_hvsc().configure(str(mirror["cat"].dir() / "DOCUMENTS"))
    st = hvsc_download.state()
    assert st.get("last_modified")
    # The mirror answers a conditional GET with 304 now.
    _Mirror.mode = 304
    res = await hvsc_download.refresh(manual=True)
    assert res == {"unchanged": True}
    p = hvsc_download.progress()
    assert p["phase"] == "done" and p["unchanged"] and p["result"]["unchanged"]


@pytest.mark.asyncio
async def test_check_now_runs_in_the_background_and_never_twice(mirror):
    _Mirror.delay = 0.08
    first = hvsc_download.check_now()
    assert first["started"] and first["job"] == 1
    assert hvsc_download.progress()["phase"] == "starting"
    again = hvsc_download.check_now()
    assert again == {"busy": True, "job": 1}
    for _ in range(300):
        if hvsc_download.progress()["phase"] not in hvsc_download.ACTIVE:
            break
        await asyncio.sleep(0.02)
    p = hvsc_download.progress()
    assert p["phase"] == "done" and p["job"] == 1
    assert len([x for x in _Mirror.log if x.endswith("Songlengths.md5")]) == 1
    assert hvsc_download.check_now()["started"]                          # a new one, once over
    for t in list(hvsc_download._now_tasks):
        await t


@pytest.mark.asyncio
async def test_check_now_says_why_it_cannot_run(mirror, monkeypatch):
    from soniqboom.config import settings
    hvsc_download._save({"failure": {"at": time.time(), "streak": 1, "refused": True,
                                     "retry_at": time.time() + 3600}})
    res = hvsc_download.check_now()
    assert "asked to wait" in res["error"] and res["retry_at"] > time.time()
    monkeypatch.setattr(settings, "hvsc_source", "local")
    assert "local folder" in hvsc_download.check_now()["error"]
    assert hvsc_download.progress()["phase"] == "idle"


def test_pending_phases_never_stay_under_way(mirror, monkeypatch):
    """A "starting" the schedule never picks up goes back to idle; an apply
    announced but never run ends as an error — nothing says "under way" for
    ever."""
    monkeypatch.setattr(hvsc_download, "_PENDING_GRACE_S", 0.0)
    hvsc_download.mark_starting()
    assert hvsc_download._progress["phase"] == "starting"
    time.sleep(0.01)
    assert hvsc_download.progress()["phase"] == "idle"
    hvsc_download._set_progress(phase="loading")
    time.sleep(0.01)
    p = hvsc_download.progress()
    assert p["phase"] == "error" and p["error"] == "apply"
    # … but not while an apply runs.
    job = hvsc_download.apply_started()
    time.sleep(0.01)
    assert hvsc_download.progress()["phase"] == "loading"
    hvsc_download.apply_finished({"scanned": 0, "updated": 0}, job=job)
    assert hvsc_download.progress()["phase"] == "done"
    assert hvsc_download._apply_runs == 0


@pytest.mark.asyncio
async def test_start_announces_a_due_download(mirror):
    hvsc_download.start()                       # Save & reload with the download chosen
    assert hvsc_download.progress()["phase"] in ("starting", "connecting")
    for _ in range(300):
        if hvsc_download.progress()["phase"] not in hvsc_download.ACTIVE:
            break
        await asyncio.sleep(0.02)
    p = hvsc_download.progress()
    assert p["phase"] == "done" and p["job"] == 1                       # the same job
    hvsc_download._task.cancel()


@pytest.mark.asyncio
async def test_the_status_endpoint_and_check_now_endpoint(mirror):
    from soniqboom.api import admin
    r = await admin.hvsc_download_now(_tok="t")
    assert r["started"]
    s = await admin.hvsc_status(_tok="t")
    assert s["download"]["progress"]["phase"] in hvsc_download.ACTIVE
    assert s["download"]["mirror"] == "127.0.0.1"
    for _ in range(300):
        if not hvsc_download.busy():
            break
        await asyncio.sleep(0.02)
    s = await admin.hvsc_status(_tok="t")
    assert s["download"]["progress"]["phase"] == "done"
    assert s["download"]["last_result"]["songlengths"] == 3 + 4000


@pytest.mark.asyncio
async def test_reextract_reports_into_the_same_box(mirror, monkeypatch):
    from soniqboom.api import admin
    from soniqboom.core import folder_album
    from soniqboom.core.hvsc import get_hvsc

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(folder_album, "refresh_album_caches", _no_refresh)
    await hvsc_download.refresh(manual=True)                   # documents in place
    get_hvsc().configure(str(mirror["cat"].dir() / "DOCUMENTS"))
    md5 = _md5("GAMES/A-F/Arkanoid.sid")
    mirror["store"].upsert_tracks_batch([
        {"id": f"a{i}", "path": f"/m/a{i}.sid", "title": "A", "format": "SID",
         "duration": 180.0, "sid_md5": md5} for i in range(30)])
    seen = []
    real = hvsc_download.apply_reporter

    def _spy(job=None):
        report = real(job)
        return lambda *a, **k: (seen.append(a), report(*a, **k))
    monkeypatch.setattr(hvsc_download, "apply_reporter", _spy)
    res = await admin.hvsc_rescan_sids(_tok="t")
    assert res["scanned"] == 30 and res["updated"] == 30
    stages = [a[0] for a in seen]
    assert stages[0] == "matching" and ("matching", 30, 30) in seen and "saving" in stages
    p = hvsc_download.progress()
    assert p["phase"] == "done" and p["kind"] == "reextract"
    assert p["result"]["scanned"] == 30 and p["result"]["songlengths"] == 3 + 4000
    # While one runs, a second click is told so instead of a second pass.
    hvsc_download._set_progress(phase="applying")
    busy = await admin.hvsc_rescan_sids(_tok="t")
    assert busy["busy"]


def test_an_apply_never_writes_over_a_newer_job(mirror):
    """A Re-extract still running when a check begins: its progress and its
    end leave the check's job alone (its result is still kept)."""
    old = hvsc_download.apply_started(kind="reextract")
    report = hvsc_download.apply_reporter(old)
    report("matching", 5, 10, songlengths=3, stil=2)
    assert hvsc_download.progress()["phase"] == "applying"
    hvsc_download._new_job("connecting", kind="download")          # a check begins
    new = hvsc_download.progress()["job"]
    report("matching", 9, 10)
    hvsc_download.apply_finished({"scanned": 10, "updated": 1}, job=old)
    p = hvsc_download.progress()
    assert p["job"] == new and p["phase"] == "connecting" and "result" not in p
    assert hvsc_download.state()["last_result"]["scanned"] == 10
    assert hvsc_download._apply_runs == 0
    hvsc_download.apply_failed(RuntimeError("x"), job=old)           # (a late failure too)
    assert hvsc_download.progress()["phase"] == "connecting"


async def _until(pred, limit=5.0):
    t0 = time.monotonic()
    while not pred():
        assert time.monotonic() - t0 < limit, hvsc_download.progress()
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_an_apply_starting_during_a_download_leaves_the_card_to_it(mirror):
    """The startup apply (or the documents' own) begun while a download runs
    reports nothing: the card keeps showing the download, then its end."""
    t = asyncio.ensure_future(hvsc_download.refresh(manual=True))
    await _until(lambda: hvsc_download.progress()["phase"] == "downloading")
    job = hvsc_download.progress()["job"]
    quiet = hvsc_download.apply_started()            # (kind "apply", follow)
    assert quiet == 0
    hvsc_download.apply_reporter(quiet)("matching", 5, 100)
    hvsc_download.apply_finished({"scanned": 100, "updated": 0}, job=quiet)
    p = hvsc_download.progress()
    assert p["job"] == job and p["phase"] in ("downloading", "connecting", "checking")
    await t
    p = hvsc_download.progress()
    assert p["job"] == job and p["phase"] == "done" and p["kind"] == "download"


@pytest.mark.asyncio
async def test_a_local_folder_saved_mid_download_stops_it(mirror, monkeypatch):
    from soniqboom.config import settings
    _Mirror.chunks = 40
    t = asyncio.ensure_future(hvsc_download.refresh(manual=True))
    await _until(lambda: hvsc_download.progress()["phase"] == "downloading")
    t0 = time.monotonic()
    monkeypatch.setattr(settings, "hvsc_source", "local")
    res = await t
    assert time.monotonic() - t0 < 1.0                     # at the next chunk, not the end
    assert "changed" in res["error"]
    p = hvsc_download.progress()
    assert p["phase"] == "error" and p["error"] == "superseded"
    assert not (mirror["cat"].dir() / "DOCUMENTS" / "Songlengths.md5").exists()
