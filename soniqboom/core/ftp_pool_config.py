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
On a clean session for >24 h without tripping, the keepalive loop may
gently retry +1 (handled in :mod:`filesource`, not here).
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
        entry = data.get(key) or {}
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


def set_detected_cap(host: str, port: int, cap: int) -> None:
    """Force-set the cap (admin override).  Used by:
      * Active probe endpoint (POST /api/admin/ftp/probe-cap)
      * Manual operator override from the UI
    """
    key = _server_key(host, port)
    with _LOCK:
        data = _load()
        entry = data.get(key) or {}
        entry["detected_cap"] = int(cap)
        entry["learned_at"]   = int(time.time())
        # Don't reset trip_count — admin override is informational, the
        # operator may still want to see how often we'd have tripped.
        data[key] = entry
        _save(data)
    log.info("FTP server %s: detected cap set to %d (admin override)", key, cap)


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
