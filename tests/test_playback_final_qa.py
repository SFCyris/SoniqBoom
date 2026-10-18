# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Playback: WebKit tail probes and play starts, prewarms that give way to
plays, balanced cache pins, failed / cancelled in-flight setups, Range numbers
of any length, Dreamcast ``.dsf`` prewarms, work kept off the event loop and
the default executor, renderers that end with the server.

Everything runs in-process against temp files, fakes and throw-away child
processes (``/bin/sleep``, a Python stand-in renderer) — never a live server
or real data, and no request to another host.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from soniqboom.api import stream
from soniqboom.core import conversion_cache as cc
from soniqboom.core import forksafe, render_tmp


def _req(headers=None, *, method="GET", query=None, cookies=None):
    return types.SimpleNamespace(headers={k.lower(): v for k, v in (headers or {}).items()},
                                 method=method, query_params=dict(query or {}),
                                 cookies=cookies or {},
                                 client=types.SimpleNamespace(host="10.0.0.9"))


# ── Range numbers of any length (finding 7) ────────────────────────────────

@pytest.mark.parametrize("header,total,want", [
    ("bytes=" + "9" * 5000 + "-", 100, "unsatisfiable"),            # start past the end
    ("bytes=0-" + "9" * 5000, 100, [(0, 100)]),                      # last clamped
    ("bytes=-" + "9" * 5000, 100, [(0, 100)]),                       # suffix: the whole
    ("bytes=" + "0" * 5000 + "5-9", 100, [(5, 10)]),                 # leading zeros: exact
    ("bytes=" + "2" * 30 + "-" + "1" * 30, 100, None),               # last < first: ignored
    ("bytes=" + "1" * 30 + "-" + "2" * 30, 100, "unsatisfiable"),
    ("bytes=0-99", 100, [(0, 100)]),
], ids=["start-5000-digits", "last-5000-digits", "suffix-5000-digits", "zero-padded",
        "huge-last-below-first", "huge-both", "sane"])
def test_range_numbers_of_any_length_never_raise(header, total, want):
    assert stream._byte_range_set(header, total) == want


def test_a_huge_range_number_is_a_416_or_206_not_a_500():
    assert stream._parse_audio_range("bytes=" + "7" * 4400 + "-", 1000) is None      # → 416
    assert stream._parse_audio_range("bytes=10-" + "7" * 4400, 1000) == (10, 1000, True)


# ── WebKit's tail probe of a growing file (finding 1) ──────────────────────

async def _growing(tmp_path, written: int, total: int):
    f = tmp_path / "growing.wav"
    f.write_bytes(b"R" * written)
    return f, asyncio.Event(), asyncio.Event()


async def _call(resp, timeout=2.0):
    """Run an ASGI response; returns (status, headers, body) — or None when
    it hasn't finished within ``timeout``."""
    sent = {"status": None, "headers": {}, "body": b""}

    async def receive():
        await asyncio.sleep(3600)

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]
            sent["headers"] = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            sent["body"] += msg.get("body", b"")
    task = asyncio.ensure_future(resp({"type": "http", "method": "GET", "headers": []},
                                      receive, send))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        task.cancel()
        with pytest.raises(BaseException):
            await task
        return None
    return sent["status"], sent["headers"], sent["body"]


async def _start(resp):
    """Run an ASGI response whose client never reads an answer (a cancel of
    this coroutine is the client going away)."""
    async def receive():
        await asyncio.sleep(3600)

    async def send(msg):
        pass
    await resp({"type": "http", "method": "GET", "headers": []}, receive, send)


@pytest.mark.asyncio
async def test_a_tail_probe_of_a_growing_file_waits_for_its_bytes(tmp_path):
    """A closed range at the end of a file still being written waits for
    those bytes (as it always did) — never a 416 answered at once, which
    WebKit re-sends hundreds of times a second on a seek near the end."""
    total = 1_000_000
    f, complete, data = await _growing(tmp_path, 50_000, total)
    resp = await stream._growing_file_range_response(
        _req({"range": f"bytes={total - 49036}-{total - 1}"}), f, total, complete,
        media_type="audio/wav", data_event=data)
    assert await _call(resp, timeout=0.5) is None


