# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Adaptive remote freshness — background polling for remote shares (FTP / SMB /
WebDAV) that don't support push notifications.

Design
──────
Each remote share gets its own async coroutine that sleeps for an
*adaptive* interval, then triggers ``start_remote_scan`` to discover
any changes.  The interval is computed from the EMA of observed
inter-arrival times of changes:

    interval = clamp(median(recent_inter_arrival[-20:]) × 0.5,
                     min=5 min, max=4 h)
              + jitter([-30s, +30s])

A share that changes 5 times per hour gets polled every ~6 min.  A
share that's been stable for a week gets polled every 4 h.  Brand new
shares with no history default to 30 min.  Stage 1 cold-start is
hardcoded to 30 min until enough samples accumulate (≥3).

Pool-budget awareness
─────────────────────
A background tick is *skipped* (interval restarts) if the share's FTP
pool reports any ``waiting_scan > 0``.  User-visible work — manual
re-index, on-demand stream — has priority.  ``check_now`` ignores
this gate (it's user-triggered, the user is actively waiting).

Persistence
───────────
Per-share state (last_check, last_change_seen, change_history) is
written to ``{data_dir}/freshness_state.json`` on every state mutation
so cadence survives restart.  The file is small (<5 KB for 100
shares).  Atomic rename so a crash mid-write doesn't corrupt it.

Triggers
────────
1. **Adaptive background tick** — the main loop above.
2. **check_now(share)** — fire from admin UI / stream 404 hook.  Skips
   the dedupe guard if forced.
3. **on_share_added(share)** — admin adds a new share → arm a fresh
   coroutine.
4. **on_share_removed(share)** — admin removes share → cancel coro.

Telemetry
─────────
Every poll cycle logs a single structured line at INFO:

    freshness: share=ftp://… walked=N fresh=M skip=K mtime_cap_skip=L
                latency_ms=T cadence_min=I.II reason=tick|check_now|stream_404

(``reason`` makes the log searchable for why a given poll happened.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import statistics
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# ── Tuning knobs ────────────────────────────────────────────────────────────

# Adaptive cadence bounds (seconds).
_MIN_INTERVAL_S = 5 * 60        # never poll faster than every 5 min
_MAX_INTERVAL_S = 4 * 60 * 60   # never sleep longer than 4 h
_COLD_START_INTERVAL_S = 30 * 60  # default before enough history

# Jitter applied to every computed interval (avoid lock-step polling).
_JITTER_S = 30

# Minimum number of change samples before adaptive math kicks in.
_ADAPTIVE_MIN_SAMPLES = 3

# Maximum number of inter-arrival samples kept per share (recency-weighted).
_CHANGE_HISTORY_CAP = 20

# Skip a background tick if the FTP pool has this many or more callers
# blocked on the scan lane.  Doesn't apply to ``check_now`` triggers.
_POOL_WAIT_SKIP_THRESHOLD = 1

# How long a "check_now" task is allowed to be in-flight before we consider
# it stuck (and refuse to start another concurrent one for the same share).
_INFLIGHT_TIMEOUT_S = 600  # 10 min — large libraries can take this long


# ── Per-share state ─────────────────────────────────────────────────────────

@dataclass
class ShareState:
    """Per-share freshness state.

    Persisted across restarts via JSON sidecar.  All time fields are
    Unix epoch seconds.
    """
    scan_root: str
    last_check_ts: float = 0.0
    last_change_ts: float = 0.0
    # Recent inter-arrival times (seconds between change observations).
    # Deque-like list capped at _CHANGE_HISTORY_CAP entries.
    change_intervals: list[float] = field(default_factory=list)
    # Total counters since first arm (for telemetry).
    total_polls: int = 0
    total_new_tracks: int = 0
    # Computed once per tick — what the NEXT tick will sleep for.
    # Surfaced via /admin/freshness/status so the UI shows "Next in N min".
    next_check_ts: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "ShareState":
        return cls(
            scan_root=d.get("scan_root", ""),
            last_check_ts=float(d.get("last_check_ts", 0.0)),
            last_change_ts=float(d.get("last_change_ts", 0.0)),
            change_intervals=[float(x) for x in d.get("change_intervals", [])],
            total_polls=int(d.get("total_polls", 0)),
            total_new_tracks=int(d.get("total_new_tracks", 0)),
            next_check_ts=float(d.get("next_check_ts", 0.0)),
        )

    def record_change(self, count: int, now_ts: float) -> None:
        """Update interval history after a poll finds *count* new tracks."""
        if count <= 0:
            return  # no change → don't update last_change_ts
        if self.last_change_ts > 0:
            delta = now_ts - self.last_change_ts
            if delta > 0:
                self.change_intervals.append(delta)
                # Bound the history (oldest dropped).
                if len(self.change_intervals) > _CHANGE_HISTORY_CAP:
                    self.change_intervals = self.change_intervals[-_CHANGE_HISTORY_CAP:]
        self.last_change_ts = now_ts
        self.total_new_tracks += count

    def adaptive_interval(self) -> float:
        """Compute the next sleep interval (seconds) based on observed history.

        Cold start (<_ADAPTIVE_MIN_SAMPLES) → 30 min default.
        Otherwise: clamp(median(intervals) × 0.5, min=5min, max=4h).
        """
        if len(self.change_intervals) < _ADAPTIVE_MIN_SAMPLES:
            return _COLD_START_INTERVAL_S
        median = statistics.median(self.change_intervals)
        target = median * 0.5
        return max(_MIN_INTERVAL_S, min(_MAX_INTERVAL_S, target))


# ── Module-level state ──────────────────────────────────────────────────────

class _Registry:
    """All freshness state lives here.  Singleton pattern."""
    states: dict[str, ShareState] = {}
    tasks: dict[str, asyncio.Task] = {}
    inflight: dict[str, float] = {}   # share → ts when current check_now started
    enabled: bool = False
    data_dir: Path | None = None
    state_file: Path | None = None
    # Callback fired after every scan that found new tracks.  Wired by
    # main.py at startup so we can broadcast a WS event.
    on_new_tracks: Callable[[str, int, list[str]], Awaitable[None]] | None = None
    # Set by main.py at startup — used by ``check_now`` to look up the
    # FileSource for the share without importing scanner directly.
    source_lookup: Callable[[str], object] | None = None


_reg = _Registry()


# ── Persistence ─────────────────────────────────────────────────────────────

def _load_state() -> None:
    """Load per-share state from the JSON sidecar (best-effort)."""
    if _reg.state_file is None or not _reg.state_file.exists():
        return
    try:
        data = json.loads(_reg.state_file.read_text("utf-8"))
    except Exception as exc:
        log.warning("freshness: could not read %s: %s", _reg.state_file, exc)
        return
    if not isinstance(data, dict):
        return
    for scan_root, st_dict in data.items():
        try:
            _reg.states[scan_root] = ShareState.from_json(st_dict)
        except Exception as exc:
            log.warning("freshness: skipping malformed entry for %s: %s",
                        scan_root, exc)


def _save_state() -> None:
    """Atomically write state to disk."""
    if _reg.state_file is None:
        return
    try:
        tmp = _reg.state_file.with_suffix(".json.tmp")
        payload = {sr: st.to_json() for sr, st in _reg.states.items()}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(_reg.state_file)
    except Exception as exc:
        log.warning("freshness: state save failed: %s", exc)


# ── Adaptive math helpers ───────────────────────────────────────────────────

def _next_tick_seconds(st: ShareState) -> float:
    """Compute the next-tick sleep duration with jitter applied."""
    base = st.adaptive_interval()
    jitter = random.uniform(-_JITTER_S, _JITTER_S)
    return max(_MIN_INTERVAL_S, base + jitter)


def _pool_has_scan_pressure(scan_root: str) -> bool:
    """Return True iff the share's FTP pool currently has scan-lane
    waiters.  Means user-visible work is contending for connections;
    a background poll would only make it worse.

    Best-effort: any exception → assume no pressure (don't block the
    poll on diagnostic failures).
    """
    try:
        # Lazy import — freshness loads before scanner sometimes.
        from soniqboom.core.filesource import get_source
        source = get_source(scan_root)
        if source is None or not hasattr(source, "_pool"):
            return False
        status = source._pool.status()
        return int(status.get("waiting_scan", 0)) >= _POOL_WAIT_SKIP_THRESHOLD
    except Exception:
        return False


# ── Per-share polling loop ──────────────────────────────────────────────────

async def _share_poll_loop(scan_root: str) -> None:
    """One asyncio.Task per share — sleep, poll, repeat.

    Cancellation propagates from the parent (``stop()``).
    """
    st = _reg.states.setdefault(scan_root, ShareState(scan_root=scan_root))
    # On startup, honour any persisted next_check_ts so we don't poll
    # everything in unison.  If the persisted time has passed, run
    # immediately.  Add a small random offset to handle the case where
    # many shares were saved with the same timestamp.
    initial_sleep = max(0.0, st.next_check_ts - time.time())
    if initial_sleep > 0:
        # Always respect at least 5s grace so the server has time to
        # finish startup before we hammer it with polls.
        initial_sleep = max(5.0, initial_sleep + random.uniform(0, 30))
    else:
        # Stagger the first polls across shares so we don't all fire
        # in the same instant.
        initial_sleep = random.uniform(10, 60)
    log.info(
        "freshness: arm %s (first poll in %.0fs, samples=%d)",
        scan_root, initial_sleep, len(st.change_intervals),
    )

    try:
        await asyncio.sleep(initial_sleep)
        while _reg.enabled:
            # Skip if user work is loading the pool.
            if _pool_has_scan_pressure(scan_root):
                log.info(
                    "freshness: %s skip tick — pool has scan waiters",
                    scan_root,
                )
                await asyncio.sleep(60)  # short retry — pool may free up soon
                continue
            await _poll_share(scan_root, reason="tick")
            # Compute next interval, persist, sleep.
            interval = _next_tick_seconds(st)
            st.next_check_ts = time.time() + interval
            _save_state()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        log.info("freshness: %s loop cancelled", scan_root)
        raise
    except Exception:
        log.exception("freshness: %s loop crashed — will not restart", scan_root)


async def _poll_share(scan_root: str, *, reason: str) -> dict:
    """Trigger one freshness scan for *scan_root*.

    Returns the scan plan dict from the scanner (or an empty dict on
    early-skip / failure).
    """
    st = _reg.states.setdefault(scan_root, ShareState(scan_root=scan_root))
    now = time.time()

    # Concurrency guard: don't start a second poll for the same share
    # if one is in-flight.  The scanner itself dedupes via
    # _current_remote_dirs but we want to avoid even invoking the
    # scanner if we already kicked it off.
    inflight_started = _reg.inflight.get(scan_root)
    if inflight_started is not None:
        age = now - inflight_started
        if age < _INFLIGHT_TIMEOUT_S:
            log.info(
                "freshness: %s skip (%s) — already in-flight for %.0fs",
                scan_root, reason, age,
            )
            return {}
        log.warning(
            "freshness: %s stale inflight marker (age=%.0fs) — clearing",
            scan_root, age,
        )

    # Look up the FileSource — registry is owned by filesource.get_source.
    source_lookup = _reg.source_lookup
    if source_lookup is None:
        log.warning("freshness: no source_lookup configured — skipping %s",
                    scan_root)
        return {}
    try:
        source = source_lookup(scan_root)
    except Exception as exc:
        log.warning("freshness: source_lookup(%s) failed: %s", scan_root, exc)
        return {}
    if source is None:
        log.debug("freshness: %s has no source (probably disconnected)", scan_root)
        return {}

    # Compute dir-mtime cap for the fast-path walk.
    #
    # ``cap = last_check_ts - safety_buffer`` so we re-walk any
    # subtree whose dir.mtime is >= our last walk start.  Safety
    # buffer (15 min) absorbs clock skew between client and FTP
    # server and any 1-tick-late writes.
    #
    # On the FIRST poll OR the FIRST TWO POLLS overall we pass
    # ``None`` to force full walks (cold-start can't trust the
    # cap yet).  After that, every 5th poll is also a full walk
    # (drift sweep) — guards against the case where the server
    # doesn't update parent-dir mtime on child changes (some FTP
    # servers don't).  At 30-min adaptive cadence, drift sweep
    # fires every 2.5 hours; at the 5-min lower bound it fires
    # every 25 min.
    #
    # Reason= telemetry surfaces 'fast' vs 'full' so the operator
    # can see what the cap is actually saving.
    DRIFT_SWEEP_EVERY = 5
    SAFETY_BUFFER_S = 15 * 60
    cap_reason = "fast"
    if st.last_check_ts <= 0 or st.total_polls < 2:
        dir_mtime_cap = None
        cap_reason = "cold_start"
    elif st.total_polls > 0 and (st.total_polls % DRIFT_SWEEP_EVERY) == 0:
        dir_mtime_cap = None
        cap_reason = "drift_sweep"
        log.info("freshness: %s drift sweep (full walk) — poll #%d",
                 scan_root, st.total_polls)
    else:
        dir_mtime_cap = max(0.0, st.last_check_ts - SAFETY_BUFFER_S)

    # The scanner needs (share_id, scan_root, source, on_progress).
    # For freshness polls we don't surface progress on the scan WebSocket;
    # the scanner will broadcast scan_progress anyway via its on_progress
    # callback — pass None to keep that quiet for the freshness path.
    _reg.inflight[scan_root] = now
    t0 = time.time()
    plan: dict = {}
    try:
        from soniqboom.core.scanner import start_remote_scan, get_progress

        await start_remote_scan(
            share_id="",  # freshness-triggered scans don't need share_id
            scan_root=scan_root,
            source=source,
            on_progress=None,  # quiet — freshness doesn't drive scan badge
            dir_mtime_cap=dir_mtime_cap,
        )
        progress = get_progress()
        plan = dict(progress.last_plan or {})
    except Exception as exc:
        log.warning("freshness: scan of %s failed: %s", scan_root, exc)
    finally:
        latency_ms = (time.time() - t0) * 1000
        _reg.inflight.pop(scan_root, None)
        st.last_check_ts = time.time()
        st.total_polls += 1

    # Telemetry: structured one-liner.
    walked  = int(plan.get("walked", 0))
    extract = int(plan.get("extract", 0))
    skip    = int(plan.get("skip", 0))
    mtime_refresh = int(plan.get("mtime_refresh", 0))
    log.info(
        "freshness: share=%s walked=%d fresh=%d skip=%d mtime_refresh=%d "
        "latency_ms=%.0f cadence_min=%.1f reason=%s cap=%s",
        scan_root, walked, extract, skip, mtime_refresh,
        latency_ms, st.adaptive_interval() / 60.0, reason, cap_reason,
    )

    # Record change for adaptive cadence + fire toast callback.
    if extract > 0:
        st.record_change(extract, time.time())
        _save_state()
        cb = _reg.on_new_tracks
        if cb is not None:
            try:
                # Sample new track titles from the plan (if available).  The
                # scanner doesn't currently expose this; the WS event just
                # carries the count + scan_root, frontend can fetch detail.
                await cb(scan_root, extract, [])
            except Exception:
                log.exception("freshness: on_new_tracks callback raised")

    return plan


# ── Public API ──────────────────────────────────────────────────────────────

async def start(
    *,
    data_dir: Path,
    source_lookup: Callable[[str], object],
    on_new_tracks: Callable[[str, int, list[str]], Awaitable[None]] | None = None,
) -> None:
    """Initialise the freshness system and launch a poll loop per remote share.

    Call once at server startup, after the FileSource registry is loaded.
    """
    if _reg.enabled:
        return
    _reg.enabled = True
    _reg.data_dir = data_dir
    _reg.state_file = data_dir / "freshness_state.json"
    _reg.source_lookup = source_lookup
    _reg.on_new_tracks = on_new_tracks
    _load_state()

    # Bootstrap a poll loop for every share that's already registered
    # in the persistence dirs list.  We re-arm via add_share() when the
    # admin adds new shares later.
    try:
        from soniqboom.core.data import list_scan_dirs as _list_dirs
        dirs = await _list_dirs()
        for d in dirs:
            path = str(d.get("path", ""))
            if path.startswith(("ftp://", "smb://", "webdav://", "webdavs://")):
                await add_share(path)
    except Exception:
        log.exception("freshness: initial share discovery failed")

    log.info("freshness: started (%d share(s) armed)", len(_reg.tasks))


async def stop() -> None:
    """Cancel all per-share loops and flush state."""
    if not _reg.enabled:
        return
    _reg.enabled = False
    tasks = list(_reg.tasks.values())
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _reg.tasks.clear()
    _save_state()
    log.info("freshness: stopped")


async def add_share(scan_root: str) -> None:
    """Arm a poll loop for *scan_root* (idempotent)."""
    if not _reg.enabled:
        return
    if scan_root in _reg.tasks:
        return
    if not scan_root.startswith(("ftp://", "smb://", "webdav://", "webdavs://")):
        return
    _reg.tasks[scan_root] = asyncio.create_task(
        _share_poll_loop(scan_root),
        name=f"freshness.poll[{scan_root}]",
    )


async def remove_share(scan_root: str) -> None:
    """Disarm a poll loop (e.g. admin removed the share)."""
    task = _reg.tasks.pop(scan_root, None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _reg.states.pop(scan_root, None)
    _save_state()


async def check_now(scan_root: str, *, reason: str = "user") -> dict:
    """Fire an immediate freshness scan for *scan_root*.

    Returns the plan dict (walked/extract/skip/...) from the scanner.
    Used by:
      * admin "Check now" button (reason="user")
      * stream-404 hook (reason="stream_404")
      * on-folder-open frontend trigger (reason="folder_open")
      * on-app-focus frontend trigger (reason="app_focus")

    Unlike the background loop, this BYPASSES the pool-pressure gate —
    the user is actively waiting on the result.
    """
    if not scan_root.startswith(("ftp://", "smb://", "webdav://", "webdavs://")):
        return {}
    # Ensure a state object exists even for shares we haven't auto-armed
    # (e.g. share was added since startup).
    _reg.states.setdefault(scan_root, ShareState(scan_root=scan_root))
    return await _poll_share(scan_root, reason=reason)


def get_status() -> list[dict]:
    """Return per-share status for the admin UI.

    Each entry: {scan_root, last_check_ts, next_check_ts,
                 cadence_seconds, total_polls, total_new_tracks,
                 inflight, samples}
    """
    now = time.time()
    out = []
    for scan_root, st in _reg.states.items():
        out.append({
            "scan_root":         scan_root,
            "last_check_ts":     st.last_check_ts,
            "next_check_ts":     st.next_check_ts,
            "cadence_seconds":   round(st.adaptive_interval(), 1),
            "total_polls":       st.total_polls,
            "total_new_tracks":  st.total_new_tracks,
            "inflight":          scan_root in _reg.inflight,
            "samples":           len(st.change_intervals),
            "armed":             scan_root in _reg.tasks,
            "seconds_since_check": (
                round(now - st.last_check_ts, 1) if st.last_check_ts > 0 else None
            ),
            "seconds_until_next":  (
                round(max(0, st.next_check_ts - now), 1)
                if st.next_check_ts > 0 else None
            ),
        })
    out.sort(key=lambda d: d["scan_root"])
    return out


def is_enabled() -> bool:
    return _reg.enabled
