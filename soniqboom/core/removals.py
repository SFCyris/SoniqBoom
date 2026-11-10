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
changed); then the root is dropped as before (the scan-dir entry, its alias,
the watcher and queued scans; a share's config entry, source, freshness poll
and cached files) and its tracks are purged in slices
(``data.delete_tracks_by_scan_root``).  The registry is store config
(``KEY``): a restart (or a shutdown mid-way) finishes what was waiting or
purging, without a new undo window; a step that failed is kept, shown, and
tried again at the next start.  Adding the root again cancels a waiting
removal (``cancel``) and stops a purge under way.

The removal itself doesn't read the root's files (an unreachable mount or
share is the usual reason to remove one); a share's source is closed as
before."""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

KEY = "removing_roots"
UNDO_S = 10.0

_ids: dict[str, frozenset] = {}         # root → its track ids when the removal began
_hidden: frozenset | None = None        # the union of ``_ids`` (None: nothing removed)
_ver = 0
_tasks: dict[str, asyncio.Task] = {}    # root → its finishing task
_purging: set[str] = set()
_readded: set[str] = set()              # added again while purging: the purge stops
_seen_seq = None                        # the store's ``_catalog_seq`` ``_ids`` last followed


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
    """Rebuild the hidden union after the registry changed, and tell the
    lists (availability token) and the Subsonic catalogue (its sequence)."""
    reg = registry(store)
    for gone in [p for p in _ids if p not in reg]:
        _ids.pop(gone, None)
    from soniqboom.core.data import path_hash
    for p in reg:
        if p not in _ids:
            _ids[p] = frozenset(store.get_track_ids_for_scan_root(path_hash(p)))
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
    """Start removing ``path`` (a share's scan root with ``share_id``): hidden
    now, dropped and purged after ``UNDO_S``."""
    store = _store(store)
    from soniqboom.core.data import path_hash
    reg = dict(registry(store))
    if path in reg:
        return reg[path]                        # (already being removed)
    reg[path] = {"share_id": share_id, "at": time.time(),
                 "total": len(store.get_track_ids_for_scan_root(path_hash(path)))}
    _save(store, reg)
    _ids.pop(path, None)
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
    """Drop a removal still waiting (False: none, or it has started)."""
    store = _store(store)
    reg = dict(registry(store))
    if path not in reg or path in _purging:
        return False
    t = _tasks.pop(path, None)
    if t is not None and not t.done():
        t.cancel()
    reg.pop(path, None)
    _save(store, reg)
    _refresh(store)
    log.info("Removal of %s undone", path)
    return True


def cancel(path: str, store=None) -> None:
    """``path`` is being added again: a waiting removal is dropped, a purge
    under way stops (what it hasn't deleted stays, for the new scan)."""
    if path in _purging:
        _readded.add(path)
    else:
        undo(path, store)


def tasks() -> list:
    """The finishing tasks (stopped with the other store writers at
    shutdown; the registry finishes them at the next start)."""
    return [t for t in _tasks.values() if not t.done()]


async def _finish(path: str, delay: float) -> None:
    if delay > 0:
        await asyncio.sleep(delay)
    store = _store()
    rec = registry(store).get(path)
    if rec is None:
        return
    _purging.add(path)
    _readded.discard(path)
    try:
        share_id = rec.get("share_id")
        if share_id:
            await _drop_share(path, share_id)
        else:
            await _drop_folder(path)
        from soniqboom.core.data import delete_scan_dir, delete_tracks_by_scan_root
        n = await delete_tracks_by_scan_root(path, stop=lambda: path in _readded)
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
        return
    finally:
        _purging.discard(path)
    _readded.discard(path)
    reg = dict(registry(store))
    if reg.pop(path, None) is not None:
        _save(store, reg)
        _refresh(store)


async def _drop_folder(path: str) -> None:
    """What removing a music folder changes: its scan-dir entry and alias,
    queued scans of it, the watcher (and a remote root's freshness poll)."""
    from soniqboom.config import load_local_conf, save_local_conf, settings
    from soniqboom.core import watcher
    from soniqboom.core.data import delete_scan_dir
    from soniqboom.core.filesource import is_remote_path
    from soniqboom.core.scanner import forget_root
    await delete_scan_dir(path)
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
        waiting = path not in _purging and path in _tasks
        left = len(store.get_track_ids_for_scan_root(path_hash(path)))
        rows.append({
            "path": path,
            "alias": (getattr(settings, "folder_aliases", None) or {}).get(path),
            "share": bool(rec.get("share_id")),
            "state": ("failed" if rec.get("error") and path not in _tasks
                      else "waiting" if waiting else "removing"),
            "error": rec.get("error"),
            "undo_s": max(0.0, round(UNDO_S - (now - float(rec.get("at") or 0)), 1))
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
    for t in _tasks.values():
        if not t.done():
            t.cancel()
    _tasks.clear()
    _ids.clear()
    _purging.clear()
    _readded.clear()
    _hidden, _ver, _seen_seq = None, 0, None
