# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Performance guard rails for loudness matching and per-tune files
(features 2 and 7): what they add to UX-critical paths stays out of sight.

Budgets are several times the measured cost (macOS arm64, 2026-11-25:
public_track 3.4 µs a row, the cache-hit hook ~2 µs, a 1000-row slice of the
native pass's sweep ~1 ms, a Subsonic song ~10 µs), so a slow CI box passes
and a real regression — a row thrown off the listing's fast path (×6), a
file read or a model build on a hot path — does not."""
from __future__ import annotations

import gc
import time

import pytest

from soniqboom.core import loudness as L
from soniqboom.core import store as S


def _rows(n: int) -> list[dict]:
    from soniqboom.models.track import TrackMeta
    out = []
    for i in range(n):
        fmt, ext = (("FLAC", "flac"), ("SID", "sid"), ("ProTracker", "mod"), ("KSS", "kss"))[i % 4]
        r = TrackMeta(id=f"t{i}", path=f"/m/a{i % 50}/t{i}.{ext}", title=f"T {i}", format=fmt,
                      duration=100.0 + i % 50).model_dump()
        if i % 3 == 0:
            r["loudness"] = {"0": [-14.3, 0.912]}
            r["loudness_source"] = "analysed"
        if fmt == "KSS":
            r["subsongs"], r["tune_slots"] = 3, [0, 2, 5]
        out.append(r)
    return out


def _per_call_us(fn, items, reps: int = 3) -> float:
    gc.collect()
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        for x in items:
            fn(x)
        best = min(best, (time.perf_counter() - t0) / len(items) * 1e6)
    return best


def test_listing_rows_with_new_fields_stay_on_the_fast_path_and_cheap():
    from soniqboom.api import tracks as T
    rows = _rows(20000)
    assert all(T.public_track(r) is r for r in rows)
    assert _per_call_us(T.public_track, rows) < 15.0


def test_the_cache_hit_hook_is_cheap(monkeypatch):
    """Every cache hit (each Range request of a play) passes ``on_hit``: a
    measured tune costs a key parse and two dict lookups — no file, no task."""
    monkeypatch.setattr(S, "_store", S.TrackStore())
    st = S.get_store()
    rows = [r for r in _rows(4000) if r.get("loudness")]
    for r in rows:
        r["loudness_v"] = L.variant_of(f"{r['id']}__sub0__r3")     # (measured from this render)
    st.upsert_tracks_batch(rows)
    keys = [f"{r['id']}__sub0__r3" for r in rows]
    L.reset_for_tests()
    us = _per_call_us(lambda k: L.note_render_stored(k, "tracker", "/x.wav"), keys)
    assert not L._QUEUED, "a measured tune is never queued again"
    assert us < 25.0


@pytest.mark.asyncio
async def test_a_sweep_slice_of_the_native_pass_keeps_the_loop_responsive(monkeypatch):
    """The pass judges 1000 rows per loop turn (``native_candidate``: no file
    read) — a slice must stay far below a frame budget."""
    monkeypatch.setattr(L, "_NET_MOUNTS", ())
    rows = _rows(50000)
    gc.collect()
    worst = 0.0
    for i in range(0, len(rows), L._PASS_SLICE):
        t0 = time.perf_counter()
        sum(1 for r in rows[i:i + L._PASS_SLICE] if L.native_candidate(r))
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.020, f"{worst * 1000:.1f} ms for one slice"


def test_a_subsonic_song_with_a_measurement_costs_little_more():
    from soniqboom.api import subsonic as sub
    rows = _rows(8000)
    measured = [r for r in rows if r.get("loudness")]
    plain = [r for r in rows if not r.get("loudness")]
    a, b = _per_call_us(sub._track_to_song, plain), _per_call_us(sub._track_to_song, measured)
    assert b < a + 10.0 and b < 60.0, (a, b)
