# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A thumbnail cache miss resizes only the bucket that was asked for."""
import asyncio

import pytest

from soniqboom.api import art


@pytest.mark.asyncio
async def test_cold_sm_thumbnail_resizes_once_and_stores_off_the_request_path(monkeypatch):
    calls, stored = [], []

    async def no_thumb(tid, size):
        return None

    async def full_art(tid):
        return b"FULL", "image/jpeg"

    async def store(tid, data, size):
        stored.append((tid, size, data))

    monkeypatch.setattr(art.art_cache, "get_art", no_thumb)
    monkeypatch.setattr(art.art_cache, "store_art", store)
    monkeypatch.setattr(art, "_resolve_full_art", full_art)
    monkeypatch.setattr(art, "_art_cached_mtime", lambda *a: 1.0)
    monkeypatch.setattr(art, "resize_cover", lambda data, px: calls.append(px) or b"SM%d" % px)

    class _Req:
        headers = {}
    resp = await art.cover_art("tid1", _Req(), size="sm", fallback="placeholder")
    assert calls == [200]
    assert resp.body == b"SM200"
    for _ in range(5):
        await asyncio.sleep(0)
    assert stored == [("tid1", "sm", b"SM200")]
