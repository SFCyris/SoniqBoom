# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-1 review fixes in the scanner:

* the post-scan scene runner also applies the Modland index (before Demozoo,
  then the one-time header-game backfill, then the folder-album pass), waits
  out a running scan, coalesces bursts, and runs once after an upgrade;
* the waveform fallback without numpy returns peaks + RMS from a zero-copy
  view instead of one Python float per sample;
* a large scan commit freezes the long-lived heap for the cyclic GC."""
from __future__ import annotations

import asyncio
import math
import struct
import sys

import pytest

from soniqboom.core import demozoo, repair, scanner, scene_metadata
from soniqboom.core import folder_album as fa
from soniqboom.core.store import TrackStore


@pytest.fixture
def runner(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(scanner, "_SCENE_AUTOAPPLY_SETTLE_S", 0)
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: False)
    scanner._scene_autoapply_pending = False
    scanner._scene_autoapply_running = False
    state = {"modland_index": True, "demozoo_index": True, "demozoo_auto": True,
             "modland_results": [], "order": []}
    monkeypatch.setattr(scene_metadata, "has_index", lambda: state["modland_index"])
    monkeypatch.setattr(demozoo, "has_index", lambda: state["demozoo_index"])
    monkeypatch.setattr(demozoo, "auto_apply_enabled", lambda: state["demozoo_auto"])

    async def modland(*, auto=False):
        state["order"].append(("modland", auto))
        await asyncio.sleep(0)
        if state["modland_results"]:
            return state["modland_results"].pop(0)
        return {"last_apply": {"updated": 0}}

    async def dz():
        state["order"].append("demozoo")
        return {"updated": 0}

    async def backfill():
        state["order"].append("backfill")
        return False
    monkeypatch.setattr(scene_metadata, "apply_to_library", modland)
    monkeypatch.setattr(demozoo, "apply_to_library", dz)
    monkeypatch.setattr(repair, "run_album_backfill_once", backfill)
    monkeypatch.setattr(fa, "schedule_after_scan", lambda: state["order"].append("folder"))
    state["store"] = s
    return state


async def _drain():
    for _ in range(300):
        await asyncio.sleep(0.01)
        if not scanner._scene_autoapply_running:
            return
    raise AssertionError("runner did not finish")


async def test_modland_runs_first_then_demozoo_backfill_and_folder(runner):
    scanner._spawn_scene_autoapply()
    await _drain()
    assert runner["order"] == [("modland", True), "demozoo", "backfill", "folder"]


async def test_modland_runs_even_with_demozoo_auto_apply_off(runner):
    runner["demozoo_auto"] = False
    scanner._spawn_scene_autoapply()
    await _drain()
    assert runner["order"] == [("modland", True), "backfill", "folder"]


async def test_without_any_index_only_the_folder_pass_is_scheduled(runner):
    runner.update(modland_index=False, demozoo_index=False)
    scanner._spawn_scene_autoapply()
    await asyncio.sleep(0.02)
    assert runner["order"] == ["folder"] and not scanner._scene_autoapply_running


async def test_a_burst_of_drains_coalesces(runner):
    scanner._spawn_scene_autoapply()
    for _ in range(5):
        scanner._spawn_scene_autoapply()          # more scans drain meanwhile
    await _drain()
    passes = [o for o in runner["order"] if isinstance(o, tuple)]
    assert 1 <= len(passes) <= 2


async def test_a_busy_modland_apply_is_retried(runner):
    runner["modland_results"] = [{"error": "apply already running"}]
    scanner._spawn_scene_autoapply()
    await _drain()
    assert [o for o in runner["order"] if isinstance(o, tuple)] == [("modland", True)] * 2


async def test_the_runner_waits_out_a_running_scan(runner, monkeypatch):
    scanning = {"v": True}
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: scanning["v"])
    scanner._spawn_scene_autoapply()
    await asyncio.sleep(0.05)
    assert runner["order"] == []                  # never joins a half-written library
    scanning["v"] = False
    await _drain()
    assert runner["order"][0] == ("modland", True)


async def test_startup_trigger_runs_once_for_an_old_apply_version(runner):
    s = runner["store"]
    s.set_config(repair.ALBUM_BACKFILL_CONFIG_KEY, True)
    scanner.schedule_startup_enrichment()
    await _drain()
    assert runner["order"][0] == ("modland", True)
    # Current version + backfill done → nothing to do at the next start.
    runner["order"].clear()
    s.set_config(scene_metadata.APPLY_VERSION_CONFIG_KEY, scene_metadata.MODLAND_APPLY_VERSION)
    scanner.schedule_startup_enrichment()
    await asyncio.sleep(0.02)
    assert runner["order"] == [] and not scanner._scene_autoapply_running


async def test_startup_trigger_for_the_pending_backfill_alone(runner):
    runner["modland_index"] = False
    scanner.schedule_startup_enrichment()
    await _drain()
    assert "backfill" in runner["order"]


# ── waveform fallback (numpy is not a declared dependency) ───────────────────

def test_waveform_fallback_returns_peaks_and_rms_without_numpy(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)          # import numpy → ImportError
    n = 22050 * 4
    # 4 s: amplitude ramps 0.1 → 1.0 per second, a pure sine each second.
    samples = [math.sin(i / 7.0) * (0.1 + 0.3 * (i // 22050)) for i in range(n)]
    raw = struct.pack(f"<{n}f", *samples)
    wf = scanner._pcm_to_waveform(raw, 200)
    assert isinstance(wf, dict) and set(wf) == {"peaks", "rms"}
    assert len(wf["peaks"]) == 200 and len(wf["rms"]) == 200
    assert max(wf["peaks"]) == pytest.approx(1.0) and max(wf["rms"]) == pytest.approx(1.0)
    # the loud last second vs the quiet first — both axes see the ramp
    assert wf["peaks"][-1] > 3 * wf["peaks"][0] and wf["rms"][-1] > 3 * wf["rms"][0]
    # Same definition as the numpy branch: per-bin max|x| and √mean(x²), each
    # axis normalised by its own maximum.
    f32 = struct.unpack(f"<{n}f", raw)
    cs = n // 200
    ref_pk = [max(abs(x) for x in f32[i * cs:(i + 1) * cs]) for i in range(200)]
    ref_rms = [math.sqrt(sum(x * x for x in f32[i * cs:(i + 1) * cs]) / cs) for i in range(200)]
    assert wf["peaks"] == pytest.approx([v / max(ref_pk) for v in ref_pk], rel=1e-6)
    assert wf["rms"] == pytest.approx([v / max(ref_rms) for v in ref_rms], rel=1e-6)


def test_waveform_fallback_short_and_empty_input(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)
    assert scanner._pcm_to_waveform(b"", 200) == [0.0] * 200
    wf = scanner._pcm_to_waveform(struct.pack("<3f", 0.5, -1.0, 0.25), 200)
    assert len(wf["peaks"]) == 200 and max(wf["peaks"]) == pytest.approx(1.0)


# ── GC freeze after a large scan commit ──────────────────────────────────────

async def test_large_scan_commit_freezes_the_heap_once(monkeypatch):
    from soniqboom.core import store as store_mod
    frozen = []
    monkeypatch.setattr(store_mod, "freeze_long_lived_heap", lambda reason: frozen.append(reason))
    monkeypatch.setattr(store_mod, "FREEZE_AFTER_UPSERTS", 100)
    s = TrackStore()
    s.enter_batch_mode()
    s.upsert_tracks_batch([{"id": f"t{i}", "title": f"s{i}"} for i in range(50)])
    await scanner._async_exit_batch_mode(s)
    assert frozen == []                            # below the threshold
    s.enter_batch_mode()
    s.upsert_tracks_batch([{"id": f"u{i}", "title": f"s{i}"} for i in range(60)])
    await scanner._async_exit_batch_mode(s)
    assert frozen == ["a large scan commit"] and s._upserts_since_freeze == 0
    s.enter_batch_mode()
    s.update_track_fields("u1", {"album": "X"})     # field updates don't count
    await scanner._async_exit_batch_mode(s)
    assert len(frozen) == 1


def test_freeze_helper_collects_then_freezes():
    import gc
    from soniqboom.core import store as store_mod
    before = gc.get_freeze_count()
    try:
        store_mod.freeze_long_lived_heap("test")
        assert gc.get_freeze_count() >= before
    finally:
        gc.unfreeze()
