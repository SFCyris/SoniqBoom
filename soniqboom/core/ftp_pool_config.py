# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Persistent FTP pool tuning state.

Two things live here:

1. **Detected caps** — what the FTP server's concurrent-client limit
   appears to be, learned reactively from ``421`` / ``530`` "too many
   clients" / "too many users" responses.  Keyed by ``(host, port)`` so
   multiple shares pointing at the same NAS share the discovery
   (otherwise re-learning the same lesson per-credential would mean
   six 530 errors before everything settles down).

2. **Per-share knobs** — the user's preferred scan / stream worker
   counts, set from the UI's share Edit panel.  Stored under each
   share's ``ftp_pool`` sub-object inside ``SoniqBoom.conf``; this
   module is just the read/write convenience layer.

File format (``data_dir/ftp_server_caps.json``):

    {
        "10.0.0.88:21": {
            "detected_cap": 10,
            "learned_at":   1717113600,
            "trip_count":   3
        },
        ...
    }

The cap is "the largest number of concurrent connections we successfully
held".  When we hit a 530, we lower the cap by 1 and bump trip_count.
After ``_CAP_DECAY_INTERVAL_S`` with no new trip the per-pool keepalive loop
(in :mod:`filesource`) calls :func:`relax_detected_cap`, which additively
raises the cap by 1 and resets the clock — an AIMD probe so a TRANSIENT
overload (or a server whose client limit was later raised) recovers on its
own instead of staying pinned low until a manual reset.  The decay never
raises the cap above the user's configured budget (that's what the separate
opt-in auto-grow does); it only ever undoes a reactive clamp.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_FILE_NAME = "ftp_server_caps.json"


def _env_int(name: str, default: int) -> int:
    """int() an env var, falling back to *default* (with a warning) on a
    non-numeric value — a malformed knob must NOT crash module import."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("Invalid %s=%r — using default %d", name, raw, default)
        return default


# A reactively-lowered cap creeps back up by _CAP_DECAY_STEP once this long has
# passed with NO new trip (every trip re-stamps ``learned_at``, so a
# repeatedly-tripping server never relaxes).  Floor of 1 h; 24 h default keeps
# the probe-trip frequency low on a server that genuinely sits at the low cap.
_CAP_DECAY_INTERVAL_S = max(3600, _env_int("SONIQBOOM_FTP_CAP_DECAY_S", 24 * 3600))
_CAP_DECAY_STEP = 1
# REENTRANT ON PURPOSE — do NOT downgrade to threading.Lock().
# record_too_many_clients() / set_detected_cap() / reset_detected_cap()
# acquire this lock and then call _load(), which re-acquires it.  With a
# plain (non-reentrant) Lock the calling thread self-deadlocks at the inner
# acquire and, because the lock is then held forever, every other FTP-pool
# caller AND the asyncio event loop (admin ftp_pool_status → get_all →
# _load) freeze behind it — a whole-server hang.  Surfaced 2026-06 when the
# browse lane tripped the NAS "too many clients" path; root cause was this
# re-entrancy.  An RLock lets the same thread re-enter while still
# serialising different threads.
_LOCK = threading.RLock()
_data_dir: Path | None = None
_cache: dict | None = None  # in-memory copy of the JSON file, lazily loaded


# ── Setup ───────────────────────────────────────────────────────────────────

def init(data_dir: Path) -> None:
    """Set the data directory.  Called once from main.py's lifespan."""
    global _data_dir, _cache
    with _LOCK:
        _data_dir = data_dir
        _cache = None  # invalidate so the next get() reloads


def _path() -> Path | None:
    if _data_dir is None:
        return None
    return _data_dir / _FILE_NAME


# ── Detected-cap persistence ────────────────────────────────────────────────

