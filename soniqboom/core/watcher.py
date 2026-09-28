# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Filesystem watcher — kicks an incremental rescan whenever files change
inside a scan root.

Implementation is a thin shim over the ``watchdog`` library, which
provides native FSEvents (macOS) / inotify (Linux) / kqueue (BSD) /
ReadDirectoryChangesW (Windows) backends.

* Only WRITE-side events count: created / modified / deleted / moved and
  closed-after-write.  inotify also reports plain reads (``opened``,
  ``closed_no_write``) — every playback opens the file — and treating those
  as changes rescanned the whole library after each play.
* The changed PATHS are collected per root (a file, or a folder for folder
  events, both sides of a move), each with the time of its last event, and
  handed to :func:`start_scan` as a SCOPED scan: the scanner walks, checks
  and prunes just those paths.  Paths inside a changed folder collapse into
  it; past ``_SCOPE_MAX`` collapsed paths a root falls back to a full
  (incremental) rescan.
* Debounce waits for ``_QUIET_SEC`` without new events (a copy of a whole
  album → one scan), but never longer than ``_MAX_WAIT_SEC`` after the first.
  A path still receiving events at that point (a file being downloaded or
  recorded), or a file whose mtime is that recent, is held back until it has
  been quiet for ``_QUIET_SEC`` — a partial file is not indexed and does not
  re-trigger the library-wide post-scan passes once a minute.
* On inotify the kernel only reports the event kinds above (plus
  close-after-write); write progress (``IN_MODIFY``) and reads never wake the
  process.  Other backends have no close events, so they keep ``modified``.
* More collapsed paths than ``_SCOPE_MAX`` are widened to their parent
  folders before a root falls back to a full rescan.

Armed for every local scan root at startup and when a root is added (never
for remote SMB/FTP shares, which have no push notifications).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import unicodedata
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.events import (
        EVENT_TYPE_CREATED, EVENT_TYPE_DELETED, EVENT_TYPE_MODIFIED, EVENT_TYPE_MOVED,
    )
    try:
        from watchdog.events import EVENT_TYPE_CLOSED
    except ImportError:                      # very old watchdog: no close events
        EVENT_TYPE_CLOSED = "closed"
    from watchdog.observers import Observer
    from watchdog.events import (
        DirCreatedEvent, DirDeletedEvent, DirMovedEvent, FileClosedEvent,
        FileCreatedEvent, FileDeletedEvent, FileMovedEvent,
    )
    # inotify event mask: write-side changes only (no IN_MODIFY / IN_ATTRIB /
    # IN_OPEN / IN_ACCESS / IN_CLOSE_NOWRITE).
    _INOTIFY_EVENT_FILTER = [FileCreatedEvent, FileDeletedEvent, FileMovedEvent,
                             FileClosedEvent, DirCreatedEvent, DirDeletedEvent,
                             DirMovedEvent]
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False
    Observer = None       # type: ignore[assignment]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    EVENT_TYPE_CREATED, EVENT_TYPE_DELETED = "created", "deleted"
    EVENT_TYPE_MODIFIED, EVENT_TYPE_MOVED, EVENT_TYPE_CLOSED = "modified", "moved", "closed"

# Events that mean "something on disk changed".  NOT ``opened`` /
# ``closed_no_write`` (reads).  ``closed`` = closed after a write.
_FILE_EVENTS = frozenset((EVENT_TYPE_CREATED, EVENT_TYPE_MODIFIED, EVENT_TYPE_DELETED,
                          EVENT_TYPE_MOVED, EVENT_TYPE_CLOSED))
_DIR_EVENTS = frozenset((EVENT_TYPE_CREATED, EVENT_TYPE_DELETED, EVENT_TYPE_MOVED))

_QUIET_SEC = 5.0        # rescan once events have been quiet this long…
_MAX_WAIT_SEC = 60.0    # …but at most this long after the first event
_SCOPE_MAX = 1024       # more changed (collapsed, widened) paths in one root → full rescan
_SCOPE_HARD_MAX = 8192  # raw paths buffered per root before giving up → full rescan


# ── Singleton observer state ─────────────────────────────────────────────────