@pytest.mark.asyncio
async def test_a_seek_past_the_written_part_still_waits_for_it(tmp_path):
    """An open-ended range (Chromium / Firefox seek) is not a tail probe: it
    waits for the bytes as before."""
    total = 1_000_000
    f, complete, data = await _growing(tmp_path, 50_000, total)
    for rng in (f"bytes={total - 49036}-", "bytes=600000-999999"):
        resp = await stream._growing_file_range_response(
            _req({"range": rng}), f, total, complete, media_type="audio/wav", data_event=data)
        assert await _call(resp, timeout=0.5) is None, rng


@pytest.mark.asyncio
async def test_a_tail_probe_of_written_bytes_is_a_206(tmp_path):
    total = 100_000
    f, complete, data = await _growing(tmp_path, total, total)
    resp = await stream._growing_file_range_response(
        _req({"range": f"bytes={total - 100}-{total - 1}"}), f, total, complete,
        media_type="audio/wav", data_event=data)
    status, headers, body = await _call(resp)
    assert status == 206 and len(body) == 100


@pytest.mark.parametrize("rng,pg,want", [
    (None, None, True), ("bytes=0-", None, True), ("bytes=0-1", None, True),
    ("bytes=0-0", None, True), ("bytes = 0-1", None, True),
    ("bytes=0-26460043", "a" * 32, True),         # WebKit's element, closed ranges
    ("bytes=0-262143", None, False),              # the player's next-track warmup
    ("bytes=24620-26411007", "a" * 32, False),    # a later range of the same play
    ("bytes=100-", "a" * 32, False),
])
def test_play_start_detection(rng, pg, want):
    req = _req({"range": rng} if rng else {}, query={"pg": pg} if pg else {})
    assert stream._is_play_start(req) is want


def test_a_webkit_probe_counts_as_moving_on():
    """A WebKit page's ``bytes=0-1`` of the next track makes the render it
    left stoppable at once (only ``bytes=0-`` did)."""
    tag = "webkit-tag-1"
    stream._CLIENT_PLAYS.pop(tag, None)
    try:
        assert stream._note_client_play(tag, "track-A")
        last_read = time.monotonic()
        req = _req({"range": "bytes=0-1"}, query={"pg": "b" * 32})
        assert stream._is_play_start(req)
        assert stream._note_client_play(tag, "track-B") is True     # another track
        assert stream._note_client_play(tag, "track-B") is False    # the same one again
        assert stream._moved_on(tag, "track-A", last_read)
    finally:
        stream._CLIENT_PLAYS.pop(tag, None)


# ── Prewarms give way to plays (finding 4) ─────────────────────────────────

def _entry(key, want_key=None, *, read=False):
    st = stream._listen_state()
    if read:
        st["last_read"] = time.monotonic()
        st["owners"].add("someone")
    return {"key": key, "track_id": key.split("__")[0], "kind": "live",
            "wanted": lambda by_play=False, k=want_key or key: cc.wanted(k, by_play=by_play),
            "listening": lambda: False, "state": st, "on_stop": None,
            "t": time.monotonic(), "stopped": False, "want_key": want_key}


def test_a_render_only_a_prewarm_wants_is_abandoned_for_a_play():
    want = cc.PrewarmWant()
    tok = cc.register_interest("pw-key__r1", want)
    try:
        e = _entry("pw-key__r1")
        assert stream._abandon_state(e, time.monotonic()) == (False, None)
        assert stream._abandon_state(e, time.monotonic(), by_play=True)[0] is True
        # A request that waits for it still keeps it.
        tok2 = cc.register_interest("pw-key__r1")
        try:
            assert stream._abandon_state(e, time.monotonic(), by_play=True)[0] is False
        finally:
            cc.drop_interest("pw-key__r1", tok2)
    finally:
        cc.drop_interest("pw-key__r1", tok)


