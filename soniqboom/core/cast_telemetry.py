# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-cast event log + first-byte latency tracking.

Three reasons to instrument from day one rather than retro-fitting:

1. Snappy-first-byte is the headline UX promise.  We can't claim it
   without numbers.  A p95 dashboard surfaces firmware regressions on
   specific renderers before user reports come in.

2. Cast workflows are intrinsically cross-host (server + renderer +
   network), so unit tests can't capture the real failure modes.
   Telemetry is the only honest feedback loop short of a hardware lab.

3. Bounded-cost: ring buffer in memory (no SQLite I/O on the hot
   path), opt-in disk persistence, opt-out entirely via config.

Privacy: we log ``(timestamp, target_id, protocol, codec, ms,
bytes, outcome, error_class)`` only — no track titles, no user IDs,
no IP addresses.  The user can ``DELETE /api/cast/telemetry`` at any
time to wipe the buffer.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, asdict
from threading import Lock
from typing import Iterator, Literal

log = logging.getLogger(__name__)


# Ring-buffer size — 10 K events × ~120 B ≈ 1.2 MB.  At a realistic
# 100 plays/day that's 100 days of history before rollover.
_BUFFER_MAX = 10_000


@dataclass(frozen=True)
class CastEvent:
    ts:               float
    protocol:         Literal["dlna", "cast", "airplay", "http"]
    target_id:        str
    source_codec:     str
    target_codec:     str
    first_byte_ms:    int | None      # None if the request errored before any bytes
    # Audio-2 P1: split first-byte into two phases so the dashboard can
    # distinguish "sidplayfp/MIDI/UADE slow" from "ffmpeg slow".  Both
    # are None for the native fast path (no rendering or transcoding).
    render_phase_ms:    int | None = None  # rendered-format prep (SID/MIDI/tracker/GME)
    transcode_phase_ms: int | None = None  # ffmpeg transcode start → first stdout byte
    total_bytes:      int = 0
    duration_ms:      int = 0         # full request lifetime
    outcome:          Literal["played", "skipped", "errored", "cancelled"] = "errored"
    error_class:      str | None = None  # e.g. "ConnectionError" / "FFmpegFailure"

    def to_public(self) -> dict:
        return asdict(self)


_lock = Lock()
_events: "deque[CastEvent]" = deque(maxlen=_BUFFER_MAX)
_enabled = True


def record(event: CastEvent) -> None:
    """Append ``event`` to the ring buffer.  Cheap — single lock acquire,
    no I/O.  Safe to call from a hot path."""
    if not _enabled:
        return
    with _lock:
        _events.append(event)


def all_events() -> list[CastEvent]:
    with _lock:
        return list(_events)


def iter_recent(limit: int = 100) -> Iterator[CastEvent]:
    with _lock:
        snapshot = list(_events)
    for ev in snapshot[-limit:]:
        yield ev


# ── Aggregations for the dashboard ────────────────────────────────────────

def p95_first_byte_ms() -> dict[str, int]:
    """Compute p95 first-byte latency per (protocol, target_codec)
    bucket.  Buckets with <5 samples are omitted — the percentile is
    noise below that.
    """
    buckets: dict[str, list[int]] = {}
    with _lock:
        snapshot = list(_events)
    for ev in snapshot:
        if ev.first_byte_ms is None or ev.outcome == "errored":
            continue
        key = f"{ev.protocol}:{ev.target_codec}"
        buckets.setdefault(key, []).append(ev.first_byte_ms)
    out: dict[str, int] = {}
    for key, samples in buckets.items():
        if len(samples) < 5:
            continue
        samples.sort()
        idx = max(0, int(round(0.95 * (len(samples) - 1))))
        out[key] = samples[idx]
    return out


def outcome_counts(window_seconds: int = 3600) -> dict[str, int]:
    """Tally outcomes (played / skipped / errored / cancelled) in a
    rolling window.  Used by the dashboard to flag elevated error
    rates on a specific renderer."""
    cutoff = time.time() - window_seconds
    counts: dict[str, int] = {
        "played": 0, "skipped": 0, "errored": 0, "cancelled": 0,
    }
    # Snapshot under the lock then iterate — without the snapshot,
    # a concurrent record() append could raise RuntimeError on the
    # deque iteration.
    with _lock:
        snapshot = list(_events)
    for ev in snapshot:
        if ev.ts < cutoff:
            continue
        counts[ev.outcome] = counts.get(ev.outcome, 0) + 1
    return counts


