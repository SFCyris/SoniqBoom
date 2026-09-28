# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seeded shuffle order over an arbitrary id set — the server side of shuffle play.

The browser only ever holds a small play queue (a few dozen tracks), never the
whole result set, so "shuffle" cannot be a client-side random pick: it would be
random only within that queue.  Instead the server defines, per ``seed``, a
pseudo-random PERMUTATION of every id matching the view's filter, and the client
pages through it exactly like a sorted list (``offset`` / ``limit``).  That gives:

  * uniform coverage of the ENTIRE result set (every track is reachable),
  * no repeats until the whole set has played (it is a permutation),
  * a deterministic "next" — so prefetch / gapless / prewarm work under shuffle,
  * resumability — the client persists ``(seed, offset)``, not thousands of rows.

Ordering is ``sorted(ids, key=SHA1(seed ‖ id))``.  Because
each id's sort key depends only on ``(seed, id)``, the order is STABLE under
library mutation: a scan that adds or removes tracks inserts/removes those
positions without reshuffling everything else, so a listener mid-session does
not suddenly get repeats.  (A linear checksum such as CRC32 must NOT be used
here: for equal-length ids two seeds differ by a constant XOR, which makes
"reshuffle" produce strongly correlated orders.)

Cost at ~263 K ids: ~150 ms of pure-Python hash+sort once per (seed, filter),
then O(limit) per page from the LRU below (~2 MB per full-library order).  The
work holds the GIL, so running it in a worker thread does not make it free — it
only interleaves it with the event loop (``main.py`` shortens the GIL switch
interval for that reason).  Big orders are sorted in 256 digest buckets
(``_bucketed_order``) so no single uninterruptible C call is long.  Concurrent cold requests for the SAME order are
single-flighted, and sorts for different orders run one at a time (``_sort_lock``).

Cache keys deliberately carry NO store version: a seed is drawn per shuffle
session, so an entry only ever serves the later pages of the session that
created it, and the order is mutation-stable — ids deleted meanwhile are skipped
by the caller, ids added meanwhile simply wait for the next shuffle.  (Keying on
the store's mutation counter threw every order away on each play-time metadata
backfill and re-paid the full sort for an unchanged id set.)
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Callable, Hashable, Iterable

# Bounded by TOTAL ids held, not just entries: a filtered order (one artist's
# tracker tunes) costs almost nothing, a full-library one ~2 MB of id references,
# and folder + library orders share this cache.  ~6 full 263 K-track orders.
# The ENTRY bound is deliberately loose (the id budget is what limits memory): a
# session's order is only touched every ~50 tracks, and with 32 slots a run of
# small folder shuffles evicted a live library order — the re-deal then drew
# from the current id set and replayed tracks the listener had already heard.
_MAX_CACHED_ORDERS = 256
# Sorts below this many ids skip ``_sort_lock``: they take ≤5 ms, and queueing a
# 50-track folder shuffle behind someone's 150 ms full-library sort cost it 129 ms.
_SORT_LOCK_MIN_IDS = 10_000
_MAX_CACHED_IDS = 1_600_000

_lock = threading.Lock()
_sort_lock = threading.Lock()
_cache: "OrderedDict[Hashable, list[str]]" = OrderedDict()
_cached_ids = 0
_inflight: "dict[Hashable, threading.Event]" = {}
_INFLIGHT_WAIT_S = 30.0
_INFLIGHT_ATTEMPTS = 4


def _store(cache_key: Hashable, order: list[str]) -> None:
    """Insert/replace under ``_lock`` and evict LRU entries past either bound
    (the entry just stored always survives, even if it alone exceeds the id
    budget)."""
    global _cached_ids
    old = _cache.pop(cache_key, None)
    if old is not None:
        _cached_ids -= len(old)
    _cache[cache_key] = order
    _cached_ids += len(order)
    while len(_cache) > 1 and (len(_cache) > _MAX_CACHED_ORDERS
                               or _cached_ids > _MAX_CACHED_IDS):
        _, dropped = _cache.popitem(last=False)
        _cached_ids -= len(dropped)


def normalize_seed(seed) -> int:
    """Clamp any client-supplied seed to a non-negative 63-bit int."""
    try:
        return int(seed) & 0x7FFF_FFFF_FFFF_FFFF
    except (TypeError, ValueError, OverflowError):
        return 0