@pytest.mark.asyncio
async def test_a_play_stops_a_prewarm_render_holding_the_last_slot_and_retires_it():
    """A prewarm's render holding the slot a play needs is stopped for it, and
    the prewarm is retired — it doesn't render the track again behind the play."""
    sem = stream._RenderSlots(1)
    want = cc.PrewarmWant()
    tok = cc.register_interest("pw2__r1", want)
    holder_started = asyncio.Event()

    async def prewarm_render():
        async with sem:
            holder_started.set()
            await asyncio.sleep(30)
    task = asyncio.ensure_future(prewarm_render())
    await holder_started.wait()
    entry = _entry("pw2__r1")
    stream._PREEMPTIBLE[task] = entry
    try:
        waiter = {"fut": asyncio.get_running_loop().create_future(), "bg": False}
        stream._preempt_abandoned_render(sem, waiter)
        assert entry["stopped"] and want.alive is False
        with pytest.raises(asyncio.CancelledError):
            await task
        # A prewarm waiting for a slot never stops it.
        sem = stream._RenderSlots(1)
        want2 = cc.PrewarmWant()
        tok2 = cc.register_interest("pw3__r1", want2)
        started = asyncio.Event()

        async def other():
            async with sem:
                started.set()
                await asyncio.sleep(30)
        t2 = asyncio.ensure_future(other())
        await started.wait()
        e2 = _entry("pw3__r1")
        stream._PREEMPTIBLE[t2] = e2
        stream._preempt_abandoned_render(sem, {"fut": None, "bg": True})
        assert not e2["stopped"] and want2.alive
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        cc.drop_interest("pw3__r1", tok2)
    finally:
        cc.drop_interest("pw2__r1", tok)
        stream._PREEMPTIBLE.pop(task, None)


@pytest.fixture
def prewarm_registry(monkeypatch):
    started = []

    async def fake_do_prewarm(track_id, track, subsong, prio, *, serve=None, want=None):
        started.append(track_id)
        await asyncio.sleep(3600)
    monkeypatch.setattr(stream, "_do_prewarm", fake_do_prewarm)
    before = dict(stream._prewarm_tasks)
    yield started
    for k, t in list(stream._prewarm_tasks.items()):
        if k not in before:
            t.cancel()
            stream._prewarm_tasks.pop(k, None)
            stream._prewarm_owner.pop(k, None)
            stream._prewarm_hover_only.discard(k)


@pytest.mark.asyncio
async def test_a_hover_prewarm_lapses(prewarm_registry, monkeypatch):
    monkeypatch.setattr(stream, "_HOVER_PREWARM_TTL_S", 0.2)
    tr = types.SimpleNamespace(path="/x/a.sid")
    stream._schedule_prewarm("hov-1", tr, None, stream.PRIO_AHEAD, "me", ".sid", hover=True)
    stream._schedule_prewarm("hov-2", tr, None, stream.PRIO_AHEAD, "me", ".sid", hover=True)
    # A look-ahead prewarm of hov-2 takes it over: no expiry for it.
    stream._schedule_prewarm("hov-2", tr, None, stream.PRIO_NEXT, "me", ".sid")
    t1 = stream._prewarm_tasks[stream._prewarm_key("hov-1", ".sid")]
    t2 = stream._prewarm_tasks[stream._prewarm_key("hov-2", ".sid")]
    await asyncio.sleep(0.5)
    assert t1.cancelled() or t1.done()
    assert stream._prewarm_key("hov-1", ".sid") not in stream._prewarm_tasks
    assert not t2.done()


