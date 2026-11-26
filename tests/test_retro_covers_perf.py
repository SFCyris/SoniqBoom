# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Performance guard for the retro covers (core/gen_cover.py, core/igdb_cover.py)
on the paths a user feels: a page of list rows asking their covers at once,
the player / Track Info picture, and a box-cover lookup that must never hold
up an answer.  The budgets are several times what the dev Mac measured
(2026-11-25, see the comments) so a slower CI box passes, while a real
regression — drawing on the event loop, a lost cache, a lookup awaited on
the request path — fails.  Server-side only (the handler, not HTTP)."""
from __future__ import annotations

import asyncio
import statistics
import time
from types import SimpleNamespace

import httpx
import pytest

from soniqboom.api import art as A
from soniqboom.config import settings
from soniqboom.core import gen_cover as G
from soniqboom.core import igdb_cover as I
from soniqboom.core import online_cover as OC

FORMATS = ["SID", "ProTracker", "TFMX Pro", "SNDH", "NSF", "SPC", "GBS", "VGZ", "PSF", "AdLib ROL"]


class _Req:
    headers: dict = {}


@pytest.fixture
def page(monkeypatch, tmp_path):
    """200 retro tracks, none with a cover of its own, each a different cover."""
    monkeypatch.setattr(settings, "art_cache_dir", str(tmp_path / "art"))
    monkeypatch.setattr(settings, "igdb_client_id", "")
    monkeypatch.setattr(settings, "igdb_client_secret", "")
    tracks = {}
    for i in range(200):
        tracks[f"t{i}"] = SimpleNamespace(
            id=f"t{i}", path=f"/m/t{i}.bin", format=FORMATS[i % len(FORMATS)],
            artist=f"Composer {i % 37}", album="", title=f"Tune number {i}",
            game=f"Game {i // 3}" if i % 2 else "", album_artist="", year=None,
            cover_art=None, sid_md5=None, album_source=None, game_aliases=None, composer="")
    absent: set = set()

    async def get_track(tid):
        return tracks.get(tid)

    async def nothing(*_a, **_k):
        return None

    async def resolve(tid, **_kw):
        absent.add(tid)
        return None, None

    async def stamp(_t, urls=True):
        return 100.0

    async def no_online(_track):
        return OC.NOTHING
    monkeypatch.setattr(A, "get_track", get_track)
    monkeypatch.setattr(A.art_cache, "get_art", nothing)
    monkeypatch.setattr(A, "_art_cached_mtime", lambda tid, size: None)
    monkeypatch.setattr(A, "_resolve_full_art", resolve)
    monkeypatch.setattr(A, "_source_stamp", stamp)
    monkeypatch.setattr(A, "get_store", lambda: SimpleNamespace(is_art_absent=lambda tid: tid in absent))
    monkeypatch.setattr(A, "_is_art_absent_persisted", lambda tid, m: False)
    monkeypatch.setattr(OC, "state", no_online)
    on = {"v": True}
    monkeypatch.setattr(G, "enabled_now", lambda: on["v"])
    G.forget_all()
    I._reset_for_tests()
    yield SimpleNamespace(tracks=tracks, on=on)
    G.forget_all()
    I._reset_for_tests()


async def _one(tid, size):
    t0 = time.perf_counter()
    r = await A.cover_art(tid, _Req(), size=size, fallback="none")
    return time.perf_counter() - t0, r


async def _run(ids, size="sm", burst=True):
    """``(wall, per-request times, answers, the event loop's longest stall)``."""
    stop, lags = asyncio.Event(), []

    async def tick():
        while not stop.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(0.002)
            lags.append(time.perf_counter() - t0 - 0.002)
    tk = asyncio.create_task(tick())
    await asyncio.sleep(0.005)
    t0 = time.perf_counter()
    if burst:
        out = await asyncio.gather(*(_one(t, size) for t in ids))
    else:
        out = [await _one(t, size) for t in ids]
    wall = time.perf_counter() - t0
    stop.set()
    await tk
    return wall, sorted(d for d, _ in out), [r for _, r in out], max(lags)


def _p95(xs):
    return xs[int(len(xs) * 0.95) - 1]


async def test_a_page_of_200_rows_cold_then_warm(page):
    ids = list(page.tracks)
    # Cold: 200 different covers drawn for a burst of 200 rows at once.
    # Dev Mac: all answered in 0.21 s, event loop stalled at most 5 ms.
    wall, times, answers, stall = await _run(ids)
    assert {r.headers["X-SoniqBoom-Art"] for r in answers} == {"generated"}
    assert wall < 3.0, f"cold burst of 200 took {wall:.2f} s"
    assert stall < 0.05, f"the event loop stalled {stall * 1000:.0f} ms (drawing on the loop?)"
    # Warm: from memory.  Dev Mac: 4 ms for all 200.
    wall, times, answers, stall = await _run(ids)
    assert wall < 0.3 and _p95(times) < 0.005, f"warm: {wall:.3f} s, p95 {_p95(times) * 1000:.1f} ms"
    assert stall < 0.05


async def test_one_cover_at_a_time_like_a_scrolled_list(page):
    ids = list(page.tracks)[:100]
    # Dev Mac: p50 1.2 ms, p95 3.3 ms per list thumbnail drawn.
    wall, times, _answers, stall = await _run(ids, burst=False)
    assert _p95(times) < 0.04, f"p95 {_p95(times) * 1000:.1f} ms per new list cover"
    assert stall < 0.05


async def test_the_player_and_track_info_picture(page):
    ids = list(page.tracks)[:40]
    # Dev Mac: lg drawn in p50 7 ms / p95 9.5 ms; from disk 0.15 ms.
    wall, times, _answers, _stall = await _run(ids, size="lg", burst=False)
    assert _p95(times) < 0.08, f"p95 {_p95(times) * 1000:.1f} ms for a new lg cover"
    G.forget_all()
    wall, times, _answers, _stall = await _run(ids, size="lg", burst=False)
    assert _p95(times) < 0.01, f"p95 {_p95(times) * 1000:.1f} ms from the disk cache"


async def test_the_old_answer_path_costs_what_it_did(page):
    page.on["v"] = False                       # the emoji: what every retro row had before
    wall, times, answers, _stall = await _run(list(page.tracks))
    assert {r.status_code for r in answers} == {204}
    assert wall < 0.3 and _p95(times) < 0.005


async def test_a_slow_box_cover_lookup_never_holds_an_answer(page, monkeypatch):
    """IGDB answering in 1.5 s: the play / Track Info signal returns at once
    and every cover request meanwhile answers at once (the generated one)."""
    monkeypatch.setattr(settings, "igdb_client_id", "clientid123")
    monkeypatch.setattr(settings, "igdb_client_secret", "secret456789")
    monkeypatch.setattr(I, "_offline", lambda: False)

    async def slow(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.5)
        return httpx.Response(503)

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await slow(request)
    monkeypatch.setattr(I, "_transport", Slow())
    t = page.tracks["t2"]
    t.format, t.game = "TFMX Pro", "Turrican II"
    t0 = time.perf_counter()
    assert (await A.art_intent("t2")) == {"queued": True}
    assert time.perf_counter() - t0 < 0.05
    d, r = await _one("t2", "lg")
    assert d < 0.1 and r.headers["X-SoniqBoom-Art"] == "generated"
    assert r.headers["Cache-Control"] == "no-cache"           # (a box cover may come)
    I._reset_for_tests()
