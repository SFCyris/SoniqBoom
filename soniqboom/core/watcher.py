# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Filesystem watcher — kicks an incremental rescan whenever files change
inside a scan root.

Implementation is a thin shim over the ``watchdog`` library, which
provides native FSEvents (macOS) / inotify (Linux) / kqueue (BSD) /
ReadDirectoryChangesW (Windows) backends.  We collect every change for
a 2-second debounce window and then call :func:`start_scan` on the
affected roots — the existing scanner is smart enough to do an
incremental rescan because the AOF / mtime cache short-circuits files
that haven't changed.

The watcher is opt-in via ``settings.fs_watch`` (default ``True`` for
local roots, never enabled for remote SMB/FTP shares which don't
support push notifications).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False
    Observer = None       # type: ignore[assignment]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]

_DEBOUNCE_SEC = 2.0
_AUDIO_EXTS = {
    ".mp3", ".flac", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".aiff", ".aif",
    ".sid", ".psid", ".mid", ".midi",
    ".mod", ".xm", ".s3m", ".it", ".mptm",
}


# ── Singleton observer state ─────────────────────────────────────────────────

class _State:
    enabled: bool = False
    observer: "Observer | None" = None
    watches: dict[str, object] = {}     # path → watchdog.ObservedWatch
    pending: set[str] = set()           # debounced changed roots
    debounce_task: asyncio.Task | None = None
    loop: asyncio.AbstractEventLoop | None = None


_state = _State()


# ── Event handler ────────────────────────────────────────────────────────────

class _Handler(FileSystemEventHandler):
    """Buffers FS events into the pending set; the debounce task drains it."""

    def __init__(self, root: str) -> None:
        self._root = root

    def _interesting(self, path: str) -> bool:
        # Ignore directory-only events except deletes; we only care about
        # files whose extension is in our supported set.
        if not path:
            return False
        ext = Path(path).suffix.lower()
        return ext in _AUDIO_EXTS

    def on_any_event(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        # ``event.event_type`` ∈ {'created', 'modified', 'deleted', 'moved'}
        if event.is_directory and event.event_type not in ("created", "deleted", "moved"):
            return
        src = getattr(event, "src_path", "") or ""
        if not (event.is_directory or self._interesting(src)):
            return
        _mark_dirty(self._root)


def _mark_dirty(root: str) -> None:
    """Thread-safe enqueue from a watchdog worker thread."""
    loop = _state.loop
    if loop is None or not loop.is_running():
        return
    loop.call_soon_threadsafe(_state.pending.add, root)
    loop.call_soon_threadsafe(_schedule_debounce)


def _schedule_debounce() -> None:
    if _state.debounce_task and not _state.debounce_task.done():
        return
    _state.debounce_task = asyncio.create_task(_debounce_and_scan())


async def _debounce_and_scan() -> None:
    """Wait ``_DEBOUNCE_SEC``, then trigger a rescan of all dirty roots.

    Re-checks the pending set after the sleep so a burst of events
    (e.g. an rsync) results in one rescan, not N."""
    await asyncio.sleep(_DEBOUNCE_SEC)
    roots = list(_state.pending)
    _state.pending.clear()
    if not roots:
        return
    log.info("watcher: triggering rescan for %d root(s): %s", len(roots), roots)
    try:
        from soniqboom.core.scanner import start_scan
        await start_scan(roots)
    except Exception:
        log.exception("watcher: rescan failed")


# ── Public API ───────────────────────────────────────────────────────────────

def is_supported() -> bool:
    """Whether the ``watchdog`` library is importable on this platform."""
    return _HAS_WATCHDOG


async def start(roots: Iterable[str]) -> None:
    """Spin up the observer and arm a watch on every (local) root."""
    if not _HAS_WATCHDOG:
        log.warning("watcher: ``watchdog`` not installed; auto-rescan disabled.")
        return
    if _state.enabled:
        return
    _state.enabled = True
    _state.loop = asyncio.get_running_loop()
    _state.observer = Observer()
    for root in roots:
        await _arm(root)
    _state.observer.start()
    log.info("watcher: started with %d watch(es)", len(_state.watches))


async def stop() -> None:
    """Tear down the observer.  Safe to call multiple times."""
    if not _state.enabled:
        return
    _state.enabled = False
    if _state.observer:
        try:
            _state.observer.stop()
            # ``join`` blocks the event loop; run it off-thread.
            await asyncio.get_running_loop().run_in_executor(None, _state.observer.join)
        except Exception:
            log.exception("watcher: stop failed")
    _state.observer = None
    _state.watches.clear()
    _state.pending.clear()
    if _state.debounce_task and not _state.debounce_task.done():
        _state.debounce_task.cancel()
    _state.debounce_task = None


async def add_root(root: str) -> None:
    """Arm a watch on a newly-added scan root."""
    if not _state.enabled or not _state.observer:
        return
    await _arm(root)


async def remove_root(root: str) -> None:
    """Disarm the watch on a removed scan root."""
    if not _state.enabled or not _state.observer:
        return
    p = str(Path(root).resolve())
    watch = _state.watches.pop(p, None)
    if watch:
        try:
            _state.observer.unschedule(watch)
        except Exception:
            log.exception("watcher: unschedule failed for %s", p)


# ── Internals ────────────────────────────────────────────────────────────────

async def _arm(root: str) -> None:
    """Schedule a recursive watch on ``root``.  Skips remote / nonexistent
    paths and roots already armed."""
    p = Path(root)
    # Remote shares (smb://, ftp://, http://) and Cloud-only roots don't
    # produce inotify/FSEvents — skip them silently.
    if not p.exists() or not p.is_dir():
        return
    abs_p = str(p.resolve())
    if abs_p in _state.watches:
        return
    try:
        watch = _state.observer.schedule(_Handler(abs_p), abs_p, recursive=True)
        _state.watches[abs_p] = watch
        log.info("watcher: armed %s", abs_p)
    except Exception:
        log.exception("watcher: failed to arm %s", abs_p)