@pytest.mark.asyncio
async def test_a_play_in_a_page_retires_that_pages_other_prewarms(prewarm_registry):
    page, other_page = "p" * 32, "q" * 32
    play = _req({}, query={"pg": page}, cookies={"sb_session": "s1"})
    who = stream._prewarm_requester(play, "s1", None, page)
    who_other = stream._prewarm_requester(play, "s1", None, other_page)
    tr = types.SimpleNamespace(path="/x/a.psf")
    for tid in ("A", "B", "C"):
        stream._schedule_prewarm(tid, tr, None, stream.PRIO_AHEAD, who, ".psf")
    stream._schedule_prewarm("D", tr, None, stream.PRIO_AHEAD, who_other, ".psf")
    stream._schedule_prewarm("C", tr, None, stream.PRIO_AHEAD, who_other, ".psf")
    await asyncio.sleep(0)
    tasks = {tid: stream._prewarm_tasks[stream._prewarm_key(tid, ".psf")] for tid in "ABCD"}
    assert stream._retire_page_prewarms(play, "s1", None, "A") == 1        # B only
    await asyncio.sleep(0)
    assert not tasks["A"].done()                 # the track it just started
    assert tasks["B"].cancelled()
    assert not tasks["C"].done()                 # another page wants it too
    assert not tasks["D"].done()                 # another page's
    assert stream._retire_page_prewarms(_req({}), "s1", None, "A") == 0    # no pg: nothing


# ── Cache pins are balanced (finding 5) ────────────────────────────────────