def clear() -> None:
    """Wipe the ring buffer.  Backs the user-visible 'reset' button."""
    with _lock:
        _events.clear()


def set_enabled(enabled: bool) -> None:
    global _enabled
    _enabled = bool(enabled)


# ── Helper context manager for the byte-server ────────────────────────────

class CastTimer:
    """Lightweight wrapper used by the byte-server to time first-byte
    + total transfer.  Pattern:

        with CastTimer(protocol="dlna", target_id=tid, source="flac",
                       target="mp3") as t:
            ... stream bytes ...
            t.mark_first_byte()
            ... more bytes ...
            t.set_bytes(total)
            t.outcome = "played"

    On ``__exit__`` the event lands in the ring buffer with whatever
    outcome was set (defaults to "errored" if the block raised).
    """

    __slots__ = (
        "_started",
        "_render_done_at",
        "_transcode_started_at",
        "_first_byte_at",
        "protocol", "target_id", "source", "target",
        "bytes_sent", "outcome", "error_class",
    )

    def __init__(self, *, protocol, target_id, source, target):
        self.protocol     = protocol
        self.target_id    = target_id or "-"
        self.source       = source or "?"
        self.target       = target or "?"
        self.bytes_sent   = 0
        self.outcome      = "errored"   # pessimistic default
        self.error_class  = None
        # Monotonic clock for intervals — wall-clock (time.time()) is
        # NTP-adjustable and can step backwards, producing negative
        # first_byte_ms metrics.  time.time() is still used for the
        # absolute event timestamp below.
        self._started              = time.monotonic()
        self._render_done_at:      float | None = None
        self._transcode_started_at: float | None = None
        self._first_byte_at:       float | None = None

    def mark_render_done(self) -> None:
        """Called when the rendered-format source (SID/MIDI/tracker/GME)
        has been produced and ffmpeg is about to be spawned.  No-op for
        native ffmpeg sources."""
        if self._render_done_at is None:
            self._render_done_at = time.monotonic()
            self._transcode_started_at = self._render_done_at

    def mark_transcode_started(self) -> None:
        """Called when ffmpeg is spawned (after any rendered-format
        prep).  Lets us measure the ffmpeg-only phase separately from
        the renderer phase."""
        if self._transcode_started_at is None:
            self._transcode_started_at = time.monotonic()

    def mark_first_byte(self) -> None:
        if self._first_byte_at is None:
            self._first_byte_at = time.monotonic()

    def set_bytes(self, n: int) -> None:
        self.bytes_sent = int(n)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc and self.outcome == "errored":
            self.error_class = exc.__class__.__name__
        end_mono = time.monotonic()
        fb_ms = (
            int(round((self._first_byte_at - self._started) * 1000))
            if self._first_byte_at is not None else None
        )
        # Split phase timings: render = start → render_done; transcode =
        # transcode_started → first_byte.  Either may be None when the
        # corresponding phase wasn't run (e.g. native FLAC path has no
        # render phase; missing first_byte means everything is None).
        render_ms: int | None = None
        if self._render_done_at is not None:
            render_ms = int(round((self._render_done_at - self._started) * 1000))
        transcode_ms: int | None = None
        if (self._first_byte_at is not None
                and self._transcode_started_at is not None):
            transcode_ms = int(round(
                (self._first_byte_at - self._transcode_started_at) * 1000,
            ))
        record(CastEvent(
            ts                 = time.time(),
            protocol           = self.protocol,
            target_id          = self.target_id,
            source_codec       = self.source,
            target_codec       = self.target,
            first_byte_ms      = fb_ms,
            render_phase_ms    = render_ms,
            transcode_phase_ms = transcode_ms,
            total_bytes        = self.bytes_sent,
            duration_ms        = int(round((end_mono - self._started) * 1000)),
            outcome            = self.outcome,
            error_class        = self.error_class,
        ))
        # Don't swallow exceptions
        return False
