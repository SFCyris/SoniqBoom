# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-side jukebox — backs the Subsonic ``jukeboxControl`` API.

A single, server-owned playback queue.  Subsonic clients (Symfonium, DSub,
Amperfy) drive it with ``jukeboxControl`` actions; this module is the pure
state machine behind it — queue + current index + playing flag + gain +
playback position.

Output model: SoniqBoom plays audio in a browser, not on the server host, so
this jukebox is the authoritative **control plane** (queue, current index, gain,
position) and its audio is realised through the **multiroom bridge**: every
jukeboxControl mutation is pushed to a reserved "Jukebox" multiroom room
(``api/multiroom.notify_jukebox_room``), so any SoniqBoom browser joined to that
room plays what the jukebox is driving, using the same sync path a human master
uses.  Without a joined browser the jukebox is still a correct shared queue — it
just has no ears.  ``version()`` bumps on every mutation.  State is process-local
and single-threaded (event-loop access); the lock is belt-and-braces.
"""
from __future__ import annotations

import random
import threading
import time


class _Jukebox:
    def __init__(self) -> None:
        self._q: list[str] = []        # ordered track ids
        self._index: int = 0           # current position in _q
        self._playing: bool = False
        self._gain: float = 1.0        # 0.0 .. 1.0
        self._offset: float = 0.0      # seconds into the current track at last (re)start
        self._anchor: float = 0.0      # monotonic seconds when playback last started
        self._version: int = 0         # bumps on every mutation (sink change-detection)
        self._lock = threading.Lock()

    # ── internal (must hold _lock) ──────────────────────────────────────────
    def _position_locked(self) -> float:
        if self._playing and self._q:
            return self._offset + max(0.0, time.monotonic() - self._anchor)
        return self._offset

    def _clamp_index_locked(self) -> None:
        self._index = 0 if not self._q else max(0, min(self._index, len(self._q) - 1))

    def _touch_locked(self) -> None:
        self._version += 1

    # ── reads ───────────────────────────────────────────────────────────────
    def version(self) -> int:
        with self._lock:
            return self._version

    def status(self) -> dict:
        with self._lock:
            has = bool(self._q)
            return {
                "currentIndex": self._index if has else -1,
                "playing": self._playing and has,
                "gain": round(self._gain, 3),
                "position": int(self._position_locked()),
            }

    def position(self) -> float:
        """Current position in seconds as a FLOAT — status() floors to int for
        the Subsonic response, but the multiroom bridge needs sub-second
        precision so play_at/seek don't accumulate ~1s of drift per re-anchor."""
        with self._lock:
            return self._position_locked()

    def queue_ids(self) -> list[str]:
        with self._lock:
            return list(self._q)

    def current_id(self) -> str | None:
        with self._lock:
            return self._q[self._index] if self._q else None

    # ── mutations (map 1:1 to jukeboxControl actions) ───────────────────────
    def set_queue(self, ids: list[str]) -> None:
        with self._lock:
            self._q = [i for i in (ids or []) if i]
            self._index = 0
            self._offset = 0.0
            self._anchor = time.monotonic()
            self._playing = False       # spec: `set` loads; client sends `start`
            self._touch_locked()

    def add(self, ids: list[str]) -> None:
        with self._lock:
            self._q.extend(i for i in (ids or []) if i)
            self._touch_locked()

    def clear(self) -> None:
        with self._lock:
            self._q = []
            self._index = 0
            self._offset = 0.0
            self._playing = False
            self._touch_locked()

    def remove(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self._q):
                removed_current = index == self._index
                self._q.pop(index)
                if index < self._index:
                    self._index -= 1
                # If _index now points past the end, the removed current track
                # was the tail — nothing slides into its slot.
                removed_tail = removed_current and self._index >= len(self._q)
                self._clamp_index_locked()
                if removed_current:
                    # The next track slid into the current slot — start it at
                    # zero, don't inherit the removed track's elapsed position.
                    self._offset = 0.0
                    self._anchor = time.monotonic()
                    if removed_tail:
                        # No next track — stop rather than rewind and restart
                        # the now-previous track.
                        self._playing = False
                self._touch_locked()

    def shuffle(self) -> None:
        with self._lock:
            if not self._q:
                return
            cur = self._q[self._index]
            random.shuffle(self._q)
            # Keep the currently-selected track at the head so playback doesn't
            # jump mid-song.
            try:
                self._q.remove(cur)
                self._q.insert(0, cur)
            except ValueError:
                pass
            self._index = 0
            self._touch_locked()

    def skip(self, index: int, offset: float = 0.0) -> None:
        with self._lock:
            self._index = int(index)
            self._clamp_index_locked()
            self._offset = max(0.0, float(offset))
            self._anchor = time.monotonic()
            self._touch_locked()

    def start(self) -> None:
        with self._lock:
            if self._q and not self._playing:
                self._anchor = time.monotonic()
                self._playing = True
                self._touch_locked()

    def stop(self) -> None:
        with self._lock:
            if self._playing:
                self._offset = self._position_locked()   # freeze position
                self._playing = False
                self._touch_locked()

    def set_gain(self, gain: float) -> None:
        with self._lock:
            self._gain = max(0.0, min(1.0, float(gain)))
            self._touch_locked()


_JUKEBOX = _Jukebox()


def get_jukebox() -> _Jukebox:
    """The process-global jukebox singleton."""
    return _JUKEBOX
