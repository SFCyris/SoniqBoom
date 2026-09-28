# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-4: an index rebuild (reindex / integrity sweep) must not swap in
indexes built from a snapshot that a write landing during the build has
outdated, and must say so (``skipped``) so the sweep retries instead of
stamping its gate; the sweep gate is seeded with the same shape it compares.

These exercise ``core/data.py`` / ``core/index_health.py`` (the swap guard
compares ``TrackStore.index_generation()``); each test runs once that guard is
present there."""
from __future__ import annotations

import asyncio

import pytest

from soniqboom.core import data
from soniqboom.core import index_health
from soniqboom.core.store import TrackStore

_GUARDED = "index_generation" in data.rebuild_indexes.__code__.co_names
_GATE_HELPER = hasattr(index_health, "_gate_seq")


def _store(n: int = 3000) -> TrackStore:
    s = TrackStore()
    s.upsert_tracks_batch([
        {"id": f"t{i}", "path": f"/m/{i}.mod", "title": f"tune {i}", "artist": f"a{i % 50}",
         "album": f"al{i % 300}", "format": "ProTracker", "genre": ["Amiga"], "added_at": 1}
        for i in range(n)])
    return s


@pytest.mark.skipif(not _GUARDED, reason="data.rebuild_indexes has no index_generation guard yet")
async def test_a_write_during_the_build_skips_the_swap(monkeypatch):
    s = _store()
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: s)

    async def writer():
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        s.update_track_fields("t1", {"album": "Gold of the Aztecs", "album_source": "modland"})
        s.upsert_track({"id": "new", "path": "/m/new.sid", "title": "uridium",
                        "format": "SID", "genre": [], "added_at": 2})
        s.record_play("t2")

    report, _ = await asyncio.gather(data.rebuild_indexes(), writer())
    assert report["skipped"] == "concurrent-mutation"
    assert report["mismatches"] == []
    assert s.verify_indexes()["index_ok"]
    assert s._candidate_ids(game="uridium") == {"new"}
    # Quiet library: the rebuild swaps and reports no skip.
    report = await data.rebuild_indexes()
    assert not report.get("skipped") and report["index_ok"]


@pytest.mark.skipif(not _GUARDED, reason="data.rebuild_indexes has no index_generation guard yet")
async def test_a_skipped_sweep_retries_on_the_next_tick(monkeypatch):
    s = _store(10)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    calls = []

    async def rebuild():
        calls.append(1)
        return {"index_ok": True, "skipped": "concurrent-mutation", "mismatches": [],
                "track_count": 10, "mutation_seq": s._mutation_seq}
    monkeypatch.setattr("soniqboom.core.data.rebuild_indexes", rebuild)
    monkeypatch.setattr(index_health, "_last_swept_seq", None)
    recorded = []
    monkeypatch.setattr(index_health, "record", lambda *a, **kw: recorded.append(kw))
    assert await index_health._sweep_once() is None
    assert await index_health._sweep_once() is None
    assert len(calls) == 2 and recorded == []            # retried, never "healed"


@pytest.mark.skipif(not _GATE_HELPER, reason="index_health has no _gate_seq helper yet")
async def test_a_freshly_booted_library_is_not_rebuilt(monkeypatch):
    s = _store(10)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    calls = []

    async def rebuild():
        calls.append(1)
        return {"index_ok": True, "mismatches": [], "track_count": 10,
                "mutation_seq": s._mutation_seq}
    monkeypatch.setattr("soniqboom.core.data.rebuild_indexes", rebuild)
    monkeypatch.setattr(index_health, "_task", None)
    monkeypatch.setattr(index_health, "_last_swept_seq", None)
    index_health.start(interval=3600)
    try:
        assert await index_health._sweep_once() is None and calls == []
        s._duration_seq += 1
        await index_health._sweep_once()
        assert len(calls) == 1
        assert await index_health._sweep_once() is None and len(calls) == 1
    finally:
        index_health.stop()
