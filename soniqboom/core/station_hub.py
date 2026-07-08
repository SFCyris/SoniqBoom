# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""station_hub.py — shared live-audio fan-out for internet-radio relays.

Today each ``GET /stations/relay/{sid}`` opens its OWN upstream connection
(httpx) or ffmpeg process, so N listeners of the same station = N upstream
pulls + N transcodes.  This module collapses that to **one** upstream reader
per ``(sid, v)`` whose clean, metadata-stripped audio is fanned out to every
subscriber — browser ``<audio>`` and cast/room targets alike, since both are
just "pull an HTTP audio URL" consumers.

Design
------
* ``StationHub`` is **producer-agnostic**: it's handed an async-generator
  factory that yields clean audio bytes (the direct-ICY or HLS-ffmpeg
  producer lives in ``stations.py``; the hub never speaks HTTP or ffmpeg).
  This keeps the hub trivially unit-testable with a fake producer and avoids
  an import cycle with ``stations.py``.
* One ``_reader`` task drains the producer and distributes each chunk to a set
  of **bounded** per-subscriber queues.  The reader NEVER awaits a subscriber
  (all ``put_nowait``) — a slow client can't stall the shared reader or the
  other listeners.  A subscriber whose queue overflows is dropped (Icecast's
  "fell too far behind → disconnect" policy) rather than corrupting its stream
  with gaps.
* **Burst-on-connect**: a new subscriber is handed the last ~2 s of buffered
  audio so its jitter buffer fills immediately instead of waiting a chunk.
  Safe for self-framing codecs (MP3/ADTS-AAC resync to the next frame header);
  the caller opts a format out of bursting when it isn't self-framing.
* **Server-side reconnect** lives in the producer (a station blip reconnects
  under the same hub, keeping every subscriber attached) — strictly better
  than today's per-client re-request storm.
* **Lifecycle**: the first subscriber creates + starts the hub; the last one
  to leave schedules teardown after a grace window (so switching stations or a
  quick reconnect reuses the warm upstream).  Concurrent first-subscribers for
  the same key share ONE creation via a single-flight future (the same idiom
  as the ``/patterns`` and transcode caches).

Concurrency model
-----------------
asyncio is single-threaded, so a critical section that does not ``await`` is
already atomic.  We rely on that: ``subscribe`` / ``unsubscribe`` and the
reader's per-chunk distribution mutate shared state WITHOUT awaiting, so they
never interleave.  The only ``await``-bearing critical region is hub creation
(it connects upstream), which is serialised by the registry's single-flight
future, NOT by holding a lock across the slow connect.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import AsyncIterator, Awaitable, Callable

log = logging.getLogger("soniqboom.station_hub")

# Sentinel pushed to a subscriber queue to end its drain loop (EOF, drop, or
# hub teardown).  A distinct object so it can never collide with an audio chunk.
_SENTINEL: object = object()

# Per-subscriber queue bound.  A chunk is ~8 KB; at 128 kbps (~16 KB/s) that is
# ~0.5 s of audio per chunk, so 128 chunks ≈ 1 MB ≈ 64 s of slack before we
# decide a client is hopelessly behind and drop it.  Generous enough that a
# normal jittery mobile client is never dropped; small enough to bound memory.
_QUEUE_MAX = 128

# Burst-on-connect: hand a new subscriber a chunk of already-buffered audio so
# its decoder starts with headroom instead of racing real-time delivery from
# byte zero.  This is the single most effective anti-stutter lever (it's exactly
# what Icecast's ``burst-on-connect`` does).  Bounded by BYTES, not chunk count,
# because the de-interleaver yields variable-size pieces: a fixed count could be
# a fraction of a second at a high bitrate.  ~192 KB ≈ 5 s at 320 kbps / ~12 s
# at 128 kbps — enough to ride out relay/network jitter, while the listener sits
# only a few seconds behind the live edge (irrelevant for music radio).
_BURST_BYTES = 192 * 1024

