# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Music folders and network shares being removed (Settings → Library →
Remove / Disconnect, with "purge tracks").

A removal is gone at once for listeners: the root leaves the folder lists
(``/api/library/dirs``, the admin list's ``dirs``, Subsonic's music folders,
the offline-roots banner) and its tracks leave the lists, counts, searches,
playlists and auto-play (``hidden_ids`` — joined into ``availability``'s
hidden and skipped sets; tracks a scan adds meanwhile join them) and the
Subsonic / DLNA library.  (Listening history and a folder view opened before
keep showing them until they are purged.)  Only Settings shows the removal:
the admin list's ``removing`` rows (``status``).

For ``UNDO_S`` the removal only waits (``undo`` drops it — nothing was
changed but the hand-over below, which Undo reverts); then the root is dropped
as before (the scan-dir entry, its alias,
the watcher and queued scans; a share's config entry, source, freshness poll
and cached files) and its tracks are purged in slices
(``data.delete_tracks_by_scan_root``).  The registry is store config
(``KEY``): a restart (or a shutdown mid-way) finishes what was waiting or
purging, without a new undo window; a step that failed is kept, shown, and
tried again at the next start.  Adding the root again cancels a waiting
removal (``cancel``) and stops a purge under way.

The removal itself doesn't read the root's files (an unreachable mount or
share is the usual reason to remove one); a share's source is closed as
before.

