# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub #32: the SID per-voice VU passes take a background slot at
``PRIO_VU`` (like the Amiga pass) — they never hold the render slot playback
needs, and the next-track prewarm goes first."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from soniqboom.api import stream


@pytest.fixture
def gates(monkeypatch):
    """4 render slots, 3 background slots — the reporter's setup."""
    monkeypatch.setattr(stream, "_render_sem", asyncio.Semaphore(4))
    monkeypatch.setattr(stream, "_bg_render_sem", stream._PriorityGate(3))
    return stream


@pytest.mark.asyncio
async def test_sid_vu_passes_wait_for_background_slots(gates, monkeypatch, tmp_path):
    ran = []

    async def renderer(cmd, tmp, *, timeout, kind, require_audio=True):
        async with stream._render_sem:          # what the real one takes
            ran.append(kind)
            await asyncio.sleep(0.05)
    monkeypatch.setattr(stream, "_await_renderer", renderer)
    # Every background slot busy (e.g. prewarms): the VU passes must wait …
    holders = [asyncio.ensure_future(stream._bg_render_sem.acquire(0)) for _ in range(3)]
    await asyncio.gather(*holders)
    passes = [asyncio.ensure_future(stream._sid_vu_pass(["x"], tmp_path / f"{i}.wav", 10))
              for i in range(3)]
    await asyncio.sleep(0.2)
    assert ran == [] and not any(p.done() for p in passes)
    # … while a foreground render still gets a slot at once.
    async with asyncio.timeout(0.5):
        async with stream._render_sem:
            pass
    # A next-track prewarm that queued after them is still served first.
    async def prewarm():
        async with stream._bg_render_sem.slot(stream.PRIO_NEXT):
            ran.append("next")
    pw = asyncio.ensure_future(prewarm())
    await asyncio.sleep(0)
    stream._bg_render_sem.release()
    await pw
    stream._bg_render_sem.release()
    stream._bg_render_sem.release()
    await asyncio.gather(*passes)
    assert ran[0] == "next" and ran[1:] == ["SID-VU"] * 3


@pytest.mark.asyncio
async def test_the_vu_passes_never_hold_every_render_slot(gates, monkeypatch, tmp_path):
    peak = {"held": 0, "now": 0}

    async def renderer(cmd, tmp, *, timeout, kind, require_audio=True):
        async with stream._render_sem:
            peak["now"] += 1
            peak["held"] = max(peak["held"], peak["now"])
            await asyncio.sleep(0.05)
            peak["now"] -= 1
    monkeypatch.setattr(stream, "_await_renderer", renderer)
    # Two tunes' passes at once (more than the gate allows): at most 3 renders.
    await asyncio.gather(*[stream._sid_vu_pass(["x"], tmp_path / f"{i}.wav", 10)
                           for i in range(6)])
    assert peak["held"] == 3


def test_the_worker_renders_through_the_gated_pass():
    import inspect
    src = inspect.getsource(stream._sid_vu_worker)
    assert "_sid_vu_pass(cmd, tp, dur)" in src
    assert "_await_renderer(" not in src