# Grace window (seconds) after the last subscriber leaves before the upstream is
# torn down.  Covers a station switch that immediately re-subscribes, and a
# browser reconnect after a transient network blip, without holding a dead
# upstream open indefinitely.
_TEARDOWN_GRACE = 10.0

# How long ``start()`` waits for the producer's FIRST chunk before declaring the
# station dead (mirrors the old per-relay peek: a dead master must surface as a
# clean error to the first listener, not a 200 with an empty body).
_FIRST_CHUNK_TIMEOUT = 16.0


class StationHub:
    """One upstream reader fanned out to N subscriber queues.

    Not created directly — go through ``registry.get_or_create`` so keying,
    single-flight creation, and teardown-cancellation are handled.
    """

    def __init__(
        self,
        key: str,
        producer_factory: Callable[..., AsyncIterator[bytes]],
        *,
        media_type: str,
        headers: dict[str, str] | None = None,
        burst: bool = True,
        on_teardown: "Callable[[StationHub], None] | None" = None,
    ) -> None:
        self.key = key
        self.media_type = media_type
        self.headers = dict(headers or {})
        self._burst = burst
        self._producer_factory = producer_factory
        self._on_teardown = on_teardown

        self._subs: set[asyncio.Queue] = set()
        self._ring: deque[bytes] = deque()      # burst-on-connect buffer (byte-bounded)
        self._ring_bytes: int = 0
        self._reader_task: asyncio.Task | None = None
        self._teardown_handle: asyncio.TimerHandle | None = None
        self._first_chunk: asyncio.Future | None = None
        self.alive = False           # True between a successful start() and teardown
        self._closed = False         # terminal: producer exhausted / torn down

    def set_stream_info(self, media_type: str | None = None,
                        headers: dict[str, str] | None = None) -> None:
        """Producer callback to finalise the response content-type/headers once
        the upstream is connected — for the DIRECT path the media_type is only
        known after reading the upstream content-type.  Called before the
        producer's first yield, so it's set by the time ``start()`` returns and
        the first subscriber's ``StreamingResponse`` reads it."""
        if media_type:
            self.media_type = media_type
        if headers:
            self.headers.update(headers)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the reader and block until the first audio chunk arrives (or
        the producer fails / times out).  Raises on failure so the creating
        subscriber can surface a clean 502/504 and the hub is NOT registered."""
        loop = asyncio.get_running_loop()
        self._first_chunk = loop.create_future()
        self._reader_task = loop.create_task(self._reader(), name=f"hub:{self.key}")
        try:
            await asyncio.wait_for(
                asyncio.shield(self._first_chunk), timeout=_FIRST_CHUNK_TIMEOUT)
        except BaseException:
            # Dead/slow station, the producer raised before its first chunk, OR
            # the creating client disconnected mid-connect (CancelledError).
            # BaseException (not Exception) so a cancel still reaps the reader.
            await self._hard_close()
            raise
        # Gate the transition on _closed: a producer that yields exactly one
        # chunk then EOFs can let the reader's finally (→ _hard_close) run DURING
        # the await above, flipping _closed True.  Blindly setting alive=True
        # here would resurrect a corpse (alive=True, _closed=True) that
        # get_or_create could hand to a racing subscriber whose stream() then
        # hangs forever on an empty queue.  (Concurrency audit 2026-07-04.)
        if not self._closed:
            self.alive = True

    def _cancel_teardown(self) -> None:
        if self._teardown_handle is not None:
            self._teardown_handle.cancel()
            self._teardown_handle = None

    def _schedule_teardown(self) -> None:
        """Arm the grace timer once the last subscriber has left."""
        self._cancel_teardown()
        loop = asyncio.get_running_loop()
        self._teardown_handle = loop.call_later(
            _TEARDOWN_GRACE, lambda: loop.create_task(self._grace_expired()))

    async def _grace_expired(self) -> None:
        # Re-check under no-await atomicity: a subscriber may have arrived during
        # the grace window, in which case we must NOT tear the hub down.
        self._teardown_handle = None
        if self._subs:
            return
        log.info("Hub %s idle past grace — tearing down", self.key)
        await self._hard_close()

    async def _hard_close(self) -> None:
        """Cancel the reader, end all subscribers, and deregister.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.alive = False
        self._cancel_teardown()
        task = self._reader_task
        self._reader_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Unblock any still-attached drain loops.
        for q in list(self._subs):
            self._end_subscriber(q)
        self._subs.clear()
        if self._on_teardown is not None:
            try:
                self._on_teardown(self)     # pass self so dereg can identity-check
            except Exception:            # noqa: BLE001 — dereg is best-effort
                pass

    # ── Reader / fan-out ─────────────────────────────────────────────────────

    async def _reader(self) -> None:
        """Drain the producer and distribute clean audio to all subscribers.

        The ``await`` is ONLY on the producer (upstream); distribution is a
        synchronous, non-awaiting critical section, so it never interleaves
        with subscribe/unsubscribe.
        """
        try:
            # The producer is handed our set_stream_info so the DIRECT path can
            # finalise media_type/headers once the upstream content-type is known
            # (before the first chunk, hence before start() returns).
            async for chunk in self._producer_factory(self.set_stream_info):
                if not chunk:
                    continue
                # Signal successful start on the very first chunk.
                if self._first_chunk is not None and not self._first_chunk.done():
                    self._first_chunk.set_result(True)
                self._distribute(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:             # noqa: BLE001 — producer gave up
            log.info("Hub %s producer ended: %s", self.key, exc)
            if self._first_chunk is not None and not self._first_chunk.done():
                self._first_chunk.set_exception(exc)
        finally:
            # Producer exhausted (EOF after all reconnect attempts) or errored →
            # end every subscriber and mark the hub dead so a re-request rebuilds
            # it rather than attaching to a corpse.
            if self._first_chunk is not None and not self._first_chunk.done():
                self._first_chunk.set_exception(
                    RuntimeError("station produced no audio"))
            # Schedule a close on the loop (we're inside the task being closed).
            self.alive = False
            loop = asyncio.get_running_loop()
            loop.call_soon(lambda: loop.create_task(self._hard_close()))

    def _distribute(self, chunk: bytes) -> None:
        """Push one chunk to every subscriber (no await → atomic).  A subscriber
        whose queue is full has fallen too far behind — drop it cleanly."""
        if self._burst:
            self._ring.append(chunk)
            self._ring_bytes += len(chunk)
            while self._ring_bytes > _BURST_BYTES and len(self._ring) > 1:
                self._ring_bytes -= len(self._ring.popleft())
        dropped: list[asyncio.Queue] = []
        for q in self._subs:
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                dropped.append(q)
        for q in dropped:
            self._subs.discard(q)
            self._end_subscriber(q)
            log.debug("Hub %s dropped a slow subscriber (queue full)", self.key)

    @staticmethod
    def _end_subscriber(q: asyncio.Queue) -> None:
        """Make a subscriber's drain loop terminate: free a slot if needed, then
        inject the sentinel so a ``get()`` currently blocked (or the next one)
        returns and the ``StreamingResponse`` closes."""
        try:
            q.put_nowait(_SENTINEL)
        except asyncio.QueueFull:
            try:
                q.get_nowait()               # drop one buffered chunk
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(_SENTINEL)
            except asyncio.QueueFull:        # pragma: no cover — freed a slot above
                pass

    # ── Subscription ─────────────────────────────────────────────────────────

    def subscribe(self) -> tuple[asyncio.Queue, list[bytes]]:
        """Register a subscriber.  Returns its queue and the burst snapshot
        (recent audio to emit before draining the queue).  No await → the
        snapshot + registration are atomic w.r.t. the reader's distribution, so
        the burst and the queued chunks neither overlap nor gap."""
        self._cancel_teardown()
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        if self._closed:
            # Raced a teardown — hand back an immediately-ending subscriber
            # (pre-loaded sentinel) rather than one that blocks forever on get().
            # The definitive guard against the corpse-attach hang: even if every
            # timing race lines up, a subscriber gets a clean empty stream (the
            # frontend then re-requests, rebuilding the hub) instead of a 200
            # that never emits a byte.  (Concurrency audit 2026-07-04.)
            q.put_nowait(_SENTINEL)
            return q, []
        burst = list(self._ring) if self._burst else []
        self._subs.add(q)
        return q, burst

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
        if not self._subs and not self._closed:
            self._schedule_teardown()

    async def stream(self) -> AsyncIterator[bytes]:
        """Async-generator a ``StreamingResponse`` drains: burst, then the live
        queue until the hub ends this subscriber."""
        q, burst = self.subscribe()
        try:
            for chunk in burst:
                yield chunk
            while True:
                item = await q.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            self.unsubscribe(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


class _HubRegistry:
    """Keyed registry of live hubs with single-flight creation."""

    def __init__(self) -> None:
        self._hubs: dict[str, StationHub] = {}
        self._creating: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        key: str,
        build: Callable[[], Awaitable[StationHub]],
    ) -> StationHub:
        """Return the live hub for ``key``, creating it via ``build`` if needed.

        ``build`` is an async factory that constructs the hub AND awaits its
        ``start()`` (so a dead station raises here).  Concurrent callers for the
        same key await one creation; the slow ``start()`` connect is NOT done
        under the registry lock, so other stations aren't blocked.
        """
        async with self._lock:
            hub = self._hubs.get(key)
            # Reader-liveness is part of the predicate: alive/_closed can lag the
            # actual reader termination by a call_soon hop, so also require a
            # live reader task — this rejects a hub whose reader has already
            # ended (or is mid-teardown) and forces a clean rebuild instead of an
            # attach-to-corpse.  (Concurrency audit 2026-07-04.)
            if (hub is not None and hub.alive and not hub._closed
                    and hub._reader_task is not None
                    and not hub._reader_task.done()):
                hub._cancel_teardown()
                return hub
            fut = self._creating.get(key)
            creator = fut is None
            if creator:
                fut = asyncio.get_running_loop().create_future()
                # Swallow the exception on GC so a dead-station build with no
                # concurrent single-flight waiters doesn't log asyncio's noisy
                # "Future exception was never retrieved".
                fut.add_done_callback(
                    lambda f: None if f.cancelled() else f.exception())
                self._creating[key] = fut
        if not creator:
            return await fut                 # share the in-flight creation
        try:
            hub = await build()
            async with self._lock:
                self._hubs[key] = hub
                self._creating.pop(key, None)
            fut.set_result(hub)
            return hub
        except Exception as exc:
            async with self._lock:
                self._creating.pop(key, None)
            if not fut.done():
                fut.set_exception(exc)
            raise

    def _deregister(self, hub: "StationHub") -> None:
        """Teardown callback — drop the hub so the next play rebuilds it, but
        ONLY if it's still the registered one.  A grace-teardown can overlap a
        rebuild: during _hard_close's ``await task``, _closed is already True, so
        get_or_create sees it, rebuilds, and registers a LIVE replacement under
        the same key — an unconditional pop-by-key would then evict that live
        hub, defeating the fan-out and spawning a third upstream.  Identity-check
        so only the torn-down hub removes itself.  (Concurrency audit 2026-07-04.)"""
        if self._hubs.get(hub.key) is hub:
            self._hubs.pop(hub.key, None)

    def stats(self) -> dict:
        """Introspection for tests / an admin endpoint: live hubs + sub counts."""
        return {
            k: {"subscribers": h.subscriber_count, "alive": h.alive}
            for k, h in self._hubs.items()
        }


# Process-wide singleton (mirrors the other module-global registries in core/).
registry = _HubRegistry()
