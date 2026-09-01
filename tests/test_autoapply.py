# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The post-scan Demozoo auto-apply must COALESCE, never drop a scan's delta:
a scan that drains while a prior apply is running (or right after one) has to be
folded in on a follow-up pass."""
import asyncio

import pytest

from soniqboom.core import scanner, demozoo


async def _drain_runner():
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not scanner._scene_autoapply_running:
            return


@pytest.mark.asyncio
async def test_autoapply_coalesces_delta_during_running_apply(monkeypatch):
    monkeypatch.setattr(demozoo, "has_index", lambda: True)
    monkeypatch.setattr(demozoo, "auto_apply_enabled", lambda: True)
    monkeypatch.setattr(scanner, "_SCENE_AUTOAPPLY_SETTLE_S", 0)
    scanner._scene_autoapply_pending = False
    scanner._scene_autoapply_running = False

    calls = []
    first_started = asyncio.Event()

    async def fake_apply():
        calls.append(1)
        if len(calls) == 1:
            first_started.set()
            await asyncio.sleep(0.2)          # apply A is running…
        return {"updated": 0}

    monkeypatch.setattr(demozoo, "apply_to_library", fake_apply)

    scanner._spawn_scene_autoapply()          # scan A drains
    await asyncio.wait_for(first_started.wait(), timeout=2)
    scanner._spawn_scene_autoapply()          # scan B drains WHILE apply A runs
    await _drain_runner()
    assert len(calls) >= 2, "B's enrichment delta was dropped"


@pytest.mark.asyncio
async def test_autoapply_retries_when_manual_apply_holds_the_lock(monkeypatch):
    monkeypatch.setattr(demozoo, "has_index", lambda: True)
    monkeypatch.setattr(demozoo, "auto_apply_enabled", lambda: True)
    monkeypatch.setattr(scanner, "_SCENE_AUTOAPPLY_SETTLE_S", 0)
    scanner._scene_autoapply_pending = False
    scanner._scene_autoapply_running = False

    calls = []

    async def fake_apply():
        calls.append(1)
        # first call: a manual Admin apply holds the lock
        if len(calls) == 1:
            return {"error": "apply already running"}
        return {"updated": 3}

    monkeypatch.setattr(demozoo, "apply_to_library", fake_apply)
    scanner._spawn_scene_autoapply()
    await _drain_runner()
    assert len(calls) >= 2, "lock-contended delta was dropped instead of retried"


@pytest.mark.asyncio
async def test_autoapply_noop_without_index(monkeypatch):
    monkeypatch.setattr(demozoo, "has_index", lambda: False)
    scanner._scene_autoapply_pending = False
    scanner._scene_autoapply_running = False
    called = []
    monkeypatch.setattr(demozoo, "apply_to_library",
                        lambda: called.append(1) or {"updated": 0})
    scanner._spawn_scene_autoapply()
    await asyncio.sleep(0.05)
    assert not called and not scanner._scene_autoapply_running


@pytest.mark.asyncio
async def test_autoapply_stops_when_toggled_off_mid_coalesce(monkeypatch):
    """F1 regression: a Reset flipping the toggle OFF while the coalescing runner
    is looping must STOP it, not re-enrich a just-reset library.  The guard was
    only in _spawn; the _run loop re-applied without re-checking."""
    monkeypatch.setattr(demozoo, "has_index", lambda: True)
    monkeypatch.setattr(scanner, "_SCENE_AUTOAPPLY_SETTLE_S", 0)
    enabled = {"v": True}
    monkeypatch.setattr(demozoo, "auto_apply_enabled", lambda: enabled["v"])
    scanner._scene_autoapply_pending = False
    scanner._scene_autoapply_running = False
    calls = []

    async def fake_apply():
        calls.append(1)
        enabled["v"] = False                  # a Reset turns the toggle OFF…
        scanner._scene_autoapply_pending = True  # …and a scan delta arrives after
        return {"updated": 0}

    monkeypatch.setattr(demozoo, "apply_to_library", fake_apply)
    scanner._spawn_scene_autoapply()
    await _drain_runner()
    assert calls == [1], "runner re-applied after the toggle went OFF"


@pytest.mark.asyncio
async def test_reset_waits_out_inflight_apply(monkeypatch):
    """F2 regression: Reset must not silently no-op while an apply holds the
    lock — it waits (bounded) for the lock, then runs."""
    monkeypatch.setattr(demozoo, "reset_enrichment", lambda: (0, []))
    demozoo._status["applying"] = True

    async def _release():
        await asyncio.sleep(0.05)
        demozoo._status["applying"] = False

    asyncio.ensure_future(_release())
    res = await demozoo.reset_to_file_state()
    assert res.get("error") != "apply already running"   # it waited, not a no-op
    assert res.get("cleared") == 0
    assert demozoo._status["applying"] is False           # released the lock
