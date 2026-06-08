# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Process-lifetime cache hit/miss counters.

One ``(hits, misses)`` pair per logical cache tier.  Read by
``GET /api/admin/cache-stats`` to drive the cache-cascade visualization
with REAL resolve probabilities instead of synthetic ones.

Design notes (why this is safe and cheap):

* **Plain ``int += 1``, no locks.**  Most increment sites run on the
  asyncio event-loop thread (the fstree browse / scan-root / per-path
  caches and the conversion cache's ``get_or_render`` are all reached
  from async endpoints), so they're single-threaded — no race.  A few
  tiers (art, remote) can fire from worker threads via
  ``asyncio.to_thread``; there a lost increment is theoretically
  possible, but these are telemetry counters feeding an animation, not
  a ledger — a lost count once in millions is invisible at the
  hit-rate precision the cascade renders.  Adding a lock would cost
  more than the data is worth and re-introduce the contention the
  cache layers were optimized to avoid.  ``list[idx] += 1`` and
  ``list.append`` are both effectively atomic enough under CPython's
  GIL for this purpose.

* **Reset on restart, never persisted.**  Cache hit-rate is a property
  of the current process's warm state.  A carried-over 97% rate would
  be a lie until the caches re-warm — the cascade climbing from 0 after
  a restart is the honest, and more interesting, behaviour.

* **~40 lines, no background task, no new threads.**  The rolling-window
  prune happens lazily inside ``snapshot()`` (called once per poll).
"""
from __future__ import annotations

import time
from collections import deque

# The seven logical cache tiers, in cascade order (top = cheapest/hottest).
TIERS = ("browse", "scan_root", "per_path", "conversion", "art", "remote", "zip")

# tier -> [hits, misses].  Mutable lists so increments mutate in place
# without rebinding a module global from a worker thread.
_C: dict[str, list[int]] = {t: [0, 0] for t in TIERS}

# Rolling window of recent (timestamp, tier, is_hit) so the endpoint can
# report a per-second event rate — without it the cascade looks frozen on
# a long-uptime server (a static 97.9% with no motion).  Pruned by timestamp
# lazily in ``snapshot()``, but ALSO hard-capped by ``deque(maxlen=...)`` so
# memory stays bounded even if the cascade is never opened (no poll ⇒ no
# timestamp prune).  The cap holds many ``_WINDOW_SEC`` windows' worth of
# events at a high cache rate, so it never clips the rate readout in practice.
_WINDOW_SEC = 10.0
_RECENT_MAXLEN = 20000
_recent: "deque[tuple[float, str, bool]]" = deque(maxlen=_RECENT_MAXLEN)
_START = time.monotonic()

# Optional live entry-count providers per tier (filled in by register_size).
# Lets the cascade render each tier-row's "fullness".  Tiers without a cheap
# count provider report ``size: null``.
_SIZE_FN: dict[str, "callable"] = {}


def hit(tier: str) -> None:
    """Record a cache hit for ``tier`` (silently ignores unknown tiers)."""
    c = _C.get(tier)
    if c is None:
        return
    c[0] += 1
    _recent.append((time.monotonic(), tier, True))


def miss(tier: str) -> None:
    """Record a cache miss for ``tier`` (silently ignores unknown tiers)."""
    c = _C.get(tier)
    if c is None:
        return
    c[1] += 1
    _recent.append((time.monotonic(), tier, False))


def register_size(tier: str, fn) -> None:
    """Register a zero-arg callable returning the live entry count for ``tier``.

    Called once at startup by each cache module that can cheaply report its
    in-memory entry count.  ``fn`` must be cheap (a ``len()``) — it's invoked
    on every ``snapshot()``.
    """
    if tier in _C:
        _SIZE_FN[tier] = fn


def reset() -> None:
    """Zero all counters (used by tests / a manual admin reset)."""
    for c in _C.values():
        c[0] = 0
        c[1] = 0
    _recent.clear()


def snapshot() -> dict:
    """Return the current per-tier stats for the cache-stats endpoint."""
    now = time.monotonic()
    cutoff = now - _WINDOW_SEC
    # Prune expired window entries from the left (deque popleft is O(1)).
    while _recent and _recent[0][0] < cutoff:
        _recent.popleft()
    rate: dict[str, int] = {}
    for _ts, tier, _is_hit in _recent:
        rate[tier] = rate.get(tier, 0) + 1

    tiers: dict[str, dict] = {}
    for t, (h, m) in _C.items():
        tot = h + m
        size_fn = _SIZE_FN.get(t)
        try:
            size = size_fn() if size_fn else None
        except Exception:
            size = None
        tiers[t] = {
            "hits": h,
            "misses": m,
            "hit_rate": round(h / tot, 4) if tot else 0.0,
            "rate_1s": round(rate.get(t, 0) / _WINDOW_SEC, 2),
            "size": size,
        }
    return {"uptime_sec": round(now - _START, 1), "tiers": tiers}