def test_a_pin_hold_releases_exactly_once():
    k = "pinhold-test__k"
    h = cc.PinHold(k)
    assert cc._pin_refs.get(k) == 1
    ts = [threading.Thread(target=h.release) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    h.release()
    assert k not in cc._pin_refs
    with pytest.raises(RuntimeError):
        with cc.PinHold(k):
            raise RuntimeError("boom")
    assert k not in cc._pin_refs
    with cc.PinHold(k) as kept:             # no error: handed on, still held
        pass
    assert cc._pin_refs.get(k) == 1
    kept.release()


def _fake_inflight(tmp_path, *, complete=False):
    wav = tmp_path / "inflight.partial.wav"
    wav.write_bytes(b"\0" * 4096)
    ev = asyncio.Event()
    if complete:
        ev.set()
    return {"setup_ready": asyncio.Event(), "wav_path": wav, "expected_size": 1_000_000,
            "complete_event": ev, "data_event": asyncio.Event(), "sample_rate": 48000,
            "channels": 2, "subscribers": 0, "listen": stream._listen_state(),
            "pump_task": None}


@pytest.mark.asyncio
async def test_ten_chunked_first_plays_cut_off_while_opening_leave_no_pin(tmp_path, monkeypatch):
    """The body of a chunked first play cancelled while it opens the file
    (the client left) used to keep its cache pin for good."""
    inflight = _fake_inflight(tmp_path)

    async def fake_get_or_start(**kw):
        return inflight
    monkeypatch.setattr(stream, "_get_or_start_inflight_wav", fake_get_or_start)
    opening = asyncio.Event()

    async def slow_open(path):
        opening.set()
        await asyncio.sleep(3600)
    monkeypatch.setattr(stream, "_open_fd_handle", slow_open)
    key = stream._inflight_cache_key("pinleak-1", None)
    for _ in range(10):
        opening.clear()
        req = _req({}, cookies={"sb_session": "s"})
        resp = await stream._serve_inflight_wav_body(
            req, None, tmp_path / "src.m4a", "pinleak-1", None, None, "alac", None)
        task = asyncio.ensure_future(_start(resp))
        await opening.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert cc._pin_refs.get(key, 0) == 0
    assert inflight["subscribers"] == 0


@pytest.mark.asyncio
async def test_a_500_after_the_pin_leaves_no_pin(tmp_path, monkeypatch):
    inflight = _fake_inflight(tmp_path)

    async def fake_get_or_start(**kw):
        return inflight
    monkeypatch.setattr(stream, "_get_or_start_inflight_wav", fake_get_or_start)

    async def boom(*a, **kw):
        raise HTTPException(500, "forced")
    monkeypatch.setattr(stream, "_growing_file_range_response", boom)
    monkeypatch.setattr(stream, "_chunked_growing_file_response", boom)
    key = stream._inflight_cache_key("pinleak-2", None)
    for rng in ({"range": "bytes=100-"}, {}):
        with pytest.raises(HTTPException) as ei:
            await stream._serve_inflight_wav_body(
                _req(rng, cookies={"sb_session": "s"}), None, tmp_path / "src.m4a",
                "pinleak-2", None, None, "alac", None)
        assert ei.value.status_code == 500
    assert cc._pin_refs.get(key, 0) == 0
    assert inflight["subscribers"] == 0


@pytest.mark.asyncio
async def test_a_huge_range_on_the_inflight_path_leaves_no_pin(tmp_path, monkeypatch):
    inflight = _fake_inflight(tmp_path)

    async def fake_get_or_start(**kw):
        return inflight
    monkeypatch.setattr(stream, "_get_or_start_inflight_wav", fake_get_or_start)
    key = stream._inflight_cache_key("pinleak-3", None)
    resp = await stream._serve_inflight_wav_body(
        _req({"range": "bytes=" + "9" * 5000 + "-"}), None, tmp_path / "src.m4a",
        "pinleak-3", None, None, "alac", None)
    status, headers, _ = await _call(resp)
    assert status == 416
    await asyncio.sleep(0.05)
    assert cc._pin_refs.get(key, 0) == 0


# ── In-flight setups that fail or are cancelled (findings 6 and 10) ─────────

@pytest.fixture
def unprobeable(monkeypatch, tmp_path):
    async def no_info(path):
        await asyncio.sleep(0.05)
        return None
    monkeypatch.setattr(stream, "_probe_source_info", no_info)

    async def no_ffmpeg(*a, **kw):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(stream.forksafe, "spawn", no_ffmpeg)
    track = types.SimpleNamespace(duration=0)
    yield track
    stream._INFLIGHT_TRANSCODES.pop("unprobeable-1", None)
    stream._INFLIGHT_TRANSCODES.pop("cancelled-1", None)


@pytest.mark.asyncio
async def test_concurrent_first_plays_of_an_unprobeable_file_all_get_its_415(unprobeable, tmp_path):
    async def one():
        try:
            await stream._get_or_start_inflight_wav(
                track_id="unprobeable-1", src_path=tmp_path / "broken.m4a", track=unprobeable,
                target_rate=None, target_channels_hint=None)
        except HTTPException as exc:
            return exc.status_code, exc.detail[:30]
        return "returned", None
    got = await asyncio.gather(*(one() for _ in range(4)))
    assert [g[0] for g in got] == [415] * 4 and len({g[1] for g in got}) == 1
    assert "unprobeable-1" not in stream._INFLIGHT_TRANSCODES


@pytest.mark.asyncio
async def test_a_setup_cancelled_half_way_leaves_no_stub(unprobeable, tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def hang(path):
        await gate.wait()
        return None
    monkeypatch.setattr(stream, "_probe_source_info", hang)
    kw = dict(track_id="cancelled-1", src_path=tmp_path / "x.m4a", track=unprobeable,
              target_rate=None, target_channels_hint=None)
    owner = asyncio.ensure_future(stream._get_or_start_inflight_wav(**kw))
    await asyncio.sleep(0.05)
    waiter = asyncio.ensure_future(stream._get_or_start_inflight_wav(**kw))
    await asyncio.sleep(0.05)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    gate.set()                          # the waiter set it up itself: the 415
    with pytest.raises(HTTPException) as ei:
        await asyncio.wait_for(waiter, 5)
    assert ei.value.status_code == 415
    assert "cancelled-1" not in stream._INFLIGHT_TRANSCODES
    # A later play doesn't wait for anything left behind.
    with pytest.raises(HTTPException):
        await asyncio.wait_for(stream._get_or_start_inflight_wav(**kw), 5)


# ── A Dreamcast .dsf's format=mp3 prewarm (finding 8) ──────────────────────

@pytest.mark.asyncio
async def test_a_dreamcast_dsf_prewarm_also_prepares_the_mp3(tmp_path, monkeypatch):
    rip = tmp_path / "jt_a.dsf"
    rip.write_bytes(b"PSF\x12" + b"\0" * 64)
    dsd = tmp_path / "song.dsf"
    dsd.write_bytes(b"DSD " + b"\0" * 64)
    wav = tmp_path / "render.wav"
    wav.write_bytes(b"\0" * 64)
    calls = []

    async def fake_render(track_id, file_path, ext, subsong, **kw):
        out = cc.render_results.get()
        if out is not None:
            out.append((f"{track_id}__r1", "psf", wav))
    monkeypatch.setattr(stream, "_do_prewarm_render", fake_render)

    async def fake_get_or_render(**kw):
        calls.append(kw["format_type"])
        return wav, False
    monkeypatch.setattr(cc, "get_or_render", fake_get_or_render)
    serve = {"target_format": "mp3", "max_bitrate_kbps": 0, "target_sample_rate": 0}
    await stream._prewarm_render_then_encode("dc-1", rip, ".dsf", None, False, None, serve)
    assert calls == ["transcoded"]
    calls.clear()
    await stream._prewarm_render_then_encode("dsd-1", dsd, ".dsf", None, False, None, serve)
    assert calls == []                  # a DSD stream file: a conversion, done already


# ── Work kept off the loop / the default executor (findings 3 and 9) ───────

_ENDLESS = r"""
import struct, sys, time
out = sys.argv[1]
hdr = (b"RIFF" + struct.pack("<I", 36) + b"WAVE" + b"fmt "
       + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16)
       + b"data" + struct.pack("<I", 0))
with open(out, "wb") as f:
    f.write(hdr); f.flush()
    while True:
        f.write((b"\x40\x1f\x40\x1f" * 50 + b"\xc0\xe0\xc0\xe0" * 50) * 100); f.flush()
        time.sleep(0.005)
"""


@pytest.mark.asyncio
async def test_a_live_render_copies_on_the_playback_pool_while_the_default_pool_is_full(
        tmp_path, monkeypatch):
    """Remote scans fill the loop's default executor: a live render's copy
    steps must not queue behind them."""
    script = tmp_path / "endless.py"
    script.write_text(_ENDLESS)
    src = tmp_path / "out.wav"
    loop = asyncio.get_running_loop()
    busy = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(busy)
    blocker = threading.Event()
    for _ in range(4):
        loop.run_in_executor(None, blocker.wait, 30)
    threads = set()
    real_write = stream._LiveWav.write

    def spy(self, chunk):
        threads.add(threading.current_thread().name.rsplit("_", 1)[0])
        return real_write(self, chunk)
    monkeypatch.setattr(stream._LiveWav, "write", spy)
    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(stream._tail_render(
            [sys.executable, str(script), str(src)], src, "pool-test__sub0", kind="FAKE",
            timeout=30, expected_seconds=0.5, stop_after_s=0.5, fade_s=0.1), 15)
        out.unlink(missing_ok=True)
    finally:
        blocker.set()
        busy.shutdown(wait=False)
    assert time.monotonic() - t0 < 10
    assert threads and all(n.startswith("sb-playback-io") for n in threads), threads


@pytest.mark.asyncio
async def test_the_ym_defect_backfill_reads_the_file_off_the_loop(tmp_path, monkeypatch):
    f = tmp_path / "bad.ym"
    f.write_bytes(b"YM4!" + b"\0" * 32)
    seen = []
    real = stream._ym_magic

    def spy(path):
        seen.append(threading.current_thread().name)
        return real(path)
    monkeypatch.setattr(stream, "_ym_magic", spy)
    updates = []

    class _Store:
        def get_track(self, tid):
            return {"id": tid}

        def update_track_fields(self, tid, fields):
            updates.append(fields["defect"])
    import soniqboom.core.store as store_mod
    monkeypatch.setattr(store_mod, "get_store", lambda: _Store())
    assert asyncio.iscoroutinefunction(stream._ym_defect_backfill)
    await stream._ym_defect_backfill("ym-1", f)
    assert updates == ["corrupt"]
    assert seen and all(n.startswith("sb-playback-io") for n in seen), seen


# ── Renderers end with the server (finding 2) ──────────────────────────────

_CHILD = r"""
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from soniqboom.core import forksafe
async def main():
    ps = [await forksafe.spawn("/bin/sleep", "300") for _ in range(2)]
    print(" ".join(str(p.pid) for p in ps), flush=True)
    await asyncio.sleep(300)
asyncio.run(main())
"""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(out.strip()) and not out.strip().startswith("Z")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
def test_children_end_when_the_server_is_killed(tmp_path):
    repo = str(Path(stream.__file__).resolve().parents[2])
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    env = dict(os.environ)
    env.pop("SONIQBOOM_CHILD_WATCHDOG", None)
    c = subprocess.Popen([sys.executable, str(script), repo], stdout=subprocess.PIPE, text=True,
                         env=env)
    pids = [int(x) for x in c.stdout.readline().split()]
    try:
        assert len(pids) == 2 and all(_alive(p) for p in pids)
        os.kill(c.pid, signal.SIGKILL)
        c.wait()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(_alive(p) for p in pids):
            time.sleep(0.05)
        assert not any(_alive(p) for p in pids)
    finally:
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


@pytest.mark.skipif(sys.platform.startswith("linux") or not os.path.exists(getattr(forksafe, "_AWK", "/usr/bin/awk")),
                    reason="the awk watchdog (not Linux)")
def test_the_watchdog_kills_only_the_children_it_was_told_of():
    """At its end the watchdog kills a listed pid only when its parent is the
    server or init AND its process name is the recorded one."""
    mine = subprocess.Popen(["/bin/sleep", "60"])         # parent: this process
    other = subprocess.Popen(["/bin/sleep", "60"])
    try:
        script = forksafe._WATCHDOG_AWK
        # srv = this process: ``mine`` is its child → killed; ``other`` is
        # recorded under another name → kept.
        feed = f"+ {mine.pid} sleep\n+ {other.pid} zxtune123\n"
        subprocess.run([forksafe._AWK, "-v", f"srv={os.getpid()}", script], input=feed,
                       text=True, timeout=10)
        assert mine.wait(timeout=5) == -signal.SIGKILL
        assert other.poll() is None
        # srv = some other pid: not its child, not init's → kept.
        subprocess.run([forksafe._AWK, "-v", "srv=999999", script],
                       input=f"+ {other.pid} sleep\n", text=True, timeout=10)
        assert other.poll() is None
        # Ended ("-") before the end: kept.
        subprocess.run([forksafe._AWK, "-v", f"srv={os.getpid()}", script],
                       input=f"+ {other.pid} sleep\n- {other.pid}\n", text=True, timeout=10)
        assert other.poll() is None
    finally:
        for p in (mine, other):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_orphan_renderers_are_only_a_dead_runs_renderers(monkeypatch, tmp_path):
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    host, me = render_tmp._HOST, os.getpid()
    dead = 999_999_0
    monkeypatch.setattr(render_tmp, "_pid_alive", lambda pid: pid not in (dead,))

    def folder(pid, token="0badc0de", h=host):
        return f"{tmp_path}/soniqboom-render-{h}-{pid}-{token}/tmpx.wav"
    table = {
        101: (1, "S", f"/opt/bin/zxtune123 --silent --loop --wav filename={folder(dead)} a.dsf"),
        102: (1, "S", f"/opt/bin/zxtune123 --wav filename={folder(4242)} a.dsf"),     # owner alive
        103: (1, "S", f"/usr/bin/vim {folder(dead)}"),                                 # not a renderer
        104: (1, "S", f"/opt/bin/zxtune123 --wav filename={folder(dead, h='00000000')} x"),  # other host
        105: (777, "S", f"/opt/bin/zxtune123 --wav filename={folder(dead)} a.dsf"),    # someone's child
        106: (1, "S", f"/opt/bin/zxtune123 --wav filename={folder(me)} a.dsf"),        # my previous image
        107: (1, "S", f"/opt/bin/zxtune123 --wav filename={folder(me, render_tmp._TOKEN)} a"),  # mine now
        108: (1, "Z", f"/opt/bin/zxtune123 --wav filename={folder(dead)} a.dsf"),      # a zombie
        109: (1, "S", f"/opt/bin/zxtune123 --wav filename=/elsewhere/soniqboom-render-{host}-{dead}-0badc0de/t.wav"),
    }
    assert sorted(p for p, _ in render_tmp.orphan_renderers(table)) == [101, 106]


# ── Offline mode (finding 11) ──────────────────────────────────────────────

def test_offline_mode_blocks_internet_hosts_only(monkeypatch):
    from soniqboom.core import netpolicy
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "1")
    for url in ("https://de1.api.radio-browser.info/json/countries", "https://lrclib.net/api",
                "ftp.modland.com", "8.8.8.8", "https://[2001:4860::8888]/"):
        assert netpolicy.blocked(url), url
    for url in ("http://127.0.0.1:8492/Music", "localhost:9", "http://nas.local/x",
                "http://192.168.1.5/", "http://[::1]:80/", "10.0.0.2"):
        assert not netpolicy.blocked(url), url
    with pytest.raises(netpolicy.Offline):
        netpolicy.check("https://api.lyrics.ovh/v1/a/b")
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    assert not netpolicy.blocked("https://lrclib.net/api")


def test_the_suite_runs_offline():
    from soniqboom.core import netpolicy
    assert netpolicy.offline()
    import socket
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("radio-browser.info", 443)
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("1.1.1.1", 443)
    assert socket.getaddrinfo("127.0.0.1", 80)


@pytest.mark.asyncio
async def test_offline_clients_never_connect(monkeypatch):
    """Radio directory, lyrics and artist info answer "unreachable" without
    making a client at all."""
    import httpx
    from soniqboom.api import tracks
    from soniqboom.core import artistinfo, radiodir

    class _NoClient:
        def __init__(self, *a, **k):
            raise AssertionError("an HTTP client was made while offline")
    monkeypatch.setattr(httpx, "AsyncClient", _NoClient)
    monkeypatch.setattr(httpx, "Client", _NoClient)
    with pytest.raises(RuntimeError):
        await radiodir._rb_get("/json/countries")
    probes = []
    monkeypatch.setattr(tracks, "probe_lyrics_providers", lambda: probes.append(1))
    assert await tracks._resolve_online_lyrics("a", "b", "", 0) == (None, False)
    assert probes == []
    with pytest.raises(artistinfo._Unreachable):
        await artistinfo._get_json("https://musicbrainz.org/ws/2/artist?query=x", mb=False)


@pytest.mark.asyncio
async def test_the_start_contacts_no_directory_and_the_first_lookup_ranks_lyrics(
        monkeypatch, tmp_path):
    """Startup only seeds the favorites (the country list is fetched when the
    world browser opens); the lyrics providers are ranked on the first
    lookup, not at startup."""
    from soniqboom.api import tracks
    from soniqboom.core import radiodir
    fetched = []

    async def fake_countries(force=False):
        fetched.append(force)
        return []
    monkeypatch.setattr(radiodir, "get_countries", fake_countries)
    monkeypatch.setattr(radiodir, "_read_json", lambda name: [] if name == radiodir._FAVS_FILE else None)
    monkeypatch.setattr(radiodir, "_write_json", lambda *a: None)
    await radiodir.ensure_ready()
    assert fetched == []
    await radiodir.ensure_ready(fetch=True)
    assert fetched == [False]
    import inspect
    from soniqboom import main
    src = inspect.getsource(main.startup)
    assert "probe_lyrics_providers" not in src
    # Online (mocked providers): the first lookup ranks them in the background.
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    monkeypatch.setattr(tracks, "_last_probe_ts", 0.0)
    monkeypatch.setattr(tracks, "_probe_in_flight", False)
    ranked = asyncio.Event()

    async def fake_probe():
        ranked.set()
    monkeypatch.setattr(tracks, "probe_lyrics_providers", fake_probe)

    async def miss(client, *a):
        return tracks._MISS
    monkeypatch.setattr(tracks, "_LYRICS_PROVIDERS",
                        [{"name": "fake", "fn": miss, "probe": "http://127.0.0.1:9/"}])
    monkeypatch.setattr(tracks, "_lyrics_order", [0])
    monkeypatch.setattr(tracks, "_get_lrclib_client", lambda: object())
    assert await tracks._resolve_online_lyrics("a", "b", "", 0) == (None, True)
    await asyncio.wait_for(ranked.wait(), 2)