A removal never deletes a track another registered root still covers (nested
music folders: ``Music`` and ``Music/Box/CD2`` both registered, each track
tagged with one of them).  Such tracks are handed over to the innermost root
still covering them (``data.hand_over_covered``) when the removal begins, so
they stay listed and playable, and Undo hands them back.  Tracks the root
gains while it waits are handed over when it is dropped, and the purge hands
over whatever is still covered before deleting anything (and again whenever a
root comes or goes, or a removal ends, meanwhile).  A root being removed covers
nothing; when an Undo lists one again, the other removals still waiting hand
over what it covers — and a purge doesn't start while a root above or below
it can still be undone."""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

KEY = "removing_roots"
UNDO_S = 10.0

_ids: dict[str, frozenset] = {}         # root → its track ids (at the last registry change)
_hidden: frozenset | None = None        # the union of ``_ids`` (None: nothing removed)
_ver = 0
_tasks: dict[str, asyncio.Task] = {}    # root → its finishing task
_purging: set[str] = set()
_readded: set[str] = set()              # added again while purging: the purge stops
_seen_seq = None                        # the store's ``_catalog_seq`` ``_ids`` last followed
_moved: dict[str, dict[str, str]] = {}  # root → {track id: root hash it was handed to} (Undo)
_attempt: dict[str, object] = {}        # root → the token of its begin still handing over
_side: set = set()                      # a begin handing over; hand-overs after an Undo
_holding: set[str] = set()              # past the window, waiting for a neighbour (``_finish``)


def _store(store=None):
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    return store


def registry(store=None) -> dict:
    """``{root: {"share_id", "at", "total"}}`` — the removals not finished."""
    reg = _store(store).get_config(KEY) or {}
    return reg if isinstance(reg, dict) else {}


def is_removing(path: str, store=None) -> bool:
    return path in registry(store)


def purging() -> frozenset:
    """The roots past their undo window (finishing: no Undo any more)."""
    return frozenset(_purging)


def root_hashes(store=None) -> frozenset:
    """The path hashes of the roots being removed."""
    from soniqboom.core.data import path_hash
    return frozenset(path_hash(p) for p in registry(store))


def hidden_ids() -> tuple[frozenset | None, int]:
    """``(ids, version)``: the tracks of the roots being removed — the same
    object until a removal begins or ends, or a track joins a root being
    removed (a scan still running; checked when the store's ``_catalog_seq``
    moved)."""
    global _hidden, _ver, _seen_seq
    if _ids:
        store = _store()
        seq = getattr(store, "_catalog_seq", None)
        if seq != _seen_seq:
            _seen_seq = seq
            from soniqboom.core.data import path_hash
            idx = getattr(store, "_tag_scan_root_hash", {}) or {}
            grew = False
            for p, ids in list(_ids.items()):
                live = idx.get(path_hash(p))
                if live and not live <= ids:
                    _ids[p] = ids | frozenset(live)
                    grew = True
            if grew:
                _union_ids()
                from soniqboom.core import availability
                availability.notify_changed()
    return _hidden, _ver


def _union_ids() -> None:
    global _hidden, _ver
    both: set = set()
    for ids in _ids.values():
        both |= ids
    _hidden = frozenset(both) if both else None
    _ver += 1


def _refresh(store) -> None:
    """Rebuild the hidden union after the registry changed — every removal's
    tracks as tagged now (a hand-over or an Undo moves tracks between roots)
    — and tell the lists (availability token) and the Subsonic catalogue (its
    sequence)."""
    reg = registry(store)
    from soniqboom.core.data import path_hash
    fresh = {p: frozenset(store.get_track_ids_for_scan_root(path_hash(p))) for p in reg}
    _ids.clear()
    _ids.update(fresh)
    _union_ids()
    try:
        store._catalog_seq += 1                  # (the Subsonic catalogue is rebuilt)
    except Exception:                            # noqa: BLE001 — a stand-in store
        pass
    from soniqboom.core import availability
    availability.notify_changed()


def _save(store, reg: dict) -> None:
    if reg:
        store.set_config(KEY, reg)
    else:
        store.delete_config(KEY)


async def begin(path: str, *, share_id: "str | None" = None, store=None) -> dict:
    """Start removing ``path`` (a share's scan root with ``share_id``): its
    tracks another registered root still covers are handed over to that root
    first (in slices of the loop; they stay in the library and are not
    counted in ``total``), then the rest is hidden, and dropped and purged
    after ``UNDO_S``.  Returns the removal's record ({} when an Undo came in
    while handing over)."""
    store = _store(store)
    from soniqboom.core.data import hand_over_covered, path_hash
    reg = dict(registry(store))
    if path in reg:
        return reg[path]                        # (already being removed)
    h = path_hash(path)
    reg[path] = {"share_id": share_id, "at": time.time(),
                 "total": len(store.get_track_ids_for_scan_root(h))}
    _save(store, reg)                           # (claimed: a second begin returns it)
    token = _attempt[path] = object()           # (an Undo meanwhile drops it)
    moved = _moved[path] = {}

    def _ours() -> bool:
        return _attempt.get(path) is token
    me = asyncio.current_task()                 # (a store writer: stopped at shutdown)
    if me is not None:
        _side.add(me)
    try:
        await hand_over_covered(store, path, exclude=reg, moved=moved, keep_going=_ours)
        for _ in range(3):
            # A removal undone meanwhile lists its root again: what it covers.
            now = dict(registry(store))
            if not _ours() or set(now) >= set(reg):
                break
            reg = now
            await hand_over_covered(store, path, exclude=reg, moved=moved, keep_going=_ours)
    except BaseException as exc:                # (failed or cancelled: it didn't begin)
        if _ours():
            _attempt.pop(path, None)
            _moved.pop(path, None)
            left = dict(registry(store))
            if left.pop(path, None) is not None:
                _save(store, left)
            try:
                _hand_back_moves(store, path, moved)
            except Exception:                   # noqa: BLE001 — the first error is raised
                log.exception("Handing %s's tracks back failed", path)
            _refresh(store)
            if not isinstance(exc, asyncio.CancelledError):   # (shutdown: no new writer)
                _rebalance_later(store)         # (it covers what it held again)
        raise
    finally:
        if me is not None:
            _side.discard(me)
    if not _ours():
        return {}                               # (undone meanwhile: Undo handed them back)
    _attempt.pop(path, None)
    if not moved:
        _moved.pop(path, None)
    reg = dict(registry(store))
    reg[path] = {**reg[path], "at": time.time(),          # (the undo window starts now)
                 "total": len(store.get_track_ids_for_scan_root(h))}
    _save(store, reg)
    _refresh(store)
    _spawn(path, UNDO_S)
    return reg[path]


def _spawn(path: str, delay: float) -> None:
    old = _tasks.pop(path, None)
    if old is not None and not old.done():
        old.cancel()
    t = asyncio.get_running_loop().create_task(_finish(path, delay),
                                               name=f"removal[{path}]")
    _tasks[path] = t

    def _done(task: asyncio.Task) -> None:
        if _tasks.get(path) is task:
            _tasks.pop(path, None)
        if not task.cancelled() and task.exception() is not None:
            log.error("Removing %s failed: %r", path, task.exception())
    t.add_done_callback(_done)


def undo(path: str, store=None) -> bool:
    """Drop a removal still waiting (False: none, or it has started).  The
    tracks it handed over go back (``_hand_back_moves``), and the other
    removals still waiting hand over what the root, listed again, covers
    (``_rebalance``, in a task)."""
    store = _store(store)
    reg = dict(registry(store))
    if path not in reg or path in _purging:
        return False
    t = _tasks.pop(path, None)
    if t is not None and not t.done():
        t.cancel()
    _attempt.pop(path, None)                    # (a begin still handing over stops)
    reg.pop(path, None)
    _save(store, reg)
    try:
        _hand_back_moves(store, path, _moved.pop(path, None))
    finally:
        _refresh(store)
        _rebalance_later(store)
    log.info("Removal of %s undone", path)
    return True


def spawn_side(coro, name: str) -> "asyncio.Task | None":
    """Run ``coro`` (a hand-over) as a task listed in ``tasks()``, so it is
    stopped with the other store writers at shutdown."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:                        # (no loop: nothing to run it on)
        coro.close()
        return None
    t = loop.create_task(coro, name=name)
    _side.add(t)
    t.add_done_callback(_side.discard)
    return t


