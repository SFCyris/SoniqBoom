# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F1 — a reactively-lowered FTP detected cap must DECAY back up after a
trip-free spell, so a TRANSIENT 421/530 (temporary server overload) doesn't
pin the pool below the user's configured budget forever (only a manual reset
recovered it before).

`relax_detected_cap` is an AIMD probe: +1 per `_CAP_DECAY_INTERVAL_S` with no
new trip; a fresh trip re-stamps the clock and blocks it; once the cap would
stop clamping below the configured budget the entry is removed entirely.
"""
import pytest

from soniqboom.core import ftp_pool_config as C


@pytest.fixture
def clock(tmp_path, monkeypatch):
    """Fresh per-test state dir + a controllable clock."""
    C.init(tmp_path)                       # sets data_dir, invalidates cache
    t = {"now": 1_000_000}
    monkeypatch.setattr(C.time, "time", lambda: t["now"])
    return t


def test_relax_absent_entry_is_noop(clock):
    assert C.relax_detected_cap("h", 21, 10) is None


def test_relax_too_soon_leaves_cap_unchanged(clock):
    C.record_too_many_clients("h", 21, 6)          # cap → 5, learned_at = now
    assert C.get_detected_cap("h", 21) == 5
    clock["now"] += 60                              # only a minute passes
    assert C.relax_detected_cap("h", 21, 10) == 5  # not trip-free long enough
    assert C.get_detected_cap("h", 21) == 5


def test_relax_after_interval_raises_by_one_and_resets_clock(clock):
    C.record_too_many_clients("h", 21, 6)          # cap → 5
    clock["now"] += C._CAP_DECAY_INTERVAL_S + 1
    assert C.relax_detected_cap("h", 21, 20) == 6  # +1
    assert C.get_detected_cap("h", 21) == 6
    # clock was reset → an immediate second relax is a no-op (AIMD: +1/interval)
    assert C.relax_detected_cap("h", 21, 20) == 6
    # …and only after ANOTHER full interval does it step again
    clock["now"] += C._CAP_DECAY_INTERVAL_S + 1
    assert C.relax_detected_cap("h", 21, 20) == 7


def test_relax_stops_and_preserves_cap_once_non_binding(clock):
    # observed 4 → cap 3, budget 3.  Raise to 4 (detected-1 == 3 == budget),
    # then STOP — the entry is preserved (NOT deleted), so a later budget raise
    # re-binds it instead of re-learning via a trip.
    C.record_too_many_clients("h", 21, 4)          # cap → 3
    clock["now"] += C._CAP_DECAY_INTERVAL_S + 1
    assert C.relax_detected_cap("h", 21, 3) == 4   # one step to the budget
    clock["now"] += C._CAP_DECAY_INTERVAL_S + 1
    assert C.relax_detected_cap("h", 21, 3) == 4   # non-binding now → no change
    assert C.get_detected_cap("h", 21) == 4        # preserved, not removed


def test_relax_preserves_cap_when_budget_lowered_below_it(clock):
    # A still-valid learned cap must NOT be deleted just because the user
    # lowered the budget beneath it — a future budget raise must re-bind it.
    C.record_too_many_clients("h", 21, 6)          # cap → 5
    clock["now"] += C._CAP_DECAY_INTERVAL_S + 1
    assert C.relax_detected_cap("h", 21, 3) == 5   # budget 3 < cap 5 → no-op
    assert C.get_detected_cap("h", 21) == 5        # intact for a future raise


def test_a_fresh_trip_restamps_the_clock_and_blocks_relax(clock):
    C.record_too_many_clients("h", 21, 6)          # cap 5, learned_at t0
    clock["now"] += C._CAP_DECAY_INTERVAL_S + 1    # interval elapses…
    C.record_too_many_clients("h", 21, 6)          # …but a NEW trip re-stamps
    assert C.relax_detected_cap("h", 21, 20) == 5  # blocked — server still capped


def test_repeated_relax_walks_cap_up_to_budget_then_stops(clock):
    C.record_too_many_clients("h", 21, 4)          # cap → 3, budget 6
    caps = []
    for _ in range(6):
        clock["now"] += C._CAP_DECAY_INTERVAL_S + 1
        caps.append(C.relax_detected_cap("h", 21, 6))
    # 3 → 4 → 5 → 6 → 7, then (7-1==6==budget) non-binding → holds at 7.
    assert caps == [4, 5, 6, 7, 7, 7]
    assert C.get_detected_cap("h", 21) == 7        # preserved at budget+1


def test_none_ceiling_is_a_noop(clock):
    C.record_too_many_clients("h", 21, 6)          # cap → 5
    clock["now"] += C._CAP_DECAY_INTERVAL_S + 1
    assert C.relax_detected_cap("h", 21, None) == 5  # can't judge binding → no raise
    assert C.get_detected_cap("h", 21) == 5


def test_manual_pin_is_exempt_from_decay(clock):
    C.set_detected_cap("h", 21, 5)                 # admin/probe pin → manual
    clock["now"] += C._CAP_DECAY_INTERVAL_S * 10   # ages a long time
    assert C.relax_detected_cap("h", 21, 20) == 5  # never auto-decays
    assert C.get_detected_cap("h", 21) == 5


def test_manual_pin_survives_a_pool_local_trip(clock):
    # A manual pin must NOT be lowered by a reactive trip: observed_in_use is a
    # POOL-LOCAL count, so on a shared server an external client's saturation
    # trips us falsely — exactly what the pin exists to override.  The trip is
    # counted for visibility but the pinned value stands.
    C.set_detected_cap("h", 21, 8)                 # pinned at 8
    got = C.record_too_many_clients("h", 21, 6)    # pool-local trip at 6
    assert got == 8                                # returned the pinned value, not 5
    assert C.get_detected_cap("h", 21) == 8        # cap unchanged
    assert C._load()["h:21"]["manual"] is True     # still pinned
    assert C._load()["h:21"]["trip_count"] == 1    # …but the trip was recorded
    # …and it still never auto-decays
    clock["now"] += C._CAP_DECAY_INTERVAL_S + 1
    assert C.relax_detected_cap("h", 21, 20) == 8


def test_probe_floor_is_decay_eligible(clock):
    # A probe that ran its whole range WITHOUT a rejection found only a FLOOR,
    # not the limit — record it manual=False so decay can creep it to budget
    # (pinning a floor would strand the pool below the real limit).
    C.set_detected_cap("h", 21, 16, manual=False)  # probe floor
    assert C._load()["h:21"]["manual"] is False
    clock["now"] += C._CAP_DECAY_INTERVAL_S + 1
    assert C.relax_detected_cap("h", 21, 30) == 17  # decays toward budget 30


def test_reactive_trip_no_longer_touches_manual_flag_on_unpinned(clock):
    # An ordinary (unpinned) reactive trip lowers the cap and leaves it
    # decay-eligible (no lingering manual key from a prior pin path).
    C.record_too_many_clients("h", 21, 6)          # cap → 5
    assert C.get_detected_cap("h", 21) == 5
    assert C._load()["h:21"].get("manual") in (False, None)


def test_relax_fails_closed_on_missing_learned_at(clock):
    C.set_detected_cap("h", 21, 5)
    entry = C._load()["h:21"]
    del entry["learned_at"]                        # corrupt: no timestamp
    entry["manual"] = False                        # make it decay-eligible
    # missing learned_at must NOT relax immediately (fail closed, not open)
    assert C.relax_detected_cap("h", 21, 20) == 5
    assert C.get_detected_cap("h", 21) == 5


def test_relax_ignores_corrupt_entry(clock):
    C.set_detected_cap("h", 21, 5)
    C._load()["h:21"]["detected_cap"] = "nope"     # corrupt the cap in place
    assert C.relax_detected_cap("h", 21, 20) is None


def test_record_and_set_survive_a_corrupt_non_dict_entry(clock):
    # A hand-edited caps JSON can leave a stray non-dict value where an entry
    # belongs.  record/set must not raise (the read paths already guarded).
    C._load()["h:21"] = "garbage"                  # truthy non-dict
    C.record_too_many_clients("h", 21, 6)          # replaces it, no crash
    assert C.get_detected_cap("h", 21) == 5
    C._load()["h:21"] = 12345                       # non-dict again
    C.set_detected_cap("h", 21, 8)                  # no crash
    assert C.get_detected_cap("h", 21) == 8


def test_env_int_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("SONIQBOOM_FTP_CAP_DECAY_S", "not-a-number")
    assert C._env_int("SONIQBOOM_FTP_CAP_DECAY_S", 12345) == 12345
    monkeypatch.setenv("SONIQBOOM_FTP_CAP_DECAY_S", "600")
    assert C._env_int("SONIQBOOM_FTP_CAP_DECAY_S", 12345) == 600
