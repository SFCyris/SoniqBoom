# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The blocking render path (get_or_render — Subsonic/DLNA/cast + web spillover)
must FINISH and cache an in-flight render even when the calling request is
cancelled by a client disconnect, so the next play is a cache hit instead of a
re-render."""
import asyncio

import pytest

from soniqboom.core import conversion_cache as cc


@pytest.mark.asyncio
async def test_detached_render_survives_caller_cancel(tmp_data_dir, tmp_path):
    started = asyncio.Event()

    async def render_fn():
        started.set()
        await asyncio.sleep(0.4)                      # still rendering when we cancel
        p = tmp_path / "out.wav"
        p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEdata" + b"\x00" * 8000)
        return p

    caller = asyncio.ensure_future(cc.get_or_render(
        track_id="t1", format_type="tracker", subsong=0, render_fn=render_fn))
    await asyncio.wait_for(started.wait(), timeout=2)
    caller.cancel()                                   # client disconnected mid-render
    with pytest.raises(asyncio.CancelledError):
        await caller

    # The detached render should complete and populate the cache anyway.
    key = cc._cache_key("t1", "tracker", 0)
    for _ in range(20):                               # up to ~1s
        await asyncio.sleep(0.05)
        cached = await cc.get_cached(key)
        if cached:
            break
    assert cached is not None and cached.exists(), "render was lost on caller cancel"


@pytest.mark.asyncio
async def test_normal_render_still_returns_and_caches(tmp_data_dir, tmp_path):
    async def render_fn():
        p = tmp_path / "n.wav"
        p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEdata" + b"\x00" * 8000)
        return p

    dest, hit = await cc.get_or_render(
        track_id="t2", format_type="tracker", subsong=0, render_fn=render_fn)
    assert hit is False and dest.exists()
    # second call is a cache hit, no re-render
    dest2, hit2 = await cc.get_or_render(
        track_id="t2", format_type="tracker", subsong=0,
        render_fn=lambda: (_ for _ in ()).throw(AssertionError("should not re-render")))
    assert hit2 is True and dest2 == dest