def _rebalance_later(store) -> None:
    if registry(store):
        spawn_side(_rebalance(store), "removal-rebalance")


async def _rebalance(store) -> None:
    """After an Undo: each removal still waiting hands over the tracks a root
    listed again now covers (recorded for its own Undo), then the hidden sets
    follow the tags."""
    from soniqboom.core.data import hand_over_covered
    for p in list(registry(store)):
        if p in _purging or p in _attempt:
            continue

        def _waiting(p=p) -> bool:
            return p in registry(store) and p not in _purging and p not in _attempt
        moved = _moved.setdefault(p, {})
        try:
            await hand_over_covered(store, p, exclude=registry(store), moved=moved,
                                    keep_going=_waiting)
        except Exception:                       # noqa: BLE001 — its finish hands them over
            log.exception("Handing over the tracks of %s failed", p)
        if not moved and _moved.get(p) is moved:
            _moved.pop(p, None)
    _refresh(store)


def _hand_back_moves(store, path: str, moved: "dict[str, str] | None") -> None:
    """Undo: the tracks this removal handed over (``moved``: ``begin``'s and
    ``_rebalance``'s) return to ``path`` — those still tagged with the root
    they were handed to (a scan meanwhile may have tagged one again).  One
    batch, on the loop (Undo is rare)."""
    if not moved:
        return
    from soniqboom.core.data import _browse_roots_changed, path_hash
    h = path_hash(path)
    tracks = store._tracks
    back = [(tid, {"scan_root_hash": h}) for tid, to in moved.items()
            if (tracks.get(tid) or {}).get("scan_root_hash") == to]
    if back:
        store.update_track_fields_batch(back)
        _browse_roots_changed(store, {h, *moved.values()})


def cancel(path: str, store=None) -> None:
    """``path`` is being added again: a waiting removal is dropped — also one
    whose purge still waits for a neighbour (``_holding``: nothing dropped
    yet; it goes the way of an Undo) — and a purge under way stops (what it
    hasn't deleted stays, for the new scan)."""
    if path in _holding:
        store = _store(store)
        t = _tasks.pop(path, None)
        if t is not None and not t.done():
            t.cancel()
        _holding.discard(path)
        _purging.discard(path)
        reg = dict(registry(store))
        reg.pop(path, None)
        _save(store, reg)
        try:
            _hand_back_moves(store, path, _moved.pop(path, None))
        finally:
            _refresh(store)
            _rebalance_later(store)
        log.info("Removal of %s dropped: the folder was added again", path)
    elif path in _purging:
        _readded.add(path)
    else:
        undo(path, store)


