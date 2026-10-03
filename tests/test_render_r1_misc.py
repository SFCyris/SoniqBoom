# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render-area fixes from the review loop, part 2: progressive SID renders
reach the cache early, prewarm cancellation never cancels a shared pump,
duration probes (one batched store write; uade probes kept as the play's
render), offline sources, the waveform for unreachable tracks, the lyrics
miss cache, and web plays in Subsonic ``getNowPlaying``.

In-process only: temp conversion cache, fake stores, stubbed renderers.
"""
from __future__ import annotations

import asyncio
import shutil
import time
import types
from pathlib import Path

import pytest

from soniqboom.api import stream
from soniqboom.api import tracks as tracks_api
from soniqboom.core import conversion_cache

from test_render_r1_stream import FakeStore, _T, _app, _client


async def _noauth(*a, **k):
    """Stand-in for the (async) stream sign-in check."""
    return None


REPO = Path(__file__).resolve().parent.parent
SID = REPO / "internal/testdata/sid/SX-64_Demo.sid"
DSF = REPO / "internal/testdata/dsd/dsd_stream_file.dsf"
DW = REPO / "internal/testdata/uade/David Whittaker/carrier command.dw"


def _wav(path: Path, seconds: float) -> Path:
    frames = int(seconds * 44100)
    # Audible (a ±8000 square wave): the cache refuses a silent render.
    path.write_bytes(stream._build_wav_header(44100, 2, frames, 16)
                     + (b"\x40\x1f\x40\x1f\xc0\xe0\xc0\xe0" * frames)[:frames * 4])
    return path


# ── r1-ren-6: a progressive SID render is cached when it finishes ───────────

@pytest.mark.skipif(not shutil.which("sidplayfp") or not SID.exists(),
                    reason="sidplayfp or the SID fixture is missing")
@pytest.mark.asyncio
async def test_progressive_sid_is_cached_before_the_listener_finishes(monkeypatch, tmp_path):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    tid, dur = "sidprog-early", 60
    key = conversion_cache._cache_key(tid, "sid", 0, duration=dur)
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    resp = await stream._serve_sid_progressive(
        types.SimpleNamespace(headers={}), SID, 0, dur, key, {}, None)
    assert resp.headers["x-accel-buffering"] == "no"
    it = resp.body_iterator.__aiter__()
    got = bytearray()
    while len(got) < 1_000_000:                       # a slow reader: 1 MB, then pause
        got += await it.__anext__()
    assert stream._render_state(tid, 0) == "rendering"
    waveform = asyncio.ensure_future(tracks_api._waveform_from_conversion_cache(
        tid, str(SID), ".sid", sid_duration=dur, appear_wait=1.0, finish_wait=30))
    t0 = time.monotonic()
    cached = None
    while time.monotonic() - t0 < 15:
        cached = await conversion_cache.get_cached(key)
        if cached is not None:
            break
        await asyncio.sleep(0.2)
    assert cached is not None, "render finished but was not cached while the listener paused"
    assert stream._render_state(tid, 0) == "complete"
    wf = await waveform
    assert isinstance(wf, Path) and wf == cached       # the waveform attached, no "pending"
    async for c in it:                                  # the rest still streams via the fd
        got += c
    data = cached.read_bytes()
    assert bytes(got[44:]) == data[44:44 + len(got) - 44]
    assert len(got) == 44 + dur * 44100 * 2
    conversion_cache._purge_entry(key)


# ── r1-ren-7: cancelling a DSD prewarm never cancels the shared pump ────────

@pytest.mark.skipif(not shutil.which("ffmpeg") or not DSF.exists(),
                    reason="ffmpeg or the DSD fixture is missing")
@pytest.mark.asyncio
async def test_cancelled_prewarm_leaves_the_shared_dsd_pump_running(monkeypatch, tmp_path):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    tid = "dsd-shield"
    tr = types.SimpleNamespace(id=tid, path=str(DSF), duration=0)

    async def get_track(x):
        return tr
    monkeypatch.setattr(stream, "get_track", get_track)
    pw = asyncio.ensure_future(stream._do_prewarm_render(tid, DSF, ".dsf", 0, track=tr))
    inf = None
    for _ in range(300):
        inf = stream._INFLIGHT_TRANSCODES.get(tid)
        if inf and "pump_task" in inf:
            break
        await asyncio.sleep(0.02)
    assert inf and "pump_task" in inf
    inf["subscribers"] += 1                              # listener B is streaming it
    pump = inf["pump_task"]
    pw.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pw
    assert not pump.cancelled()
    await asyncio.wait_for(pump, timeout=120)
    assert not pump.cancelled() and inf["clean_exit"] is True
    assert await conversion_cache.get_cached(stream._inflight_cache_key(tid, stream._DSD_OUTPUT_RATE))


# ── r1-ren-13 / r1-ren-21: duration probes ──────────────────────────────────

@pytest.mark.asyncio
async def test_probe_durations_writes_once_per_batch(monkeypatch, tmp_path):
    from soniqboom.core import store as store_mod
    tracks = {f"g{i}": _T(f"g{i}", tmp_path / f"t{i}.nsf", 180.0, fmt="NSF") for i in range(3)}
    fs = FakeStore(tracks)
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)
    monkeypatch.setattr(stream.settings, "sid_default_duration", 180)

    async def get_track(tid):
        return tracks.get(tid)
    monkeypatch.setattr(stream, "get_track", get_track)

    async def resolve(tid, path_str, **k):
        return Path(path_str)
    monkeypatch.setattr(stream, "_resolve_adlib_local_path", resolve)
    n = {"i": 0}

    async def fake_gme(path, subsong=0):
        n["i"] += 1
        return _wav(tmp_path / f"r{n['i']}.wav", 10 + n["i"])
    monkeypatch.setattr(stream, "_render_gme", fake_gme)
    monkeypatch.setattr(stream, "_require_stream_auth", _noauth)
    out = await stream.probe_durations({"track_ids": list(tracks)}, None, None)
    assert len(out) == 3
    assert fs.updates == [] and len(fs.batches) == 1 and len(fs.batches[0]) == 3
    # every row already real → no probe, no write
    out2 = await stream.probe_durations({"track_ids": list(tracks)}, None, None)
    assert len(fs.batches) == 1 and n["i"] == 3 and len(out2) == 3
    # the play path still writes immediately
    t = _T("p1", tmp_path / "x.nsf", 180.0)
    await stream._backfill_rendered_duration("p1", t, _wav(tmp_path / "p.wav", 7), 180.0)
    assert fs.updates == [("p1", {"duration": 7.0})]


@pytest.mark.asyncio
async def test_uade_probe_is_the_plays_render_when_the_cache_has_room(monkeypatch, tmp_path):
    from soniqboom.config import settings
    from soniqboom.core import store as store_mod
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    tid = "uprobe1"
    t = _T(tid, tmp_path / "tune.dw", 0.0)
    fs = FakeStore({tid: t})
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)

    async def get_track(x):
        return t if x == tid else None
    monkeypatch.setattr(stream, "get_track", get_track)

    async def resolve(x, path_str, **k):
        return Path(path_str)
    monkeypatch.setattr(stream, "_resolve_adlib_local_path", resolve)
    calls = []

    async def slow_render(path, subsong=0, with_vu=True, *, live_key=None, expected_seconds=0.0,
                          subsong_base=0):
        calls.append(live_key)
        await asyncio.sleep(0.3)
        return _wav(tmp_path / f"u{len(calls)}.wav", 9)
    monkeypatch.setattr(stream, "_render_uade", slow_render)
    probe = asyncio.ensure_future(stream._probe_one_rendered_duration(tid))
    await asyncio.sleep(0.05)
    # a click during the probe attaches instead of rendering again
    from soniqboom.core.conversion_cache import get_or_render
    played, hit = await get_or_render(track_id=tid, format_type="uade", subsong=0,
                                      render_fn=lambda: slow_render(None))
    assert await probe == 9.0
    assert len(calls) == 1 and calls[0] == stream._ck(tid, "uade", subsong=0)
    assert played.exists() and await conversion_cache.get_cached(played.stem) == played
    assert fs.updates == [(tid, {"duration": 9.0})]
    conversion_cache._purge_entry(played.stem)


@pytest.mark.asyncio
async def test_uade_probe_throws_its_render_away_without_headroom(monkeypatch, tmp_path):
    from soniqboom.config import settings
    from soniqboom.core import store as store_mod
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(settings, "conversion_cache_max_bytes", 1024)
    tid = "uprobe2"
    t = _T(tid, tmp_path / "tune.dw", 0.0)
    monkeypatch.setattr(store_mod, "get_store", lambda: FakeStore({tid: t}))

    async def get_track(x):
        return t
    monkeypatch.setattr(stream, "get_track", get_track)

    async def resolve(x, path_str, **k):
        return Path(path_str)
    monkeypatch.setattr(stream, "_resolve_adlib_local_path", resolve)
    made = []

    async def render(path, subsong=0, with_vu=True, *, live_key=None, expected_seconds=0.0,
                     subsong_base=0):
        made.append(_wav(tmp_path / "throw.wav", 5))
        return made[-1]
    monkeypatch.setattr(stream, "_render_uade", render)
    assert await stream._probe_one_rendered_duration(tid) == 5.0
    assert not made[0].exists()
    assert await conversion_cache.get_cached(stream._ck(tid, "uade", subsong=0)) is None


# ── r1-ren-14: an offline source is not a missing file ──────────────────────

@pytest.mark.asyncio
async def test_offline_root_is_503_but_a_missing_file_stays_410(monkeypatch, tmp_path):
    from soniqboom.core import data as data_mod
    offline_root = str(tmp_path / "ejected-drive")          # does not exist
    online_root = tmp_path / "music"
    online_root.mkdir()
    fs = FakeStore(scan_dirs=[
        {"path": offline_root, "status": "ok", "path_hash": data_mod.path_hash(offline_root)},
        {"path": str(online_root), "status": "ok",
         "path_hash": data_mod.path_hash(str(online_root))},
    ])
    tracks = {
        "off1": _T("off1", f"{offline_root}/Uridium.sid", 60.0, fmt="SID",
                   scan_root_hash=data_mod.path_hash(offline_root)),
        "gone1": _T("gone1", online_root / "deleted.flac", 60.0, fmt="FLAC",
                    scan_root_hash=data_mod.path_hash(str(online_root))),
        "offz": _T("offz", f"{offline_root}/pack.zip::mod.x", 60.0, fmt="TFMX"),
    }
    app, _ = _app(monkeypatch, tmp_path, tracks, store=fs)
    async with _client(app) as c:
        r = await c.get("/api/stream/off1")
        assert r.status_code == 503 and "Source offline" in r.json()["detail"]
        assert offline_root in r.json()["detail"]
        rz = await c.get("/api/stream/offz")
        assert rz.status_code == 503
        r2 = await c.get("/api/stream/gone1")
        assert r2.status_code == 410


# ── r1-ren-16: no 5 s "pending" for a track that cannot render ──────────────

@pytest.mark.asyncio
async def test_waveform_for_an_unreachable_source_answers_at_once(monkeypatch, tmp_path):
    from soniqboom.core import store as store_mod
    root = str(tmp_path / "offline")
    fs = FakeStore(scan_dirs=[{"path": root, "status": "unavailable"}])
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)
    t0 = time.monotonic()
    got = await tracks_api._waveform_from_conversion_cache(
        "wf-off", f"{root}/Uridium.sid", ".sid", sid_duration=60, appear_wait=5.0)
    assert got is None and time.monotonic() - t0 < 1.0
    # a reachable-but-not-yet-rendered track still says "pending"
    got = await tracks_api._waveform_from_conversion_cache(
        "wf-on", str(tmp_path / "here/x.sid"), ".sid", sid_duration=60, appear_wait=0.2)
    assert got is tracks_api._WAVEFORM_PENDING
    # remote share not connected
    assert tracks_api._source_unreachable("smb://nas/music:/a/b.mod") is True


@pytest.mark.asyncio
async def test_waveform_endpoint_has_no_pending_flag_for_an_offline_track(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from soniqboom.core import data as data_mod
    from soniqboom.core import store as store_mod
    root = str(tmp_path / "offline")
    fs = FakeStore(scan_dirs=[{"path": root, "status": "unavailable"}])
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)
    t = _T("wf-ep", f"{root}/Uridium.sid", 60.0, fmt="SID")

    async def get_track(x):
        return t
    async def get_waveform(x):
        return None
    monkeypatch.setattr(data_mod, "get_track", get_track)
    monkeypatch.setattr(data_mod, "get_waveform", get_waveform)
    app = FastAPI()
    app.include_router(tracks_api.router, prefix="/api")
    t0 = time.monotonic()
    async with _client(app) as c:
        r = await c.get("/api/tracks/wf-ep/waveform")
    assert r.status_code == 200 and r.json() == {"waveform": None}
    assert time.monotonic() - t0 < 1.0


# ── r1-ren-19: lyrics misses are remembered, errors are not ─────────────────

@pytest.mark.asyncio
async def test_lyrics_clean_misses_are_cached_errors_are_not(monkeypatch, tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"\0" * 10)
    t = types.SimpleNamespace(path=str(song), artist="A", album_artist="", title="T",
                              album="", duration=100.0)

    async def get_track(x):
        return t
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    monkeypatch.setattr(tracks_api, "extract_lyrics", lambda p: None)
    calls = {"n": 0}
    answer = {"v": tracks_api._MISS}

    async def prov(client, artist, title, album, duration):
        calls["n"] += 1
        if isinstance(answer["v"], Exception):
            raise answer["v"]
        return answer["v"]
    monkeypatch.setattr(tracks_api, "_LYRICS_PROVIDERS", [{"name": "p", "fn": prov, "probe": ""}])
    monkeypatch.setattr(tracks_api, "_lyrics_order", [0])
    monkeypatch.setattr(tracks_api, "_last_probe_ts", time.monotonic())
    monkeypatch.setattr(tracks_api, "_lyrics_miss", {})
    monkeypatch.setattr(tracks_api, "_lyrics_cache", {})
    # (a) a clean miss twice → one provider call
    assert (await tracks_api.get_lyrics("ly1"))["lyrics"] is None
    assert (await tracks_api.get_lyrics("ly1"))["lyrics"] is None
    assert calls["n"] == 1
    # (b) an error twice → two calls
    answer["v"] = RuntimeError("timeout")
    await tracks_api.get_lyrics("ly2")
    await tracks_api.get_lyrics("ly2")
    assert calls["n"] == 3
    answer["v"] = None                                   # a provider-side failure
    await tracks_api.get_lyrics("ly3")
    await tracks_api.get_lyrics("ly3")
    assert calls["n"] == 5
    # (c) a tag edit forgets the miss
    answer["v"] = tracks_api._MISS
    tracks_api._forget_lyrics("ly1")
    await tracks_api.get_lyrics("ly1")
    assert calls["n"] == 6
    # (d) after the TTL it re-queries
    tracks_api._lyrics_miss["ly1"] = time.monotonic() - 1
    await tracks_api.get_lyrics("ly1")
    assert calls["n"] == 7
    # (e) clearing the cache clears the misses too
    assert "ly1" in tracks_api._lyrics_miss
    tracks_api.clear_lyrics_cache()
    assert tracks_api._lyrics_miss == {}


# ── r1-ren-23: web plays show up in getNowPlaying ───────────────────────────

@pytest.mark.asyncio
async def test_a_recorded_web_play_is_now_playing(monkeypatch):
    from soniqboom.api import smart, subsonic
    from soniqboom.core import store as store_mod
    from soniqboom.core import users as users_mod

    async def no_history(*a, **k):
        return None
    monkeypatch.setattr(smart, "push_history", no_history)
    fs = FakeStore()
    fs.get_track = lambda tid: None                      # → no external scrobble
    monkeypatch.setattr(store_mod, "get_store", lambda: fs)
    noted = []
    monkeypatch.setattr(subsonic, "_note_now_playing",
                        lambda user, tid, player, **k: noted.append((user.id, tid, player, k)))
    user = types.SimpleNamespace(id="u1", username="bob", enabled=True)

    class _Users:
        def lookup_session(self, tok):
            return user if tok == "good" else None
    monkeypatch.setattr(users_mod, "get_user_store", lambda: _Users())
    t = types.SimpleNamespace(id="np1", title="x", artist="y")

    async def get_track(x):
        return t

    async def record_play(x):
        return {"play_count": 1}
    monkeypatch.setattr(tracks_api, "get_track", get_track)
    monkeypatch.setattr(tracks_api, "record_play", record_play)
    await tracks_api.mark_played("np1", sb_session="good")
    assert noted == [("u1", "np1", "SoniqBoom Web", {"from_scrobble": True})]
    await tracks_api.mark_played("np1", sb_session=None)       # cookieless → nothing
    await tracks_api.mark_played("np1", sb_session="bad")
    assert len(noted) == 1
