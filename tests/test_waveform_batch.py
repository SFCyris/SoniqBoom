# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""POST /api/tracks/meta/waveforms — the playlist rows' mini waveforms.

Only STORED waveforms are answered (never computed), thumb-sized (``points``
peaks, 0..255 against the waveform's own loudest point); a track without one,
or with a blank one, is left out; ids and points are capped.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import tracks as tracks_api


class _Store:
    def __init__(self, waves):
        self.waves = waves
        self.asked = []

    def get_waveform(self, tid):
        self.asked.append(tid)
        return self.waves.get(tid)


@pytest.fixture()
def store(monkeypatch):
    s = _Store({
        "a": [0.0, 0.5, 1.0, 0.25] * 50,          # 200 points
        "quiet": [0.1] * 10,                      # scaled to its own peak
        "blank": [0.0] * 200,
        "bad": ["x", None],
    })
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    return s


@pytest.mark.asyncio
async def test_stored_waveforms_come_back_thumb_sized(store):
    res = await tracks_api.batch_waveforms({"ids": ["a", "quiet", "blank", "bad", "none", 7], "points": 8})
    assert res["points"] == 8
    assert set(res["waveforms"]) == {"a", "quiet"}
    a = res["waveforms"]["a"]
    assert len(a) == 8 and max(a) == 255 and all(0 <= v <= 255 for v in a)
    assert res["waveforms"]["quiet"] == [255] * 8


@pytest.mark.asyncio
async def test_points_and_ids_are_capped(store):
    res = await tracks_api.batch_waveforms({"ids": ["a"], "points": 10_000})
    assert res["points"] == tracks_api._WAVE_BATCH_MAX_POINTS
    assert len(res["waveforms"]["a"]) == tracks_api._WAVE_BATCH_MAX_POINTS
    res = await tracks_api.batch_waveforms({"ids": ["a"], "points": "x"})
    assert res["points"] == 48
    store.asked.clear()
    await tracks_api.batch_waveforms({"ids": [f"t{i}" for i in range(1000)]})
    assert len(store.asked) == tracks_api._WAVE_BATCH_MAX_IDS


@pytest.mark.asyncio
async def test_a_bad_body_is_a_422(store):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await tracks_api.batch_waveforms({"ids": "a"})
    assert e.value.status_code == 422


@pytest.mark.asyncio
async def test_the_route_is_not_shadowed_by_a_track_id_route(store):
    app = FastAPI()
    app.include_router(tracks_api.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/tracks/meta/waveforms", json={"ids": ["a", "none"], "points": 16})
    assert r.status_code == 200
    body = r.json()
    assert list(body["waveforms"]) == ["a"] and len(body["waveforms"]["a"]) == 16


def test_thumb_takes_the_peak_of_each_slice():
    assert tracks_api._wave_thumb([0.2, 1.0, 0.4, 0.5], 2) == [255, 128]
    assert tracks_api._wave_thumb([0.5], 4) == [255, 255, 255, 255]     # fewer points than asked
    assert tracks_api._wave_thumb([], 4) is None
    assert tracks_api._wave_thumb([float("nan")], 4) is None