def tasks() -> list:
    """The finishing tasks, a ``begin`` handing over, and the hand-overs
    after an Undo — stopped with the other store writers at shutdown.  The
    registry finishes the waiting and purging removals at the next start; a
    ``begin`` stopped while handing over drops its removal (it hadn't begun)."""
    return [t for t in (*_tasks.values(), *_side) if not t.done()]


_NEIGHBOUR_POLL_S = 0.25


def _undoable_neighbour(store, path: str) -> bool:
    """Is a local root above or below ``path`` still waiting to be removed —
    one an Undo may list again?  (A removal that failed or has no task left
    can't move on: it doesn't hold the purge up.)"""
    from soniqboom.core.filesource import is_remote_path
    if is_remote_path(path):
        return False
    own = path.rstrip("/") + "/"
    for p, rec in registry(store).items():
        if (p == path or p in _purging or is_remote_path(p)
                or (isinstance(rec, dict) and rec.get("error"))
                or not (p in _tasks or p in _attempt)):
            continue
        other = p.rstrip("/") + "/"
        if own.startswith(other) or other.startswith(own):
            return True
    return False


async def _finish(path: str, delay: float) -> None:
    if delay > 0:
        await asyncio.sleep(delay)
    store = _store()
    rec = registry(store).get(path)
    if rec is None:
        return
    _purging.add(path)                          # (no Undo from here)
    _readded.discard(path)
    moved: dict[str, str] = {}                  # (handed over: back if it is added again)
    try:
        # A root above or below this one that an Undo may still list again
        # would cover tracks of it: wait until each is undone or past its own
        # undo window (a root being removed covers nothing).  Adding the
        # folder again meanwhile drops the removal (``cancel``).
        _holding.add(path)
        try:
            while _undoable_neighbour(store, path):
                await asyncio.sleep(_NEIGHBOUR_POLL_S)
        finally:
            _holding.discard(path)
        moved = _moved.pop(path, None) or {}    # (no Undo from here: this removal keeps it)
        share_id = rec.get("share_id")
        if share_id:
            await _drop_share(path, share_id)
        else:
            await _drop_folder(path, moved)
        _refresh(store)                         # (what the drop handed over: listed)
        from soniqboom.core.data import delete_scan_dir, delete_tracks_by_scan_root
        n = await delete_tracks_by_scan_root(path, stop=lambda: path in _readded,
                                             moved=moved)
        if path not in _readded:
            await delete_scan_dir(path)         # (a scan that ended meanwhile listed it again)
        log.info("Purged %d tracks from %s", n, path)
    except asyncio.CancelledError:
        raise                                   # (kept: the next start finishes it)
    except Exception as exc:                    # noqa: BLE001
        log.exception("Removing %s failed", path)
        reg = dict(registry(store))
        if path in reg:
            reg[path] = {**reg[path], "error": f"{type(exc).__name__}: {exc}"}
            _save(store, reg)                   # (shown; tried again at the next start)
        _refresh(store)
        return
    finally:
        _purging.discard(path)
    if path in _readded:                        # (added again: what it handed over is its own)
        try:
            _hand_back_moves(store, path, moved)
        except Exception:                       # noqa: BLE001 — its scan tags them again
            log.exception("Handing %s's tracks back failed", path)
        log.info("Removal of %s stopped: the folder was added again", path)
    _readded.discard(path)
    reg = dict(registry(store))
    if reg.pop(path, None) is not None:
        _save(store, reg)
        _refresh(store)