def _load() -> dict:
    """Return the on-disk map.  Lazy + cached; reloads only after init()."""
    global _cache
    with _LOCK:
        if _cache is not None:
            return _cache
        p = _path()
        if p is None or not p.exists():
            _cache = {}
            return _cache
        try:
            with open(p, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                log.warning("ftp_server_caps.json: expected object, got %s",
                            type(data).__name__)
                data = {}
            _cache = data
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed to read FTP server caps: %s", exc)
            _cache = {}
        return _cache


def _save(data: dict) -> None:
    """Atomically write the on-disk map.  Failures are logged, not raised."""
    p = _path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.new")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError as exc:
        log.warning("Failed to write FTP server caps: %s", exc)


def _server_key(host: str, port: int) -> str:
    """Canonical ``host:port`` key.  Matches the on-disk JSON shape."""
    return f"{host}:{int(port)}"


def get_detected_cap(host: str, port: int) -> int | None:
    """Return the learned cap for ``host:port``, or None if not yet learned.

    None means "no signal yet — use the user-configured budget directly".
    A positive int is the largest concurrent-client count the server
    accepted before throwing 421/530.
    """
    entry = _load().get(_server_key(host, port))
    if isinstance(entry, dict):
        cap = entry.get("detected_cap")
        if isinstance(cap, int) and cap > 0:
            return cap
    return None


def record_too_many_clients(host: str, port: int, observed_in_use: int) -> int:
    """Record that the server rejected us with too-many-clients at
    ``observed_in_use`` concurrent connections.

    The new detected cap is ``observed_in_use - 1`` (we know N didn't
    work, so N-1 is the largest known-good).  Clamped to a floor of 1.
    Idempotent: a second trip at the same level doesn't change the cap.
    Returns the (now-current) detected cap.
    """
    if observed_in_use < 2:
        # Even 1 connection failed — server is unusable or hard-down.
        # Don't pin the cap to 0; let other paths surface the failure.
        return max(1, observed_in_use - 1) or 1
    key = _server_key(host, port)
    new_cap = max(1, observed_in_use - 1)
    with _LOCK:
        data = _load()
        entry = data.get(key)
        entry = entry if isinstance(entry, dict) else {}
        # A manual/probe pin is the operator's assertion of the real limit; a
        # reactive trip must NOT override it.  ``observed_in_use`` is a
        # POOL-LOCAL count, not the server-wide client count, so on a SHARED
        # FTP server another client's saturation trips US at a low local count
        # — exactly the false-positive a manual pin exists to suppress.  Record
        # the trip for visibility (climbing trip_count signals a too-high pin)
        # but leave the pinned cap intact; the operator changes it via reset or
        # a fresh set/probe.
        if entry.get("manual"):
            entry["trip_count"] = int(entry.get("trip_count", 0)) + 1
            data[key] = entry
            _save(data)
            pinned = entry.get("detected_cap")
            log.warning(
                "FTP server %s: too many clients at %d in-flight, but cap is "
                "PINNED at %s (manual) — not lowering; reset to change it",
                key, observed_in_use, pinned,
            )
            return pinned if isinstance(pinned, int) and pinned > 0 else new_cap
        prev = entry.get("detected_cap")
        if isinstance(prev, int) and prev <= new_cap:
            # Already throttled lower or equal — no change.
            new_cap = prev
        entry["detected_cap"] = new_cap
        entry["learned_at"]   = int(time.time())
        entry["trip_count"]   = int(entry.get("trip_count", 0)) + 1
        data[key] = entry
        _save(data)
    log.warning(
        "FTP server %s: too many clients at %d in-flight; "
        "detected cap lowered to %d",
        key, observed_in_use, new_cap,
    )
    return new_cap


def relax_detected_cap(host: str, port: int,
                       config_ceiling: int | None = None) -> int | None:
    """Gently raise a previously-lowered detected cap after a quiet spell.

    A 421/530 lowers the cap via :func:`record_too_many_clients` (which
    re-stamps ``learned_at``).  If that trip was TRANSIENT (temporary server
    overload) or the operator later raised the server's client limit, the cap
    would otherwise stay pinned low forever — only a manual reset/override
    recovers it.  After ``_CAP_DECAY_INTERVAL_S`` with NO new trip
    (``learned_at`` still old) we additively raise the cap by
    ``_CAP_DECAY_STEP`` and reset the clock — an AIMD probe: if the higher
    value trips again, :func:`record_too_many_clients` drops it right back and
    re-stamps the clock, so a server genuinely at the lower limit pays only one
    probe-trip per interval.

    ``config_ceiling`` (the user's configured budget) is REQUIRED to raise: the
    cap is only raised while it still clamps below the budget, and it stops
    (never deletes) once ``detected - 1 >= config_ceiling`` — at that point the
    clamp is redundant, so the learned value is LEFT INTACT (a later budget
    raise re-binds it; a fresh trip re-lowers it), rather than being deleted
    based on the transient budget.  Auto-grow (a separate opt-in) is what
    pushes ABOVE the budget; this only ever undoes a reactive clamp.  A
    ``None`` ceiling is a no-op (we can't tell a binding clamp from a
    non-binding one without the budget, so we refuse to grow unbounded).  An
    admin/probe-pinned cap (``manual``) is authoritative and never auto-decays.

    Returns the (possibly raised) cap; ``None`` if the entry is absent/invalid;
    or the unchanged cap when pinned, non-binding, or not trip-free long enough.
    """
    if config_ceiling is None:
        return get_detected_cap(host, port)
    key = _server_key(host, port)
    now = int(time.time())
    with _LOCK:
        data = _load()
        entry = data.get(key)
        if not isinstance(entry, dict):
            return None
        cap = entry.get("detected_cap")
        if not isinstance(cap, int) or cap <= 0:
            return None
        # An admin/probe-pinned cap is the operator's assertion — never
        # second-guess it with a passive decay (only a real trip supersedes it,
        # which clears the pin in record_too_many_clients).
        if entry.get("manual"):
            return cap
        # The clamp is ``max_size = min(configured, detected - 1)``.  Once
        # ``detected - 1 >= configured`` it no longer limits anything — nothing
        # to recover.  Leave the learned value intact (do NOT delete: the
        # budget may merely have been lowered beneath a still-valid cap, and a
        # future budget raise must re-bind it instead of re-learning via a trip).
        if (cap - 1) >= int(config_ceiling):
            return cap
        learned_raw = entry.get("learned_at")
        # Fail CLOSED on a missing/corrupt timestamp — treat as just-learned so
        # malformed state can't bypass the interval gate and relax immediately.
        learned = int(learned_raw) if isinstance(learned_raw, (int, float)) else now
        if now - learned < _CAP_DECAY_INTERVAL_S:
            return cap  # not trip-free long enough yet
        new_cap = cap + _CAP_DECAY_STEP
        entry["detected_cap"] = new_cap
        entry["learned_at"]   = now       # reset the decay clock (+1 per interval)
        data[key] = entry
        _save(data)
    log.info(
        "FTP server %s: no trip in ~%dh — detected cap raised %d→%d (AIMD probe)",
        key, _CAP_DECAY_INTERVAL_S // 3600, cap, new_cap,
    )
    return new_cap


def set_detected_cap(host: str, port: int, cap: int, *, manual: bool = True) -> None:
    """Force-set the cap.  Used by:
      * Active probe endpoint (POST /api/admin/ftp/probe-cap)
      * Manual operator override from the UI

    ``manual`` (default True) PINS the entry: the passive decay won't creep it
    (relax_detected_cap skips pinned entries) and a reactive pool-local trip
    won't lower it (record_too_many_clients leaves pinned entries intact) — an
    admin override / a probe that FOUND the real limit is the operator's
    assertion and stands until an explicit reset/re-set.  Pass ``manual=False``
    for a value that is only a FLOOR (e.g. a probe that ran its whole range
    WITHOUT a rejection — it proved the server tolerates at least N, not that N
    is the limit) so decay can still creep it toward the configured budget.
    """
    key = _server_key(host, port)
    with _LOCK:
        data = _load()
        entry = data.get(key)
        entry = entry if isinstance(entry, dict) else {}
        entry["detected_cap"] = int(cap)
        entry["learned_at"]   = int(time.time())
        entry["manual"]       = bool(manual)
        # Don't reset trip_count — it's informational; the operator may still
        # want to see how often we'd have tripped.
        data[key] = entry
        _save(data)
    log.info("FTP server %s: detected cap set to %d (%s)", key, cap,
             "pinned" if manual else "floor, decay-eligible")


def reset_detected_cap(host: str, port: int) -> bool:
    """Forget the learned cap for ``host:port``.  Next borrow falls
    back to the user-configured budget without any auto-clamp.

    Returns True if an entry was removed.  Used after a server config
    change ("I bumped MaxClientsPerHost to 30, stop throttling me").
    """
    key = _server_key(host, port)
    with _LOCK:
        data = _load()
        if key not in data:
            return False
        del data[key]
        _save(data)
    log.info("FTP server %s: detected cap reset", key)
    return True


def get_all() -> dict:
    """Return the full ``host:port → {detected_cap, …}`` map.

    Convenience for the admin UI listing.  Caller gets a shallow copy
    so they can sort / annotate freely.
    """
    return dict(_load())