def seeded_order(ids: Iterable[str], seed: int) -> list[str]:
    """Return ``ids`` as a pseudo-random permutation determined by ``seed``.

    Pure function (no store access) so it can run off the event loop on a
    snapshot list.  The sort key is the FULL SHA-1 of ``seed ‖ id`` (160 bits —
    no tie-break needed, the order is total and deterministic), which measured
    40 % faster than a keyed 64-bit BLAKE2b plus an ``(digest, id)`` tuple
    (153 ms vs 256 ms for 263 K ids).  Not a security use of SHA-1.
    """
    prefix = normalize_seed(seed).to_bytes(8, "big")
    sha1 = hashlib.sha1

    def _k(tid: str) -> bytes:
        return sha1(prefix + tid.encode("utf-8", "surrogatepass"),
                    usedforsecurity=False).digest()

    ids = ids if isinstance(ids, (list, tuple)) else list(ids)
    if len(ids) < _SORT_LOCK_MIN_IDS:
        return sorted(ids, key=_k)
    # One BIG sort at a time: two side by side finish no sooner in total (they
    # share the GIL) and only lengthen the stretch the event loop is contended.
    with _sort_lock:
        return _bucketed_order(ids, prefix)


def _bucketed_order(ids: list[str] | tuple[str, ...], prefix: bytes) -> list[str]:
    """Same order as ``sorted(ids, key=sha1(prefix ‖ id))``, without one long
    GIL hold.  A single ``sorted()`` over 263 K keys is ONE C call (~60 ms) the
    interpreter can't interrupt, so every listener stalled for its length.
    Split by the digest's first byte into 256 buckets (already in order
    relative to each other) and sort each (~1 K plain 20-byte digests): the
    GIL switches between the pieces (longest stall of another thread measured
    57 ms → ~4 ms at 263 K ids).  A digest → id map hands back the ORIGINAL
    id strings — the cached order must reference the store's strings, not
    hold decoded copies (~10× the memory).  Equal digests mean equal ids."""
    sha1 = hashlib.sha1
    buckets: list[list[bytes]] = [[] for _ in range(256)]
    by_digest: dict[bytes, str] = {}
    for tid in ids:
        d = sha1(prefix + tid.encode("utf-8", "surrogatepass"),
                 usedforsecurity=False).digest()
        buckets[d[0]].append(d)
        by_digest[d] = tid
    out: list[str] = []
    get = by_digest.__getitem__
    for b in buckets:
        b.sort()
        out.extend(map(get, b))
    return out


def peek(cache_key: Hashable) -> list[str] | None:
    """Return the cached order for ``cache_key`` (refreshing its LRU slot), or
    ``None`` on a miss — lets a caller skip building the id snapshot on a hit."""
    with _lock:
        hit = _cache.get(cache_key)
        if hit is not None:
            _cache.move_to_end(cache_key)
        return hit


def cached_order(cache_key: Hashable, ids_factory: Callable[[], Iterable[str]],
                 seed: int, *, refresh: bool = False) -> list[str]:
    """LRU-memoised :func:`seeded_order`.

    ``cache_key`` must capture everything that defines the candidate set (filter,
    dedup mode, …) plus the seed; ``ids_factory`` is only invoked on a miss.
    ``refresh=True`` recomputes from THIS caller's ids and replaces the entry —
    for a caller that can tell the cached order no longer covers its candidates.
    It never settles for another thread's result: it waits for a computation in
    flight and then does its own.

    Blocking (hash + sort, and a wait on a concurrent computation of the same
    key) — call it from a worker thread, not the event loop.
    """
    for _ in range(_INFLIGHT_ATTEMPTS):
        with _lock:
            if not refresh:
                hit = _cache.get(cache_key)
                if hit is not None:
                    _cache.move_to_end(cache_key)
                    return hit
            waiter = _inflight.get(cache_key)
            if waiter is None:
                waiter = _inflight[cache_key] = threading.Event()
                break                               # this thread computes
        # Someone is already sorting this key.  A normal miss takes their result
        # on the next pass; if they failed (or this is a refresh) the next pass
        # makes THIS thread the one that computes — still one at a time.
        waiter.wait(_INFLIGHT_WAIT_S)
    else:
        # Never got a turn (every wait timed out): compute unregistered rather than
        # block further — but keep the result, or every later page pays the sort again.
        order = seeded_order(ids_factory(), seed)
        with _lock:
            _store(cache_key, order)
        return order
    try:
        order = seeded_order(ids_factory(), seed)
        with _lock:
            _store(cache_key, order)
        return order
    finally:
        with _lock:
            _inflight.pop(cache_key, None)
        waiter.set()


def clear() -> None:
    """Drop every cached order (tests / explicit invalidation)."""
    global _cached_ids
    with _lock:
        _cache.clear()
        _cached_ids = 0