class _State:
    enabled: bool = False
    observer: "Observer | None" = None
    watches: dict[str, object] = {}     # path → watchdog.ObservedWatch
    # root → {changed path: monotonic time of its last event}, or None once
    # the root needs a FULL rescan
    pending: dict[str, "dict[str, float] | None"] = {}
    debounce_task: asyncio.Task | None = None
    loop: asyncio.AbstractEventLoop | None = None
    first_event_at: float = 0.0
    last_event_at: float = 0.0
    warned_no_cover: bool = False


_state = _State()


# ── Event handler ────────────────────────────────────────────────────────────

def _canonical_path(path: str) -> str:
    """The on-disk spelling of an existing absolute path.  On a
    case-insensitive filesystem a root registered as ``…/music`` for the
    folder ``…/Music`` gets its events reported as ``…/Music/…``."""
    parts = Path(path).parts
    if not parts:
        return path
    cur = parts[0]
    for comp in parts[1:]:
        try:
            names = os.listdir(cur)
        except OSError:
            return path
        if comp not in names:
            want = unicodedata.normalize("NFC", comp).casefold()
            folded = [n for n in names if unicodedata.normalize("NFC", n).casefold() == want]
            if len(folded) != 1:
                return path
            comp = folded[0]
        cur = os.path.join(cur, comp)
    return cur


_name_check = None


def _interesting(path: str) -> bool:
    """A file the scanner would index: any supported music name (incl.
    Amiga prefix-form names) or a container it looks inside — never a name
    the scanner skips as junk (``._x.flac`` AppleDouble files)."""
    global _name_check
    if not path:
        return False
    try:
        if _name_check is None:
            from soniqboom.core.metadata import is_supported_music_name
            from soniqboom.core import diskimage
            from soniqboom.core.scanner import _is_junk_filename

            def _name_check(name: str) -> bool:
                lower = name.lower()
                if _is_junk_filename(name):
                    return False
                return (is_supported_music_name(name)
                        or lower.endswith((".zip", ".lha", ".lzh"))
                        or diskimage.is_disk_image(lower))
        return _name_check(os.path.basename(path))
    except Exception:                         # noqa: BLE001 — never kill the observer thread
        return False


