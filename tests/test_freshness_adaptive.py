# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixes H + I for the adaptive remote-freshness poller (remote_freshness.py).

H — `_poll_share` must consume the plan RETURNED by `start_remote_scan`, never
    the process-global `get_progress().last_plan`.  That global is last-writer-
    wins, so a concurrent sibling-share scan would make one share record the
    OTHER share's new-track count into its cadence and toast the wrong share.

I — `adaptive_interval` must relax toward the 4h max during a dry spell.  Before
    the fix, a share that burst-changed once (or never changes) stayed pinned
    near the 5-min floor / 30-min cold-start forever, contradicting the
    documented "stable → 4h" behaviour.
"""
import time

import pytest

from soniqboom.core import remote_freshness as RF
from soniqboom.core import scanner
from soniqboom.core.remote_freshness import ShareState


# ── I: adaptive_interval relaxation ────────────────────────────────────────
def test_busy_share_just_changed_polls_fast():
    st = ShareState(scan_root="ftp://h/s", change_intervals=[60, 60, 60])
    st.quiet_since_ts = time.time()           # a change just happened
    # median=60 → base=30s → clamped up to the 5-min floor; dry≈0 has no effect.
    assert st.adaptive_interval() == RF._MIN_INTERVAL_S


def test_bursty_then_quiet_share_relaxes_to_max():
    st = ShareState(scan_root="ftp://h/s", change_intervals=[60, 60, 60])
    st.quiet_since_ts = time.time() - 9 * 3600   # bursted once, quiet 9h since
    # dry*0.5 = 4.5h → clamped to the 4h max, overriding the stale 60s median.
    assert st.adaptive_interval() == RF._MAX_INTERVAL_S


def test_never_changing_share_relaxes_to_max():
    # No samples at all (cold-start 30 min) but quiet for a long time → 4h.
    st = ShareState(scan_root="ftp://h/s")
    st.quiet_since_ts = time.time() - 9 * 3600
    assert st.adaptive_interval() == RF._MAX_INTERVAL_S


def test_cold_start_without_dry_spell_is_30min():
    st = ShareState(scan_root="ftp://h/s")
    st.quiet_since_ts = time.time()           # just seeded, no dry spell yet
    assert st.adaptive_interval() == RF._COLD_START_INTERVAL_S


def test_record_change_restarts_dry_clock_and_persists():
    st = ShareState(scan_root="ftp://h/s")
    st.quiet_since_ts = time.time() - 10_000
    now = time.time()
    st.record_change(5, now)
    assert st.quiet_since_ts == now           # a change ends the dry spell
    assert st.total_new_tracks == 5
    # round-trips through the JSON sidecar
    assert ShareState.from_json(st.to_json()).quiet_since_ts == now


# ── H: _poll_share consumes the returned plan, never the global ────────────
@pytest.mark.asyncio
async def test_poll_share_uses_returned_plan_not_global(monkeypatch):
    RF._reg.states.clear()
    RF._reg.inflight.clear()
    RF._reg.state_file = None                 # _save_state becomes a no-op
    RF._reg.source_lookup = lambda sr: object()
    toasts: list = []

    async def _cb(sr, n, titles):
        toasts.append((sr, n))

    RF._reg.on_new_tracks = _cb

    # Poison the process-global with a DIFFERENT share's huge count. If
    # _poll_share still read it, `new` would be 999 and the assertions fail.
    scanner._progress.last_plan = {
        "scan_root": "ftp://other/share", "new": 999, "extract": 999, "walked": 999,
    }

    async def _fake_start(share_id, scan_root, source, on_progress=None, *,
                          dir_mtime_cap=None):
        return {"scan_root": scan_root, "new": 3, "extract": 3,
                "walked": 10, "skip": 7}

    monkeypatch.setattr(scanner, "start_remote_scan", _fake_start)

    plan = await RF._poll_share("ftp://h/s", reason="tick")
    assert plan.get("new") == 3               # the RETURNED plan, not the global's 999
    st = RF._reg.states["ftp://h/s"]
    assert st.total_new_tracks == 3           # cadence recorded the right count
    assert toasts == [("ftp://h/s", 3)]       # toast attributed to the right share


@pytest.mark.asyncio
async def test_poll_share_deduped_empty_plan_records_nothing(monkeypatch):
    RF._reg.states.clear()
    RF._reg.inflight.clear()
    RF._reg.state_file = None
    RF._reg.source_lookup = lambda sr: object()
    toasts: list = []
    RF._reg.on_new_tracks = lambda sr, n, t: toasts.append((sr, n))

    # Stale global that would wrongly re-toast old arrivals if consumed.
    scanner._progress.last_plan = {"scan_root": "ftp://h/s", "new": 42}

    async def _fake_start(*a, **k):
        return {}                             # deduped / aborted → empty plan

    monkeypatch.setattr(scanner, "start_remote_scan", _fake_start)

    plan = await RF._poll_share("ftp://h/s", reason="tick")
    assert plan == {}
    assert RF._reg.states["ftp://h/s"].total_new_tracks == 0
    assert toasts == []


# ── I (2nd round): full-walk wall-clock ceiling decouples ghost cleanup ─────
def _arm_capture(monkeypatch, *, full_walk_ok=None):
    RF._reg.states.clear()
    RF._reg.inflight.clear()
    RF._reg.state_file = None
    RF._reg.source_lookup = lambda sr: object()
    RF._reg.on_new_tracks = None
    captured: dict = {}

    async def _fake_start(share_id, scan_root, source, on_progress=None, *,
                          dir_mtime_cap=None):
        captured["cap"] = dir_mtime_cap
        plan = {"scan_root": scan_root, "new": 0, "extract": 0, "walked": 5}
        # By default mirror the scanner: a full walk (cap None) that saw
        # entries is full_walk_ok.  Override to force the not-completed case.
        plan["full_walk_ok"] = (dir_mtime_cap is None) if full_walk_ok is None else full_walk_ok
        return plan

    monkeypatch.setattr(scanner, "start_remote_scan", _fake_start)
    return captured


@pytest.mark.asyncio
async def test_full_walk_ceiling_forces_full_walk_when_relaxed(monkeypatch):
    captured = _arm_capture(monkeypatch)
    st = RF._reg.states.setdefault("ftp://h/s", ShareState(scan_root="ftp://h/s"))
    st.total_polls = 3                              # not a drift-sweep multiple, not cold-start
    st.last_check_ts = time.time() - 300
    st.last_full_walk_ts = time.time() - (RF._FULL_WALK_CEILING_S + 600)  # overdue
    await RF._poll_share("ftp://h/s", reason="tick")
    assert captured["cap"] is None                  # ceiling forced a FULL walk
    assert st.last_full_walk_ts >= st.last_check_ts  # stamp refreshed (full_walk_ok True)


@pytest.mark.asyncio
async def test_fast_walk_when_ceiling_not_reached(monkeypatch):
    captured = _arm_capture(monkeypatch)
    st = RF._reg.states.setdefault("ftp://h/s", ShareState(scan_root="ftp://h/s"))
    st.total_polls = 3
    st.last_check_ts = time.time()
    st.last_full_walk_ts = time.time() - 60         # just did a full walk → not overdue
    await RF._poll_share("ftp://h/s", reason="tick")
    assert captured["cap"] is not None              # a capped (fast) walk, cleanup deferred


@pytest.mark.asyncio
async def test_ceiling_clock_not_stamped_when_full_walk_did_not_complete(monkeypatch):
    # Finding I2: a full walk was REQUESTED (cap None) but ghost cleanup did
    # NOT run (deduped / crashed / partial → full_walk_ok False).  The ceiling
    # clock must NOT advance, or cleanup would be deferred indefinitely on a
    # chronically-degraded share while telemetry claims a clean full walk.
    captured = _arm_capture(monkeypatch, full_walk_ok=False)
    st = RF._reg.states.setdefault("ftp://h/s", ShareState(scan_root="ftp://h/s"))
    st.total_polls = 3
    st.last_check_ts = time.time() - 300
    overdue_ts = time.time() - (RF._FULL_WALK_CEILING_S + 600)
    st.last_full_walk_ts = overdue_ts
    await RF._poll_share("ftp://h/s", reason="tick")
    assert captured["cap"] is None                  # ceiling still forced the attempt…
    assert st.last_full_walk_ts == overdue_ts       # …but the clock did NOT advance


def test_last_full_walk_ts_round_trips():
    st = ShareState(scan_root="ftp://h/s")
    st.last_full_walk_ts = 12345.0
    assert ShareState.from_json(st.to_json()).last_full_walk_ts == 12345.0
