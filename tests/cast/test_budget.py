# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""First-byte budget gate.

The headline UX promise for cast / DLNA / AirPlay is *snappy first
audible byte across every supported format*.  This test enforces the
budget mechanically:

  • For each codec the negotiator can target, render N times against
    the same 2-second sine WAV source.
  • Measure milliseconds from spawn → first stdout chunk.
  • Compute the median (p50) — not the worst case, since cold-cache
    disk thrashing produces 5-10x variance for the first call on a
    laptop coming out of sleep.
  • Assert the p50 is under the per-codec budget below.

Budgets are calibrated against the Doherty-threshold heuristic (an
interaction feels "continuous" below 400 ms first byte; "good" below
500 ms).  Generous CI margin doubles the dev-box numbers because
GitHub Actions runners are slower and more variable than my Apple
silicon.

Marked ``budget`` — opt in with ``pytest -m budget``.  Not on by
default because each codec adds ~2-5 s to the suite (10-30 s total),
which is too much for a fast iteration cycle.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from pathlib import Path

import pytest

from soniqboom.core import cast_pipe


# Codec → p50 first-byte budget in milliseconds.  These are CI-friendly
# (2x the measured dev-box numbers).  If a number trips, the next step
# is to check whether ffmpeg's startup cost regressed (lib load, codec
# registry init) or whether the encoder itself slowed down.
BUDGETS_MS = {
    "mp3":  500,
    "flac": 500,
    "wav":  500,
    "ogg":  500,
    "opus": 500,
    "aac":  500,
}

# Iterations per codec.  More samples = tighter p50 estimate; this
# trades against suite wall-clock.  5 is the smallest N where p50 is
# meaningful (median of an even count drops back to the 2nd-3rd
# average, but 5 picks a stable middle value).
ITERATIONS = 5


@pytest.mark.budget
@pytest.mark.parametrize("codec", list(BUDGETS_MS.keys()))
async def test_first_byte_budget(sine_wav: Path, tmp_path: Path, codec: str):
    """For each codec: p50 first-byte < budget.

    Failure = either a real perf regression OR the dev box / CI runner
    is overloaded.  When a budget trips on CI, retry once before
    treating it as broken — both legitimate causes deserve
    investigation, but transient runner load is the more common one.
    """
    samples_ms: list[int] = []
    for i in range(ITERATIONS):
        sink = tmp_path / f"out_{codec}_{i}.bin"
        started = time.monotonic()
        first_byte_at: float | None = None
        gen = cast_pipe.render_stream(
            sine_wav, codec=codec, cache_sink=sink,
        )
        try:
            async for _chunk in gen:
                if first_byte_at is None:
                    first_byte_at = time.monotonic()
                    break
        finally:
            # Generator's finally clause kills the still-running ffmpeg.
            await gen.aclose()
        assert first_byte_at is not None, f"codec={codec} run {i}: no bytes"
        samples_ms.append(int(round((first_byte_at - started) * 1000)))
        # Wipe the .partial so the next iteration is a fresh cold start
        for stale in (sink, sink.with_suffix(sink.suffix + ".partial")):
            if stale.exists():
                stale.unlink()

    p50 = int(statistics.median(samples_ms))
    p95 = sorted(samples_ms)[int(round(0.95 * (len(samples_ms) - 1)))]
    budget = BUDGETS_MS[codec]

    print(
        f"\n  budget[{codec}]: samples={samples_ms} "
        f"p50={p50}ms p95={p95}ms budget={budget}ms"
    )
    assert p50 < budget, (
        f"{codec}: p50 first-byte {p50} ms ≥ budget {budget} ms.  "
        f"Raw samples: {samples_ms}"
    )