class _Handler(FileSystemEventHandler):
    """Buffers FS events into the pending set; the debounce task drains it.

    ``root`` is the root as registered (what the scanner and the store use);
    ``event_root`` its on-disk spelling, which the backend reports paths in.
    """

    def __init__(self, root: str, event_root: str | None = None) -> None:
        self._root = root
        self._event_root = event_root if event_root and event_root != root else None

    _interesting = staticmethod(_interesting)

    def _cover_subtree(self, d: str) -> None:
        """inotify watches every folder separately, and watchdog adds them
        only for folders CREATED inside the tree: a folder MOVED IN from
        outside would stay unwatched (watchdog's own TODO).  Add a watch for
        it and each folder below — idempotent for folders already watched.
        Runs on the observer's dispatch thread, never on the event loop."""
        ino = self._inotify
        if ino is None:
            if type(_state.observer).__name__ == "InotifyObserver" and not _state.warned_no_cover:
                _state.warned_no_cover = True
                log.warning("watcher: this watchdog version doesn't expose its inotify "
                            "handle — folders moved into a library from outside are not "
                            "watched until the next restart")
            return
        for dirpath, _dirs, _files in os.walk(d):
            try:
                ino.add_watch(os.fsencode(dirpath))
            except OSError as exc:
                if exc.errno == 28:           # ENOSPC: fs.inotify.max_user_watches reached
                    log.warning("watcher: inotify watch limit reached — %s and below are "
                                "not watched (raise fs.inotify.max_user_watches)", dirpath)
                    return
                log.debug("watcher: could not watch %s", dirpath, exc_info=True)
            except Exception:                     # noqa: BLE001 — best effort
                log.debug("watcher: could not watch %s", dirpath, exc_info=True)
                return

    @property
    def _inotify(self):
        """The watch's low-level inotify handle (None on other backends or
        a watchdog without it)."""
        try:
            em = _state.observer._emitter_for_watch.get(self.watch)   # type: ignore[union-attr]
            buf = getattr(em, "_inotify", None)
            return getattr(buf, "_inotify", None)
        except Exception:                         # noqa: BLE001
            return None

    watch = None                                  # set by _arm once scheduled

    def _as_registered(self, p: str) -> str:
        er = self._event_root
        if er is not None and (p == er or p.startswith(er + os.sep)):
            return self._root + p[len(er):]
        return p

    def on_any_event(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        kind = event.event_type
        if event.is_directory:
            if kind not in _DIR_EVENTS:
                return
        elif kind not in _FILE_EVENTS:
            return                             # opened / closed_no_write: a read
        paths = [getattr(event, "src_path", "") or ""]
        if kind == EVENT_TYPE_MOVED:
            paths.append(getattr(event, "dest_path", "") or "")
        paths = [self._as_registered(os.fsdecode(p)) for p in paths if p]
        if event.is_directory and kind in (EVENT_TYPE_CREATED, EVENT_TYPE_MOVED):
            self._cover_subtree(os.fsdecode(getattr(event, "dest_path", "") or event.src_path))
        if not event.is_directory:
            # Only the music side of a file event counts: an rsync / download
            # temp name (``.x.flac.Ab12``) renamed to ``x.flac`` adds just the
            # final name.
            paths = [p for p in paths if _interesting(p)]
            if not paths:
                return
        _mark_dirty(self._root, paths)


def _mark_dirty(root: str, paths: "list[str] | None" = None) -> None:
    """Thread-safe enqueue from a watchdog worker thread."""
    loop = _state.loop
    if loop is None or not loop.is_running():
        return
    loop.call_soon_threadsafe(_note_change, root, list(paths or []))


def _note_change(root: str, paths: list[str]) -> None:
    """On the loop: record the changed paths of ``root`` (with the time of
    this event) and (re)arm the quiet-period debounce."""
    if root not in _state.watches:
        return                                 # root was removed meanwhile
    now = time.monotonic()
    if not _state.pending:
        _state.first_event_at = now
    _state.last_event_at = now
    if root in _state.pending and _state.pending[root] is None:
        pass                                   # already a full rescan
    elif not paths or root in paths:
        _state.pending[root] = None            # no path / the root itself → full rescan
    else:
        cur = _state.pending.setdefault(root, {})
        pre = root.rstrip(os.sep) + os.sep
        for p in paths:
            if p.startswith(pre):
                cur[p] = now
            else:
                # Under this watch but not spelled like the root (a Unicode /
                # case form the mapping missed): rescan the root rather than
                # drop the change.
                _state.pending[root] = None
                _schedule_debounce()
                return
        if len(cur) > _SCOPE_HARD_MAX:
            # A big move / copy: fold into folders before giving up.
            folded = _widen(_collapse(cur, root), root)
            _state.pending[root] = (None if folded is None or len(folded) > _SCOPE_HARD_MAX
                                    else folded)
    _schedule_debounce()


def _schedule_debounce() -> None:
    if _state.debounce_task and not _state.debounce_task.done():
        return
    _state.debounce_task = asyncio.create_task(_debounce_and_scan())


def _collapse(paths: "dict[str, float]", root: "str | None" = None) -> dict[str, float]:
    """Fold every path into its outermost collected ancestor (a changed folder
    covers its files); each result carries the LATEST event time among the
    paths folded into it — a folder is quiet only once all of it is.  With
    ``root`` the ancestor walk stops there."""
    keys = set(paths)
    stop = len(root.rstrip(os.sep)) if root else 0
    out: dict[str, float] = {}
    for p, ts in paths.items():
        top, cur = p, p
        while True:
            parent = os.path.dirname(cur)
            if parent == cur or len(parent) <= stop:
                break
            if parent in keys:
                top = parent
            cur = parent
        if ts > out.get(top, -1.0):
            out[top] = ts
    return out


def _widen(groups: "dict[str, float]", root: str) -> "dict[str, float] | None":
    """Replace changed paths by their parent folders, one level at a time,
    until at most ``_SCOPE_MAX`` remain (many files changed across a few
    folders → those folders).  None when even that doesn't fit below the
    root (→ full rescan)."""
    rootlen = len(root.rstrip(os.sep))
    while len(groups) > _SCOPE_MAX:
        up: dict[str, float] = {}
        moved = False
        for p, ts in groups.items():
            parent = os.path.dirname(p)
            if len(parent) > rootlen:          # a top-level entry stays as it is
                moved = True
                p = parent
            if ts > up.get(p, -1.0):
                up[p] = ts
        if not moved:
            return None
        groups = _collapse(up, root)
    return groups


def _recently_written(paths: "list[str]", now_wall: float) -> "set[str]":
    """Files whose mtime is within ``_QUIET_SEC`` of now (still being written
    without event noise — e.g. under the inotify filter).  Folders and
    vanished paths are never held."""
    out: set[str] = set()
    import stat as _stat
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        # (a future mtime — clock skew on a share — is never "being written")
        if not _stat.S_ISDIR(st.st_mode) and 0.0 <= now_wall - st.st_mtime < _QUIET_SEC:
            out.add(p)
    return out


def _within(path: str, dirs: "set[str]") -> bool:
    """``path`` is one of ``dirs`` or lies inside one (O(depth))."""
    cur = path
    while True:
        if cur in dirs:
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


async def _debounce_and_scan() -> None:
    """Wait until events have been quiet for ``_QUIET_SEC`` (at most
    ``_MAX_WAIT_SEC`` after the first), then queue one rescan per kind: a
    scoped scan of the changed paths, and a full incremental rescan for roots
    that overflowed or had a root-level change.  A burst (an rsync, a copied
    album) results in one rescan, not N."""
    # Loop until nothing is pending: events that arrive while a batch is being
    # queued (this task is still running, so they don't start a new one) are
    # picked up by the next pass instead of waiting for some later event.
    while _state.pending:
        while True:
            now = time.monotonic()
            due = min(_state.last_event_at + _QUIET_SEC,
                      _state.first_event_at + _MAX_WAIT_SEC)
            if now >= due:
                break
            await asyncio.sleep(due - now)
        # Split synchronously (no await until ``_state.pending`` is rebuilt):
        # quiet paths are scanned now; paths still receiving events are put
        # back and scanned once they settle.
        pending, _state.pending = _state.pending, {}
        now = time.monotonic()
        full: list[str] = []
        scoped: dict[str, frozenset[str]] = {}
        quiet: dict[str, list[str]] = {}
        for r, paths in pending.items():
            if r not in _state.watches:
                continue                        # root removed while pending
            if paths is None:
                full.append(r)
                continue
            groups = _widen(_collapse(paths, r), r)
            if groups is None:
                full.append(r)
                continue
            ready = [g for g, ts in groups.items() if now - ts >= _QUIET_SEC]
            hot = {g for g in groups if now - groups[g] < _QUIET_SEC}
            if ready:
                quiet[r] = ready
            if hot:
                _state.pending[r] = {p: ts for p, ts in paths.items() if _within(p, hot)}
        # A quiet file with a fresh mtime is still being written: hold it too
        # (a stat per ready path, off the loop).
        if quiet:
            wall = time.time()
            fresh = await asyncio.to_thread(
                _recently_written, [g for v in quiet.values() for g in v], wall)
            for r, ready in quiet.items():
                if r not in _state.watches:
                    continue                    # removed while the stat ran
                keep = frozenset(g for g in ready if g not in fresh)
                if keep:
                    scoped[r] = keep
                held = [g for g in ready if g in fresh]
                if held and _state.pending.get(r, {}) is not None and r in _state.watches:
                    cur = _state.pending.setdefault(r, {})
                    t_hold = time.monotonic()
                    for g in held:
                        cur[g] = t_hold
                    _state.last_event_at = max(_state.last_event_at, t_hold)
        if _state.pending:
            _state.first_event_at = now         # the cap restarts for held paths
        if not scoped and not full:
            continue
        try:
            from soniqboom.core.scanner import start_scan
            if scoped:
                log.info("watcher: scoped rescan of %d path(s) in %d root(s)",
                         sum(len(v) for v in scoped.values()), len(scoped))
                await start_scan(list(scoped), scope=scoped)
            if full:
                log.info("watcher: full rescan of %d root(s): %s", len(full), full)
                await start_scan(full, rescan_if_running=True, light=True)
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
    _state.pending.pop(p, None)                # never rescan (and re-add) a removed root
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
        handler = _Handler(abs_p, await asyncio.to_thread(_canonical_path, abs_p))
        kw = {}
        if type(_state.observer).__name__ == "InotifyObserver":
            kw["event_filter"] = _INOTIFY_EVENT_FILTER
        try:
            watch = _state.observer.schedule(handler, abs_p, recursive=True, **kw)
        except TypeError:                     # watchdog without ``event_filter``
            watch = _state.observer.schedule(handler, abs_p, recursive=True)
        handler.watch = watch
        _state.watches[abs_p] = watch
        log.info("watcher: armed %s", abs_p)
    except Exception:
        log.exception("watcher: failed to arm %s", abs_p)