async def _drop_folder(path: str, moved: "dict[str, str] | None" = None) -> None:
    """What removing a music folder changes: its scan-dir entry and alias,
    queued scans of it, the watcher (and a remote root's freshness poll) —
    then its tracks another root covers are handed over (``moved`` collects
    them).  The hand-over comes last and stops if the folder is added again:
    the re-add's own set-up then stays, and ``_finish`` hands its tracks
    back."""
    from soniqboom.config import load_local_conf, save_local_conf, settings
    from soniqboom.core import watcher
    from soniqboom.core.data import hand_over_covered
    from soniqboom.core.filesource import is_remote_path
    from soniqboom.core.scanner import forget_root
    store = _store()
    store.delete_scan_dir(path)                 # (no await before the teardown below)
    conf = load_local_conf()
    aliases = conf.get("folder_aliases", {})
    if path in aliases:
        del aliases[path]
        conf["folder_aliases"] = aliases
        settings.folder_aliases = dict(save_local_conf(conf).get("folder_aliases") or {})
    try:
        forget_root(path)
        await watcher.remove_root(path)
    except Exception:                                   # noqa: BLE001
        log.exception("watcher.remove_root failed for %s", path)
    if is_remote_path(path):
        try:
            from soniqboom.core import remote_freshness
            await remote_freshness.remove_share(path)
        except Exception:                               # noqa: BLE001
            log.exception("freshness.remove_share failed for %s", path)
    # (a root still in its undo window takes what it covers: undone, it keeps
    #  it; removed after all, its own purge decides — a removal begun after
    #  this one's wait for its neighbours must not lose those tracks)
    await hand_over_covered(store, path, moved=moved,
                            keep_going=lambda: path not in _readded)


async def _drop_share(path: str, share_id: str) -> None:
    """What disconnecting a network share changes: its source, freshness
    poll, scan-dir entry and cached files, then its config entry and alias
    (read and written in one go, after the awaits — a save meanwhile is
    kept)."""
    from soniqboom.config import load_local_conf, save_local_conf, settings
    from soniqboom.core.data import delete_scan_dir
    from soniqboom.core.filesource import remove_source
    remove_source(path)
    try:
        from soniqboom.core import remote_freshness
        await remote_freshness.remove_share(path)
    except Exception:                                   # noqa: BLE001
        log.exception("freshness.remove_share failed for %s", path)
    await delete_scan_dir(path)
    try:
        from soniqboom.core.remote_cache import get_cache
        get_cache().invalidate_share(path)
    except Exception:                                   # noqa: BLE001
        pass                                            # (the cache may not be set up)
    conf = load_local_conf()
    shares = conf.get("network_shares", {})
    shares.pop(share_id, None)
    aliases = conf.get("folder_aliases", {})
    aliases.pop(path, None)
    conf["folder_aliases"] = aliases
    conf["network_shares"] = shares
    settings.folder_aliases = dict(save_local_conf(conf).get("folder_aliases") or {})


def status(store=None) -> list[dict]:
    """The admin list's ``removing`` rows: ``path``, ``alias``, ``share``,
    ``state`` (``waiting`` — undo possible for ``undo_s`` more seconds —,
    ``removing``, or ``failed``: ``error``, tried again at the next start),
    ``total`` and ``left`` (tracks still in the library)."""
    store = _store(store)
    from soniqboom.config import settings
    from soniqboom.core.data import path_hash
    now = time.time()
    rows = []
    for path, rec in registry(store).items():
        waiting = path not in _purging and (path in _tasks or path in _attempt)
        left = len(store.get_track_ids_for_scan_root(path_hash(path)))
        rows.append({
            "path": path,
            "alias": (getattr(settings, "folder_aliases", None) or {}).get(path),
            "share": bool(rec.get("share_id")),
            "state": ("failed" if rec.get("error") and path not in _tasks
                      else "waiting" if waiting else "removing"),
            "error": rec.get("error"),
            "undo_s": (UNDO_S if path in _attempt          # (its window starts after the hand-over)
                       else max(0.0, round(UNDO_S - (now - float(rec.get("at") or 0)), 1)))
                      if waiting else 0.0,
            "total": int(rec.get("total") or 0),
            "left": left,
        })
    rows.sort(key=lambda r: r["path"])
    return rows


def resume(store=None) -> None:
    """At startup: hide what is being removed and finish it (no undo)."""
    store = _store(store)
    if not registry(store):
        return
    _ids.clear()
    _refresh(store)
    for path in registry(store):
        log.info("Finishing the removal of %s", path)
        _spawn(path, 0.0)


def reset_for_tests() -> None:
    global _hidden, _ver, _seen_seq
    for t in (*_tasks.values(), *_side):
        if not t.done():
            t.cancel()
    _tasks.clear()
    _side.clear()
    _ids.clear()
    _purging.clear()
    _readded.clear()
    _moved.clear()
    _attempt.clear()
    _holding.clear()
    _hidden, _ver, _seen_seq = None, 0, None
