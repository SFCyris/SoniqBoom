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

Each root is armed on its own, off the event loop, on an observer that is
already running: one root that can't be watched (Linux: the inotify watch or
instance limit, ``ENOSPC`` / ``EMFILE``; an unreadable folder) never stops the
others.  A root that can't be watched is retried with a doubling back-off
(``_RETRY_BASE_SEC`` → ``_RETRY_MAX_SEC``) and, until it is watched again, gets
an incremental rescan every ``_FALLBACK_POLL_SEC``; once it is watched again
one rescan covers what changed meanwhile.  A watch whose thread stops, or a
folder moved in that hits the watch limit, is handled the same way.  The state
of every root is in :func:`root_status` / :func:`summary` (Settings → Library
folders, ``/api/health``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
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

_RETRY_BASE_SEC = 60.0          # first re-arm of a root that couldn't be watched…
_RETRY_MAX_SEC = 30 * 60.0      # …doubling up to this
_RETRY_MISSING_MAX_SEC = 5 * 60.0   # a missing root (unmounted drive) is a cheap stat
_FALLBACK_POLL_SEC = 15 * 60.0  # incremental rescan of a root that isn't watched
_SUPERVISE_SEC = 5.0            # how often retries / polls / watch threads are checked
_LIMIT_WARN_SEC = 10 * 60.0     # one "watch limit reached" warning per root per this long

# root → (monotonic time of its last "watch limit" warning, folders not
# warned about since) — folders moved in one after another past the limit
# would otherwise log one warning each.
_limit_warned: dict = {}
_limit_warned_lock = threading.Lock()


def _limit_warning_due(root: str) -> "int | None":
    """None while ``root``'s last watch-limit warning is recent (the folder is
    counted); else the number of folders not warned about since (the caller
    warns now)."""
    now = time.monotonic()
    with _limit_warned_lock:
        last, held = _limit_warned.get(root, (None, 0))
        if last is not None and now - last < _LIMIT_WARN_SEC:
            _limit_warned[root] = (last, held + 1)
            return None
        _limit_warned[root] = (now, 0)
        return held


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
    roots: "set[str]" = set()           # roots asked for (resolved), armed or not
    # root → {"state": "watching" | "partial" | "retrying" | "missing", ...}
    status: "dict[str, dict]" = {}
    supervisor: "asyncio.Task | None" = None
    arm_lock: "asyncio.Lock | None" = None
    handlers: "dict[str, _Handler]" = {}  # root → its event handler (partial-root retries)

    def __init__(self) -> None:
        self.roots = set()
        self.status = {}
        self.handlers = {}


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

    def _cover_subtree(self, d: str, *, notify: bool = True) -> bool:
        """inotify watches every folder separately, and watchdog adds them
        only for folders CREATED inside the tree: a folder MOVED IN from
        outside would stay unwatched (watchdog's own TODO).  Add a watch for
        it and each folder below — idempotent for folders already watched.
        Runs on the observer's dispatch thread (or, to retry a partly watched
        root, a worker thread), never on the event loop.  False when the
        watch limit stopped it (``notify``: the root is marked partial)."""
        ino = self._inotify
        if ino is None:
            if type(_state.observer).__name__ == "InotifyObserver" and not _state.warned_no_cover:
                _state.warned_no_cover = True
                log.warning("watcher: this watchdog version doesn't expose its inotify "
                            "handle — folders moved into a library from outside are not "
                            "watched until the next restart")
            return True
        for dirpath, _dirs, _files in os.walk(d):
            try:
                ino.add_watch(os.fsencode(dirpath))
            except OSError as exc:
                if exc.errno == 28:           # ENOSPC: fs.inotify.max_user_watches reached
                    if notify:
                        held = _limit_warning_due(self._root)
                        if held is not None:
                            log.warning("watcher: inotify watch limit reached — %s and below "
                                        "are not watched (raise fs.inotify.max_user_watches)%s",
                                        dirpath,
                                        f"; {held} more folder(s) in {self._root} since the "
                                        f"last warning" if held else "")
                        _call_on_loop(_note_partial, self._root, _limit_reason(exc))
                    return False
                log.debug("watcher: could not watch %s", dirpath, exc_info=True)
            except Exception:                     # noqa: BLE001 — best effort
                log.debug("watcher: could not watch %s", dirpath, exc_info=True)
                return True
        return True

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
    """Start the observer, then arm a watch on every (local) root — each on
    its own, so a root that can't be watched never stops the others."""
    if not _HAS_WATCHDOG:
        log.warning("watcher: ``watchdog`` not installed; auto-rescan disabled.")
        return
    if _state.enabled:
        return
    _state.enabled = True
    _state.loop = asyncio.get_running_loop()
    _state.roots = set()
    _state.status = {}
    _state.handlers = {}
    # (a previous run in this process — the bundled app — may have left
    # these behind: a root still listed as watched would never be armed on
    # this run's observer)
    _state.watches = {}
    _state.pending = {}
    _state.debounce_task = None
    _state.arm_lock = asyncio.Lock()
    _state.observer = Observer()
    # Started before any watch is scheduled: ``schedule`` on a running
    # observer starts that root's emitter at once, so its failure is that
    # root's alone (watchdog's ``start`` re-raised the first emitter's error
    # and left every other root — and the dispatcher — unstarted).
    _state.observer.start()
    for root in roots:
        await _arm(root)
    _state.supervisor = asyncio.create_task(_supervise(), name="watcher.supervise")
    n_bad = sum(1 for st in _state.status.values() if st.get("state") != "watching")
    log.info("watcher: started with %d watch(es)%s", len(_state.watches),
             f", {n_bad} root(s) not watched" if n_bad else "")


async def stop() -> None:
    """Tear down the observer.  Safe to call multiple times.

    The watcher's state is cleared first, before any await: a stop cancelled
    while it waits (the shutdown step's timeout) still leaves nothing behind
    for a later ``start``, and an arm still running finds the watcher off
    (``_arm`` checks after each await).  The observer's ``stop`` + ``join``
    run in a worker thread — ``stop`` waits for the observer's lock, which a
    ``schedule`` in progress (inotify adding a watch per folder of a big
    tree) holds for as long as it takes."""
    if not _state.enabled:
        return
    _state.enabled = False
    obs, _state.observer = _state.observer, None
    sup, _state.supervisor = _state.supervisor, None
    _state.watches = {}
    _state.pending = {}
    _state.roots = set()
    _state.status = {}
    _state.handlers = {}
    with _limit_warned_lock:
        _limit_warned.clear()
    deb, _state.debounce_task = _state.debounce_task, None
    if deb is not None and not deb.done():
        deb.cancel()
    if sup is not None and not sup.done():
        sup.cancel()
        try:
            await sup
        except (asyncio.CancelledError, Exception):
            pass
    if obs is not None:
        def _stop_join() -> None:
            obs.stop()
            obs.join()
        try:
            await asyncio.to_thread(_stop_join)
        except Exception:
            log.exception("watcher: stop failed")


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
    _state.roots.discard(p)
    _state.status.pop(p, None)
    _state.handlers.pop(p, None)
    _state.pending.pop(p, None)                # never rescan (and re-add) a removed root
    watch = _state.watches.pop(p, None)
    if watch:
        try:
            # (waits for the observer's lock: off the loop)
            await asyncio.to_thread(_state.observer.unschedule, watch)
        except Exception:
            log.exception("watcher: unschedule failed for %s", p)


def root_status(root: str) -> "dict | None":
    """How ``root`` is kept up to date — None for a root the watcher doesn't
    handle (remote, or the watcher is off).  ``state``: ``watching``;
    ``partial`` (watched, but folders below ``reason``'s limit are not);
    ``retrying`` (not watched: re-armed every ``retry_in_s`` with back-off,
    rescanned every ``poll_interval_s`` meanwhile); ``missing`` (the folder
    isn't there — re-armed when it is)."""
    if not _state.enabled:
        return None
    # No ``resolve()`` here (a stalled mount would block the caller): the
    # folder list stores roots in their resolved spelling already.
    st = _state.status.get(root) or _state.status.get(os.path.normpath(os.path.abspath(root)))
    return _public(st) if st is not None else None


def summary() -> dict:
    """Counts only (no paths) — for the public health endpoint."""
    if not _HAS_WATCHDOG:
        return {"state": "unsupported", "roots": 0, "watching": 0, "not_watching": 0,
                "missing": 0}
    if not _state.enabled:
        return {"state": "off", "roots": 0, "watching": 0, "not_watching": 0, "missing": 0}
    states = [st.get("state") for st in _state.status.values()]
    watching = sum(1 for x in states if x == "watching")
    missing = sum(1 for x in states if x == "missing")
    bad = len(states) - watching - missing
    return {"state": "degraded" if bad else "ok", "roots": len(states),
            "watching": watching, "not_watching": bad, "missing": missing}


# ── Internals ────────────────────────────────────────────────────────────────

def _public(st: dict) -> dict:
    now = time.time()
    out = {
        "state": st.get("state"),
        "reason": st.get("reason"),
        "since": st.get("since"),
        "attempts": st.get("attempts", 0),
        "retry_in_s": None,
        "polling": bool(st.get("polling")),
        "poll_interval_s": int(_FALLBACK_POLL_SEC) if st.get("polling") else None,
        "last_poll_ts": st.get("last_poll"),
    }
    if st.get("state") != "watching" and st.get("next_retry"):
        out["retry_in_s"] = max(0, int(st["next_retry"] - now))
    return out


def _call_on_loop(fn, *args) -> None:
    """Run ``fn(*args)`` on the event loop (from an observer thread)."""
    loop = _state.loop
    if loop is None or not loop.is_running():
        return
    try:
        loop.call_soon_threadsafe(fn, *args)
    except RuntimeError:                       # loop closed meanwhile
        pass


def _limit_reason(exc: BaseException) -> str:
    """The reason shown for a root that couldn't be watched."""
    err = getattr(exc, "errno", None)
    if err == 28:                              # ENOSPC
        return "inotify watch limit reached, raise fs.inotify.max_user_watches"
    if err == 24:                              # EMFILE
        return "inotify instance limit reached, raise fs.inotify.max_user_instances"
    msg = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    return msg


def _retry_delay(attempts: int, *, missing: bool = False) -> float:
    cap = _RETRY_MISSING_MAX_SEC if missing else _RETRY_MAX_SEC
    return min(cap, _RETRY_BASE_SEC * (2 ** max(0, attempts - 1)))


def _set_watching(abs_p: str) -> None:
    _state.status[abs_p] = {"state": "watching", "reason": None, "since": time.time(),
                            "attempts": 0, "polling": False}


def _set_failed(abs_p: str, reason: str, *, state: str = "retrying",
                poll_now: bool = False) -> dict:
    """Record that ``abs_p`` isn't (fully) watched and when to try again."""
    now = time.time()
    prev = _state.status.get(abs_p) or {}
    attempts = int(prev.get("attempts", 0)) + 1
    was_bad = prev.get("state") not in (None, "watching")
    polling = state in ("retrying", "partial")
    st = {
        "state": state,
        "reason": reason,
        "since": prev.get("since") if was_bad and prev.get("since") else now,
        "attempts": attempts,
        "next_retry": now + _retry_delay(attempts, missing=(state == "missing")),
        "polling": polling,
        "next_poll": (now if poll_now else
                      prev.get("next_poll") or now + _FALLBACK_POLL_SEC) if polling else None,
        "last_poll": prev.get("last_poll"),
    }
    _state.status[abs_p] = st
    return st


def _note_partial(root: str, reason: str = "") -> None:
    """On the loop: a folder moved into ``root`` hit the watch limit — the
    root is only partly watched.  Rescan it now and on the poll cadence, and
    re-arm it with back-off."""
    if root not in _state.roots:
        return
    st = _state.status.get(root) or {}
    if st.get("state") == "partial":
        return                                 # already handled
    _set_failed(root, reason or "watch limit reached", state="partial", poll_now=True)
    log.warning("watcher: %s is only partly watched (%s) — rescanning it every %d min "
                "until it is fully watched again", root, reason, _FALLBACK_POLL_SEC // 60)


def _release_failed_arm(exc: BaseException, observer, abs_p: str) -> None:
    """Free what a failed ``schedule`` left behind.  watchdog's inotify opens
    its instance and adds watches folder by folder; when one fails (the
    watch limit) the half-built instance is dropped WITHOUT closing it — its
    descriptor, kill pipe and every watch it added stay allocated (and count
    against the limit) until the process ends.  It is still reachable from the
    exception's traceback: close it there.  The handler ``schedule``
    registered for the watch is dropped too."""
    tb = exc.__traceback__
    seen: set[int] = set()
    while tb is not None:
        obj = tb.tb_frame.f_locals.get("self")
        tb = tb.tb_next
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        if type(obj).__name__ == "Inotify" and hasattr(obj, "_close_resources"):
            if getattr(obj, "_closed", True):
                continue
            try:
                obj._closed = True
                obj._close_resources()
            except Exception:                  # noqa: BLE001 — best effort
                log.debug("watcher: closing a half-built inotify failed", exc_info=True)
    try:
        lock = getattr(observer, "_lock", None)
        handlers = getattr(observer, "_handlers", None)
        watches = getattr(observer, "_watches", set())
        if lock is not None and handlers is not None:
            with lock:
                for w in [w for w in list(handlers) if getattr(w, "path", None) == abs_p
                          and w not in watches]:
                    handlers.pop(w, None)
    except Exception:                          # noqa: BLE001
        log.debug("watcher: dropping a failed watch's handler failed", exc_info=True)


def _schedule_blocking(observer, handler, abs_p: str):
    """``observer.schedule`` — on a running observer this starts the root's
    emitter (inotify: a watch per folder of the tree), so it runs in a
    worker thread.  Raises what the backend raised, after cleaning up."""
    kw = {}
    if type(observer).__name__ == "InotifyObserver":
        kw["event_filter"] = _INOTIFY_EVENT_FILTER
    try:
        try:
            return observer.schedule(handler, abs_p, recursive=True, **kw)
        except TypeError:                     # watchdog without ``event_filter``
            return observer.schedule(handler, abs_p, recursive=True)
    except BaseException as exc:
        _release_failed_arm(exc, observer, abs_p)
        raise


async def _arm(root: str, *, retry: bool = False) -> bool:
    """Schedule a recursive watch on ``root`` (skips remote paths and roots
    already armed).  A root that can't be watched — missing, or the backend
    refused it — is recorded in ``_state.status`` and retried by
    :func:`_supervise` (``retry``: such a retry never adds the root again —
    one removed while it waited stays removed).  True when the root is
    watched."""
    try:
        from soniqboom.core.filesource import is_remote_path
        if is_remote_path(root):
            return False                      # no push notifications on a share
    except Exception:                         # noqa: BLE001
        pass
    p = Path(root)
    abs_p = await asyncio.to_thread(lambda: str(p.resolve()))
    lock = _state.arm_lock
    if lock is None:
        lock = _state.arm_lock = asyncio.Lock()
    async with lock:
        if not _state.enabled or _state.observer is None:
            return False
        observer = _state.observer
        if retry and abs_p not in _state.roots:
            return False                      # removed while this retry waited

        def _gone() -> bool:
            """After an await: the watcher stopped (or was started again —
            another observer), or the root was removed meanwhile.  Nothing of
            this arm may land in the state then."""
            return (not _state.enabled or _state.observer is not observer
                    or abs_p not in _state.roots)

        _state.roots.add(abs_p)
        if abs_p in _state.watches:
            return True
        is_dir = await asyncio.to_thread(lambda: p.exists() and p.is_dir())
        if _gone():
            return False                      # removed / stopped meanwhile
        if not is_dir:
            prev = _state.status.get(abs_p) or {}
            st = _set_failed(abs_p, "folder not found", state="missing")
            if prev.get("state") != "missing":
                log.info("watcher: %s is not there — watching it once it is (next "
                         "check in %.0f s)", abs_p, st["next_retry"] - time.time())
            return False
        prev = _state.status.get(abs_p) or {}
        try:
            handler = _Handler(abs_p, await asyncio.to_thread(_canonical_path, abs_p))
            if _gone():
                return False                  # removed / stopped meanwhile
            watch = await asyncio.to_thread(_schedule_blocking, observer, handler, abs_p)
        except Exception as exc:
            if _gone():
                return False                  # removed / stopped while arming
            reason = _limit_reason(exc)
            st = _set_failed(abs_p, reason)
            delay = st["next_retry"] - time.time()
            if prev.get("state") in (None, "watching", "missing"):
                log.error("watcher: cannot watch %s — %s; rescanning it every %d min "
                          "and retrying in %.0f s (the other folders are not affected)",
                          abs_p, reason, _FALLBACK_POLL_SEC // 60, delay)
                if not isinstance(exc, OSError):
                    log.debug("watcher: arm failure detail", exc_info=True)
            else:
                log.info("watcher: %s still can't be watched (%s, attempt %d) — next "
                         "retry in %.0f s", abs_p, reason, st["attempts"], delay)
            return False
        if _gone():
            # Removed, stopped or the observer replaced while arming: the
            # watch goes (off the loop — ``unschedule`` waits for the
            # observer's lock; a stopped observer drops it anyway).
            try:
                await asyncio.to_thread(observer.unschedule, watch)
            except Exception:                 # noqa: BLE001
                pass
            return False
        handler.watch = watch
        _state.watches[abs_p] = watch
        _state.handlers[abs_p] = handler
        _set_watching(abs_p)
        if prev.get("state") in ("retrying", "partial", "missing"):
            log.info("watcher: %s is watched again (after %d attempt(s)) — one rescan "
                     "picks up what changed meanwhile", abs_p, prev.get("attempts", 0))
            await _rescan_root(abs_p)
        else:
            log.info("watcher: armed %s", abs_p)
        return True


async def _rescan_root(abs_p: str) -> None:
    """One incremental (light) rescan of ``abs_p`` — the fallback poll, and
    the catch-up once a root is watched again."""
    try:
        from soniqboom.core.scanner import start_scan
        await start_scan([abs_p], rescan_if_running=True, light=True)
    except Exception:
        log.exception("watcher: rescan of %s failed", abs_p)


def _emitter_alive(watch) -> bool:
    obs = _state.observer
    try:
        em = obs._emitter_for_watch.get(watch)        # type: ignore[union-attr]
    except Exception:                                  # noqa: BLE001
        return True                                    # can't tell: assume fine
    if em is None:
        return True
    if not em.is_alive():
        return False
    # inotify: the reader thread ends on its own (the watched folder was
    # unmounted: IN_UNMOUNT / IN_IGNORED) while the emitter keeps waiting.
    buf = getattr(em, "_inotify", None)
    return buf is None or not hasattr(buf, "is_alive") or buf.is_alive()


async def _restart_observer() -> None:
    """The dispatcher thread stopped: start a new observer and re-arm every
    root on it (each rescanned once it is watched again)."""
    old = _state.observer
    log.error("watcher: the observer stopped — restarting it and re-arming %d root(s)",
              len(_state.roots))
    try:
        if old is not None:
            await asyncio.to_thread(old.stop)          # joins its emitters
    except Exception:                                  # noqa: BLE001
        pass
    _state.observer = Observer()
    _state.observer.start()
    _state.watches.clear()
    _state.handlers.clear()
    for r in list(_state.roots):
        st = _set_failed(r, "the watcher restarted", poll_now=False)
        st["next_retry"] = 0.0


async def _supervise_once() -> None:
    """Retry roots that can't be watched, rescan them meanwhile, and notice a
    watch (or the whole observer) that stopped."""
    obs = _state.observer
    if obs is None or not _state.enabled:
        return
    if not obs.is_alive():
        await _restart_observer()
    now = time.time()
    for root in list(_state.roots):
        st = _state.status.get(root)
        if st is None:
            continue
        if st.get("state") == "watching":
            w = _state.watches.get(root)
            if w is not None and not _emitter_alive(w):
                _state.watches.pop(root, None)
                _state.handlers.pop(root, None)
                try:
                    await asyncio.to_thread(_state.observer.unschedule, w)
                except Exception:                      # noqa: BLE001
                    pass
                _set_failed(root, "the watch stopped", poll_now=True)
                log.error("watcher: the watch on %s stopped — rescanning it and re-arming",
                          root)
            continue
        if st.get("next_retry", 0.0) <= now:
            if st.get("state") == "partial":
                # Keep the watch (most of the tree is watched) and add the
                # folders that are missing — idempotent for the others.
                h = _state.handlers.get(root)
                ok = h is not None and await asyncio.to_thread(h._cover_subtree, root,
                                                               notify=False)
                if root not in _state.roots:
                    continue                           # removed meanwhile
                if ok:
                    _set_watching(root)
                    log.info("watcher: %s is fully watched again — one rescan picks up "
                             "what changed meanwhile", root)
                    await _rescan_root(root)
                    continue
                _set_failed(root, st.get("reason") or "watch limit reached", state="partial")
            else:
                await _arm(root, retry=True)
            st = _state.status.get(root)
            if st is None or st.get("state") == "watching":
                continue
        if st.get("polling") and (st.get("next_poll") or 0.0) <= now:
            st["next_poll"] = now + _FALLBACK_POLL_SEC
            st["last_poll"] = now
            await _rescan_root(root)


async def _supervise() -> None:
    while _state.enabled:
        await asyncio.sleep(_SUPERVISE_SEC)
        try:
            await _supervise_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watcher: supervision pass failed (continuing)")
