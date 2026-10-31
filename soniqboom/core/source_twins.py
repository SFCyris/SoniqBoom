# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Online-collection tunes that are files of the library itself ("twins").

An online collection (``core/catalogue.py`` — Modland, HVSC, scene.org,
Aminet) can list a file the library already holds — the same bytes in a local
folder or on a network share.  Such a tune is the library file's *twin*: the
duplicate grouping (``core/duplicates.py``) puts it in that file's group, its
primary pick ranks online collections last (so the library's own copy is the
primary, and the twin is hidden wherever duplicate copies are hidden), and a
play of the twin plays the library's copy (``stream.stream_track``) — nothing
is fetched from the collection.

A twin is matched exactly, never by name:

* Modland — the library file's ``scene_path`` (its place in Modland, from the
  checksum list, ``core/scene_metadata.py``) names the Modland tune;
* HVSC — the library SID's ``sid_md5`` names an HVSC path
  (``core/hvsc.path_for_md5``), whose online HVSC tune is the twin;
* any collection — a tune whose ``file_md5`` (known once it has been played,
  ``merge_after_play``) is a library file's ``file_md5``.

``refresh`` recomputes the map (after a scan, a collection build or refresh,
a played tune's merge, and at start) and marks every library file whose twins
changed — and every tune that stopped being a twin — for the incremental
duplicate re-grouping (``TrackStore._note_dup_dirty``).  The map lives in
memory; the annotations it leads to are stored with the tracks.  Before a
duplicate re-grouping it is recomputed only when it may have changed
(``refresh_async(if_stale=True)``): a write that may change it moves the
store's ``_twin_gen`` (``TrackStore._twin_touch``), and the collections and the
HVSC checksums it read are compared (``_inputs``); at the latest every
``_MAX_AGE_S``.
"""
from __future__ import annotations

import logging
import threading
import time

from soniqboom.core import catalogue as _catalogue

log = logging.getLogger(__name__)

_lock = threading.Lock()
_twin_of: dict[str, str] = {}             # collection tune id → library track id
_twins: dict[str, list[str]] = {}         # library track id → collection tune ids
# What the map in use was computed from: (the store's ``_twin_gen`` before the
# pass, ``_inputs``, ``time.monotonic()`` after it) — None before the first.
_last: "tuple | None" = None
_MAX_AGE_S = 600.0                        # recomputed at the latest this long after


def own_of(tid: str) -> "str | None":
    """The library track a collection tune is the twin of, or None."""
    return _twin_of.get(tid)


def twins_of(tid: str) -> "list[str]":
    """The collection tunes that are twins of library track ``tid``."""
    return list(_twins.get(tid, ()))


def _collection_ids(store) -> "tuple":
    """(Modland ``track_id``, the online HVSC collection or None) — for the
    collections that are present."""
    from soniqboom.core import modland_catalog
    hv = None
    try:
        from soniqboom.core.online_hvsc import HVSC_ONLINE as hv
        if not hv.root or not store._tag_scan_root_hash.get(hv.root_hash()):
            hv = None
    except Exception:                       # noqa: BLE001 — no HVSC collection
        hv = None
    return modland_catalog.track_id, hv


def compute(store) -> "dict[str, str]":
    """Collection tune id → library track id, for every twin in ``store``
    (blocking: one pass over the library).  Of several library copies of
    one file, the one ``duplicates.primary_sort_key`` puts first."""
    from soniqboom.core.duplicates import primary_sort_key
    modland_id, hvsc = _collection_ids(store)
    hv_db = None
    if hvsc is not None:
        try:
            from soniqboom.core.hvsc import get_hvsc
            hv_db = get_hvsc()
        except Exception:                   # noqa: BLE001 — no HVSC song lengths
            hv_db = None
    tracks = store._tracks
    best: dict[str, dict] = {}               # collection id → best library track
    own_md5: dict[str, list[dict]] = {}
    played_tunes: list[dict] = []
    for t in list(tracks.values()):
        path = t.get("path")
        if _catalogue.is_catalogue_path(path):
            if t.get("file_md5"):
                played_tunes.append(t)
            continue
        cands = []
        sp = t.get("scene_path")
        if sp:
            cands.append(modland_id(sp))
        sm = t.get("sid_md5")
        if sm and hv_db is not None:
            try:
                rel = hv_db.path_for_md5(sm)
            except Exception:               # noqa: BLE001
                rel = None
            if rel:
                cands.append(hvsc.track_id(rel.lstrip("/")))
        fm = t.get("file_md5")
        if fm:
            own_md5.setdefault(fm, []).append(t)
        for cid in cands:
            if cid in tracks:
                cur = best.get(cid)
                if cur is None or primary_sort_key(t) < primary_sort_key(cur):
                    best[cid] = t
    for tune in played_tunes:
        for t in own_md5.get(tune["file_md5"], ()):
            cur = best.get(tune["id"])
            if cur is None or primary_sort_key(t) < primary_sort_key(cur):
                best[tune["id"]] = t
    return {cid: t["id"] for cid, t in best.items()}


def apply(store, new: "dict[str, str]") -> int:
    """Take ``new`` (``compute``'s map) as the twins, and mark for the
    duplicate re-grouping every tune that became, changed or stopped being a
    twin — and a twin whose stored annotation was lost (a first-play merge
    rewrote its row) — with the library files involved.  On the loop (it
    writes the store's pending set).  Returns how many tunes were marked."""
    with _lock:
        old = dict(_twin_of)
        _twin_of.clear()
        _twin_of.update(new)
        _twins.clear()
        for cid, oid in new.items():
            _twins.setdefault(oid, []).append(cid)
    marked = {cid for cid in set(old) | set(new) if old.get(cid) != new.get(cid)}
    marked |= {cid for cid in new
               if (store._tracks.get(cid) or {}).get("is_duplicate_primary") is not False}
    for cid in marked:
        # The tune itself (its annotation follows or is reset) and the
        # library files it was / is the twin of (their groups take it in or
        # let it go).
        store._note_dup_dirty(cid, store._tracks.get(cid), twin=True)
        for oid in {old.get(cid), new.get(cid)} - {None}:
            t = store._tracks.get(oid)
            if t is not None:
                store._note_dup_dirty(oid, t)
    if marked:
        log.info("Collection twins: %d tune(s) of the online collections are files "
                 "of the library (%d to re-group)", len(new), len(marked))
    return len(marked)


def _inputs(store) -> tuple:
    """What ``compute`` reads besides the tracks: the registered collections
    (``catalogue.generation``), the HVSC checksum table (the object itself —
    a reload makes a new one — and whether it is loaded) and whether the
    online HVSC collection is indexed."""
    hv_db = hv_root = indexed = None
    try:
        from soniqboom.core.hvsc import get_hvsc
        db = get_hvsc()
        hv_db = (db.docs_path, db._md5_paths, db._loaded)
    except Exception:                       # noqa: BLE001 — no HVSC song lengths
        pass
    try:
        from soniqboom.core.online_hvsc import HVSC_ONLINE as hv
        hv_root = hv.root
        indexed = bool(hv.root and store._tag_scan_root_hash.get(hv.root_hash()))
    except Exception:                       # noqa: BLE001 — no HVSC collection
        pass
    return (_catalogue.generation(), hv_db, hv_root, indexed)


def _same_inputs(a: tuple, b: tuple) -> bool:
    # (the HVSC table by identity: two tables of one size can differ)
    return (a[0] == b[0] and a[2:] == b[2:]
            and (a[1] is None) == (b[1] is None)
            and (a[1] is None or (a[1][0] == b[1][0] and a[1][1] is b[1][1]
                                  and a[1][2] == b[1][2])))


def _stale(store) -> bool:
    """May the map in use differ from a ``compute`` now?"""
    last = _last
    return (last is None or last[0] != store._twin_gen
            or not _same_inputs(last[1], _inputs(store))
            or time.monotonic() - last[2] >= _MAX_AGE_S)


def _remember(store, gen: int, inputs: tuple) -> None:
    global _last
    _last = (gen, inputs, time.monotonic())


def refresh(store=None) -> int:
    """``compute`` + ``apply`` (blocking, on the loop's thread — tests)."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    store._twin_told = False
    gen, inputs = store._twin_gen, _inputs(store)
    n = apply(store, compute(store))
    _remember(store, gen, inputs)
    return n


async def refresh_async(store=None, *, regroup: bool = True, if_stale: bool = False) -> int:
    """``compute`` in a worker thread, ``apply`` on the loop, then (with
    ``regroup``) the duplicate re-grouping of what was marked, run as for
    any other write.  With ``if_stale`` nothing is done while the map in use
    still holds (``_stale``)."""
    import asyncio
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    if if_stale and not _stale(store):
        return 0
    store._twin_told = False                # (the next write that moves it asks again)
    # (taken before the pass: a write or a collection / HVSC change during it
    # leaves the map stale)
    gen, inputs = store._twin_gen, _inputs(store)
    try:
        new = await asyncio.to_thread(compute, store)
    except Exception:                       # noqa: BLE001 — never fail the caller
        log.warning("Collection twins: computing failed", exc_info=True)
        return 0
    n = apply(store, new)
    _remember(store, gen, inputs)
    if n and regroup:
        try:
            from soniqboom.core.scanner import _ensure_dup_runner
            _ensure_dup_runner()
        except Exception:                   # noqa: BLE001
            log.debug("duplicate runner not started", exc_info=True)
    return n


# Debounced refresh: collection jobs and first-play merges ask for one; it runs
# ``_REFRESH_DELAY_S`` after the last ask, on the loop ``install`` was given.
_REFRESH_DELAY_S = 5.0
_loop = None
_timer = None


def install(loop) -> None:
    """Called at start, on the server's loop: refresh once (soon)."""
    global _loop
    _loop = loop
    schedule()


def schedule(delay: float = _REFRESH_DELAY_S) -> None:
    """Ask for a refresh — from the loop or any thread; asks within
    ``delay`` of each other make one."""
    loop = _loop
    if loop is None or loop.is_closed():
        return

    def _arm():
        global _timer
        if _timer is not None:
            _timer.cancel()
        _timer = loop.call_later(delay, lambda: loop.create_task(refresh_async()))
    import asyncio
    try:
        if asyncio.get_running_loop() is loop:
            _arm()
            return
    except RuntimeError:
        pass
    loop.call_soon_threadsafe(_arm)


def reset_for_tests() -> None:
    global _last
    with _lock:
        _twin_of.clear()
        _twins.clear()
    _last = None
