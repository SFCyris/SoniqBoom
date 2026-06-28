# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Runtime index-integrity: a background drift sweep + cached health state.

The TrackStore's derived indexes (``_tag_*``, ``_sorted_*``, ``_agg_*``,
``_word_index``, ``_unplayed_ids``) are maintained incrementally on every
mutation.  If any mutation path mis-maintains one, it drifts out of sync with
``_tracks`` SILENTLY — producing fast-but-wrong results (e.g. a format filter
returning 0) with no error, no hung request, and no log anomaly — until a
restart's full rebuild quietly fixes it.

This module is the missing runtime safety net:

* a low-frequency background **sweep** that, only when the library has actually
  changed since the last check, runs ``data.rebuild_indexes`` (the non-blocking
  atomic shadow-swap, which both DIAGNOSES drift and HEALS it in one step) and
  records whether drift was found;
* a cached **health** snapshot for ``GET /admin/stats`` so that endpoint can
  report a real ``index_ok`` instead of a hardcoded ``True``.

Detection without self-healing would just be an alarm; healing without
detection would hide bugs.  The sweep does both and logs a warning when it had
to correct drift, so a recurring incremental-maintenance bug becomes visible.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger("soniqboom.core.index_health")

DEFAULT_INTERVAL_SECONDS = 1800.0  # 30 min; override via SONIQBOOM_INDEX_SWEEP_SECONDS, <=0 disables

# Cached last-known health.  Seeded optimistic because the boot rebuild
# (populate_store -> store.rebuild_indexes) is authoritative, so indexes are
# correct at startup; ``checked_at=None`` marks this as boot-assumed, not yet
# independently verified.
_state: dict = {
    "index_ok": True,
    "checked_at": None,            # unix ts of the last real verification
    "last_check_kind": "boot",     # boot | sweep | reindex | manual
    "mutation_seq": None,
    "track_count": None,
    "drift_detected_total": 0,
    "auto_heal_total": 0,
    "last_mismatches": [],
}
_task: asyncio.Task | None = None
_last_swept_seq: int | None = None   # store._mutation_seq at the last sweep rebuild (gate)


def snapshot() -> dict:
    """Return the last-known index health (used by GET /admin/stats)."""
    return dict(_state)


def record(report: dict, kind: str, *, healed: bool) -> None:
    """Fold a verify/rebuild drift report into the cached health state.

    ``healed`` = did this operation REBUILD the indexes (reindex / sweep)?  If
    so the indexes are correct NOW even when drift was found pre-heal, so
    ``index_ok`` reflects the healthy post-heal state; a non-healing verify
    (``healed=False``) reports the actual current state.  Either way the drift,
    if any, is counted and its mismatches retained.
    """
    drift = not report.get("index_ok", True)
    _state["index_ok"] = True if healed else (not drift)
    _state["checked_at"] = time.time()
    _state["last_check_kind"] = kind
    _state["mutation_seq"] = report.get("mutation_seq")
    _state["track_count"] = report.get("track_count")
    _state["last_mismatches"] = list(report.get("mismatches", []))[:10]
    if drift:
        _state["drift_detected_total"] += 1
        if healed:
            _state["auto_heal_total"] += 1


async def _sweep_once() -> dict | None:
    """One gated sweep tick.  Rebuilds (heals) iff the library changed since the
    last sweep and no scan is running; records health.  Returns the drift
    report, or None if skipped."""
    global _last_swept_seq
    from soniqboom.core.store import get_store
    from soniqboom.core.data import rebuild_indexes
    store = get_store()
    seq = store._mutation_seq
    if _last_swept_seq == seq:
        return None  # nothing has mutated since the last rebuild -> no new drift possible
    # Don't fight an in-progress scan: it churns _tracks heavily and rebuilds the
    # sorted indexes on exit; we'll re-check next tick once it settles.
    try:
        from soniqboom.core import scanner
        if scanner.is_scanning():
            return None
    except Exception:
        pass
    report = await rebuild_indexes()                  # non-blocking shadow-swap: diagnoses + heals
    _last_swept_seq = store._mutation_seq             # post-rebuild (includes the rebuild's own bump)
    drift = not report.get("index_ok", True)
    record(report, kind="sweep", healed=True)         # the rebuild healed it; index_ok is True now
    if drift:
        log.warning(
            "Index integrity sweep DETECTED DRIFT and auto-healed it "
            "(track_count=%s): %s",
            report.get("track_count"), report.get("mismatches"),
        )
    return report


async def _sweep_loop(interval: float) -> None:
    log.info("Index integrity sweep running every %.0fs", interval)
    # First tick sooner so drift introduced by an early-session mutation (before
    # one full interval elapses) is caught within minutes — narrows the window
    # where /admin/stats reports the optimistic boot-assumed ``index_ok``.
    # Still gated on _mutation_seq, so a quiescent library does nothing.
    delay = min(interval, 300.0)
    while True:
        try:
            await asyncio.sleep(delay)
            delay = interval
            await _sweep_once()
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Index integrity sweep iteration failed (continuing)")


def start(interval: float | None = None) -> asyncio.Task | None:
    """Start the background sweep (idempotent).  Seeds the gate with the current
    mutation seq so a freshly-booted, healthy library is not rebuilt until
    something actually mutates."""
    global _task, _last_swept_seq
    if _task is not None and not _task.done():
        return _task
    if interval is None:
        try:
            interval = float(os.environ.get("SONIQBOOM_INDEX_SWEEP_SECONDS", DEFAULT_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL_SECONDS
    if interval <= 0:
        log.info("Index integrity sweep disabled (interval <= 0)")
        return None
    try:
        from soniqboom.core.store import get_store
        _last_swept_seq = get_store()._mutation_seq
    except Exception:
        _last_swept_seq = None
    _task = asyncio.create_task(_sweep_loop(interval))
    return _task


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
