# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tracks that can't play right now, and what the web UI does with them.

A track can't play right now when its scan root is unreachable — the root's
``status`` is ``unavailable`` (an ejected drive, a dropped mount, a share the
health monitor lost, a catalogue whose archive can't be reached) or it is a
network share with no connected source (``filesource.get_source`` is None:
never connected, a refused sign-in backing off, a catalogue holding itself
offline) — and no local copy can serve it:

* a remote file whose bytes sit in the remote cache (a fetched catalogue tune
  included),
* a member of a remote archive whose whole archive, or a subset built for
  that member, is cached.

A local file on an offline drive never plays — local archive members neither
(their extract is only reused while the archive itself can be checked).  This
follows ``api/stream._resolve_play_source`` for the common cases; known
differences: a share the health monitor marked unavailable while its source
is still registered counts as offline here although a play may still reach
it, and a cached multi-file remote tune (TFMX, an AdLib tune with its bank)
counts as playable although a play without the share lacks its companion
files.

The setting "Tracks on unavailable drives" (store config ``offline_tracks``,
server-wide like "Hide duplicates in search") decides what the web UI does:

* ``mark`` (default) — rows are listed with ``"offline": <kind>``
  (``drive`` / ``share`` / ``catalogue``) so the UI can dim them and badge
  them; auto-play (shuffle deals, radio / instant mix, similar tracks, the
  next track) skips them.
* ``hide`` — searches and track lists leave them out (totals and paging
  count only the rest, group counts too); playlists, folder views and the
  duplicates view still list them, marked.  Auto-play skips them.
* ``show`` — everything as before: no flag, nothing left out, nothing
  skipped.

With "Hide duplicates" on (``filter_duplicates``), ``mark`` and ``hide`` list a
duplicate group whose primary can't play right now — while another member
can — by that member instead: the best one that can play, by the order the
primary was picked by (``primary_swap`` → ``TrackStore.primary_swap``).  The
lists, counts, group counts and the player's pages and shuffles all use it,
and nothing is written: once the primary's drive is back, the primary is
listed again.  A shuffle already dealt keeps its order — the member takes its
primary's place in it and gives it back (``api/tracks.shuffled_tracks``).  A
group none of whose members can play keeps its primary (flagged / hidden).

Cost: ``snapshot()`` is O(number of scan roots) per call — the dead roots and
a signature of what could change their tracks' state (each dead root's track
count, the remote cache's change counter for a dead share).  Only when that
signature changes (a root goes offline / comes back, a cached copy of an
offline share's file is added or evicted) are the cached copies looked up
again; the id set a Hide filter subtracts is built lazily, once per snapshot.
The duplicate swap is built once per snapshot and track write, from the
unplayable ids or the duplicate groups (whichever are fewer).
A root whose ``status`` flips calls the store's ``_on_scan_dirs_changed`` hook
→ ``notify_changed`` → the web clients get an ``availability_changed`` event.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

MODE_KEY = "offline_tracks"
MODES = ("mark", "hide", "show")
DEFAULT_MODE = "mark"

KIND_DRIVE = "drive"
KIND_SHARE = "share"
KIND_CATALOGUE = "catalogue"

# A Playable page (``playable_page``) reads at most this many list rows per
# request before it hands back what it found (with the position to go on
# from) — a long run of offline tracks is crossed in several cheap requests,
# never one huge one.
PLAYABLE_SCAN_BUDGET = 5000

_ARCHIVE_SUFFIXES = (".zip", ".lha", ".lzh")


def normalize_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in MODES else DEFAULT_MODE


def mode(store=None) -> str:
    """The setting: ``mark`` / ``hide`` / ``show``."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    try:
        return normalize_mode(store.get_config(MODE_KEY, DEFAULT_MODE))
    except Exception:                                   # noqa: BLE001
        return DEFAULT_MODE


def _root_kind(path: str) -> str:
    from soniqboom.core import catalogue
    from soniqboom.core.filesource import is_remote_path
    if catalogue.is_catalogue_root(path):
        return KIND_CATALOGUE
    return KIND_SHARE if is_remote_path(path) else KIND_DRIVE


def _dead_roots(store) -> dict[str, tuple[str, str]]:
    """``{root path_hash: (root path, kind)}`` of the scan roots whose files
    can't be read right now.  Stored state and the source registry only —
    never touches a file system (a stalled mount can't block a request)."""
    sds = getattr(store, "_scan_dirs", None)
    if not isinstance(sds, dict) or not sds:
        return {}
    import hashlib
    from soniqboom.core.filesource import get_source, is_remote_path
    dead: dict[str, tuple[str, str]] = {}
    for path, sd in list(sds.items()):
        if not isinstance(sd, dict) or not isinstance(path, str) or not path:
            continue
        down = sd.get("status") == "unavailable"
        if not down and is_remote_path(path):
            try:
                down = get_source(path) is None   # a remote root's path IS its source key
            except Exception:                     # noqa: BLE001
                down = False
        if down:
            ph = sd.get("path_hash") or hashlib.sha256(path.encode()).hexdigest()[:16]
            dead[ph] = (path, _root_kind(path))
    return dead


class Snapshot:
    """Which roots are offline right now and which of their tracks still play
    from a cached copy.  Immutable once built; ``unavailable`` is built on
    first use."""

    __slots__ = ("seq", "dead", "paths", "ok", "_unavailable", "_store", "_lock", "_roots")

    def __init__(self, seq: int, dead: dict[str, str], paths: dict[str, str],
                 ok: frozenset, store) -> None:
        self.seq = seq
        self.dead = dead            # root hash → kind
        self.paths = paths          # root hash → root path
        self.ok = ok                # ids under dead roots that still play
        self._unavailable: frozenset | None = None
        self._store = store
        self._lock = threading.Lock()
        self._roots: list | None = None          # ``state()``'s per-root rows, built once

    def __bool__(self) -> bool:
        return bool(self.dead)

    def kind_of(self, row) -> str | None:
        """``drive`` / ``share`` / ``catalogue`` when the track ``row`` (a
        track dict) can't play right now, else None.  O(1)."""
        if not self.dead or not row:
            return None
        try:
            kind = self.dead.get(row.get("scan_root_hash"))
        except AttributeError:
            return None
        if kind is None or row.get("id") in self.ok:
            return None
        return kind

    @property
    def unavailable(self) -> frozenset:
        """Every track id that can't play right now (built once)."""
        u = self._unavailable
        if u is not None:
            return u
        with self._lock:
            if self._unavailable is None:
                idx = getattr(self._store, "_tag_scan_root_hash", {}) or {}
                parts = []
                for h in self.dead:
                    ids = idx.get(h)
                    if ids:
                        parts.append(ids - self.ok if self.ok else ids)
                if not parts:
                    self._unavailable = frozenset()
                elif len(parts) == 1:
                    self._unavailable = frozenset(parts[0])
                else:
                    self._unavailable = frozenset().union(*parts)
            return self._unavailable


_EMPTY_OK: frozenset = frozenset()
_state_lock = threading.Lock()
_seq = 0
_memo: dict[str, Any] = {"store": None, "sig": None, "snap": None}
# Per dead root: (store id, root size, [(id, archive rel, first member)]) — the
# archive members of a root, reused while its track count stays put (the
# cached copies change far more often than a root's archive list).
_members_memo: dict[str, tuple[int, int, list]] = {}
# A big root's member list is built in a worker thread when asked for on the
# event loop (a pass over every track of a 300,000-track root holds the loop for
# ~0.3 s): the snapshot meanwhile counts that root's archive members as
# unavailable, and the finished list (a new ``_members_gen``) re-snapshots and
# tells the clients.
_MEMBERS_SYNC_MAX = 20_000
_members_jobs: dict[str, Any] = {}
_members_gen = 0


def _cache_gen(path: str):
    try:
        from soniqboom.core.remote_cache import get_cache
        return get_cache().generation(path)
    except Exception:                                   # noqa: BLE001
        return None


def _signature(store, dead: dict[str, tuple[str, str]]) -> tuple:
    idx = getattr(store, "_tag_scan_root_hash", {}) or {}
    items = []
    for h, (path, kind) in sorted(dead.items()):
        n = len(idx.get(h) or ())
        # The root's write version too: an add + a delete leave ``n`` alone.
        rev = store.root_rev(h) if hasattr(store, "root_rev") else 0
        gen = _cache_gen(path) if kind != KIND_DRIVE else None
        items.append((h, path, kind, n, rev, gen))
    return (id(store), _members_gen, tuple(items))


def _scan_members(store, root: str, ids: list) -> list:
    """``[(id, archive rel, first member)]`` of the archive members among
    ``ids`` (a root's track ids).  Reads only — safe in a worker thread."""
    tracks = store._tracks
    cut = len(root) + 1
    out = []
    for tid in ids:
        t = tracks.get(tid)
        p = (t.get("path") if t else None) or ""
        if "::" not in p:
            continue
        zip_rel, _sep, rest = p[cut:].partition("::")
        out.append((tid, zip_rel, rest.split("::", 1)[0]))
    return out


def _archive_members(store, h: str, root: str, root_ids) -> list | None:
    """The archive members of a root (memoised per root size), or None while
    a big root's list is still being built off the event loop."""
    n = len(root_ids)
    hit = _members_memo.get(h)
    if hit is not None and hit[0] == id(store) and hit[1] == n:
        return hit[2]
    loop = _running_loop()
    if loop is None or n <= _MEMBERS_SYNC_MAX:
        out = _scan_members(store, root, list(root_ids))
        _members_memo[h] = (id(store), n, out)
        return out
    job = _members_jobs.get(h)
    if job is None or job[0] != (id(store), n):
        ids = list(root_ids)
        fut = loop.run_in_executor(None, _scan_members, store, root, ids)
        _members_jobs[h] = ((id(store), n), fut)

        def _done(f, key=(id(store), n)) -> None:
            global _members_gen
            if _members_jobs.get(h, (None,))[0] == key:
                _members_jobs.pop(h, None)
            try:
                out = f.result()
            except BaseException:                       # noqa: BLE001 — cancelled / failed: ask again later
                return
            _members_memo[h] = (key[0], key[1], out)
            _members_gen += 1
            notify_changed()
        fut.add_done_callback(_done)
    return None


def _cached_ok(store, h: str, root: str, root_ids) -> set[str]:
    """The ids under the offline remote root ``root`` that still play from
    the remote cache."""
    from soniqboom.core.remote_cache import _subset_parts, get_cache
    try:
        names = get_cache().names_for_share(root)
    except Exception:                                   # noqa: BLE001
        return set()
    if not names or not root_ids:
        return set()
    whole: set[str] = set()
    subsets: dict[str, set[str]] = {}
    for name in names:
        parts = _subset_parts(name)
        if parts is not None:
            subsets.setdefault(parts[0], set()).add(parts[1])
        elif name:
            whole.add(name)
    ok: set[str] = set()
    if whole:
        if len(whole) * 4 <= len(root_ids):
            # Few cached files: their track ids (uuid5 of "<root>:<path>").
            for name in whole:
                tid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{root}:{name}"))
                if tid in root_ids:
                    ok.add(tid)
        else:
            tracks = store._tracks
            cut = len(root) + 1
            for tid in list(root_ids):
                t = tracks.get(tid)
                if t is not None and (t.get("path") or "")[cut:] in whole:
                    ok.add(tid)
    if subsets or any(n.lower().endswith(_ARCHIVE_SUFFIXES) for n in whole):
        for tid, zip_rel, first in _archive_members(store, h, root, root_ids) or ():
            if zip_rel in whole or first in subsets.get(zip_rel, ()):
                ok.add(tid)
    return ok


def snapshot(store=None) -> Snapshot:
    """The current availability state (memoised; see the module docstring)."""
    global _seq
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    dead = _dead_roots(store)
    sig = _signature(store, dead)
    with _state_lock:
        if _memo["sig"] == sig and _memo["store"] is store and _memo["snap"] is not None:
            return _memo["snap"]
    idx = getattr(store, "_tag_scan_root_hash", {}) or {}
    ok: set[str] = set()
    for h, (path, kind) in dead.items():
        if kind == KIND_DRIVE:
            continue
        root_ids = idx.get(h) or set()
        try:
            ok |= _cached_ok(store, h, path, root_ids)
        except Exception:                               # noqa: BLE001 — never fail a list
            log.debug("cached-copy lookup failed for %s", path, exc_info=True)
    with _state_lock:
        if _memo["sig"] == sig and _memo["store"] is store and _memo["snap"] is not None:
            return _memo["snap"]
        _seq += 1
        snap = Snapshot(_seq, {h: k for h, (_p, k) in dead.items()},
                        {h: p for h, (p, _k) in dead.items()},
                        frozenset(ok) if ok else _EMPTY_OK, store)
        _memo.update(store=store, sig=sig, snap=snap)
        for gone in [h for h in _members_memo if h not in dead]:
            _members_memo.pop(gone, None)
    return snap


# Part of every token: a client still holding one from before a restart must
# not take the new process's first generation for the one it knows.
_BOOT = uuid.uuid4().hex[:8]


def token(store=None) -> str:
    """What a web client compares to know its rows are stale: the setting and
    the snapshot generation (of this server process)."""
    return f"{mode(store)}:{_BOOT}:{snapshot(store).seq}"


# ── What the list endpoints use ──────────────────────────────────────────────

_last_interest = 0.0           # (monotonic) the last web request that used the state
INTEREST_S = 600               # local roots are re-probed while used this recently


def _touch() -> None:
    global _last_interest
    _last_interest = time.monotonic()


def hidden(store=None) -> frozenset | None:
    """The ids a searched / listed result leaves out: the tracks that can't
    play right now in ``hide`` mode, else None."""
    _touch()
    if mode(store) != "hide":
        return None
    u = snapshot(store).unavailable
    return u or None


def unplayable(store=None) -> frozenset | None:
    """The ids auto-play skips (shuffle deals, radio, similar tracks): the
    tracks that can't play right now in ``mark`` / ``hide`` mode, else None."""
    _touch()
    if mode(store) == "show":
        return None
    u = snapshot(store).unavailable
    return u or None


def hide_token(store=None):
    """Part of every cache key of a list / count / aggregate that ``hide``
    mode changes: the snapshot generation while tracks are hidden, else None."""
    if mode(store) != "hide":
        return None
    snap = snapshot(store)
    return snap.seq if snap and snap.unavailable else None


def primary_swap(store=None, dedup: bool | None = None):
    """The duplicate groups a deduped list shows by another member while
    their primary can't play (a ``store.PrimarySwap`` —
    ``TrackStore.primary_swap``), in ``mark`` / ``hide`` mode; else None.
    ``dedup``: whether the caller's list hides duplicate copies — None reads
    the web lists' "Hide duplicates" setting (``filter_duplicates``)."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    if dedup is None:
        try:
            dedup = bool(store.get_config("filter_duplicates", False))
        except Exception:                               # noqa: BLE001
            dedup = False
    if not dedup or mode(store) == "show":
        return None
    u = snapshot(store).unavailable
    return store.primary_swap(u) if u else None


def swap_token(store=None):
    """Part of every cache key of a deduped count / aggregate that the swap
    changes (``primary_swap``; Mark mode too, where ``hide_token`` is None):
    the snapshot generation while a swap applies, else None."""
    if primary_swap(store) is None:
        return None
    return snapshot(store).seq


def mark_rows(rows, store=None, snap: Snapshot | None = None) -> list:
    """``rows`` (track dicts) with ``"offline": <kind>`` added to the ones
    that can't play right now — copies, never the caller's / store's dicts.
    Nothing is added in ``show`` mode or while every root is reachable."""
    _touch()
    if mode(store) == "show":
        return rows if isinstance(rows, list) else list(rows)
    snap = snap or snapshot(store)
    if not snap.dead:
        return rows if isinstance(rows, list) else list(rows)
    out = []
    for r in rows:
        k = snap.kind_of(r) if isinstance(r, dict) else None
        out.append({**r, "offline": k} if k else r)
    return out


def offline_kinds(ids: Iterable[str], store=None) -> dict[str, str]:
    """``{id: kind}`` for the ids that can't play right now (any mode — the
    caller decides what to do with them)."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    snap = snapshot(store)
    if not snap.dead:
        return {}
    tracks = store._tracks
    out: dict[str, str] = {}
    for tid in ids:
        if not isinstance(tid, str):
            continue
        k = snap.kind_of(tracks.get(tid))
        if k:
            out[tid] = k
    return out


def playable_page(fetch: Callable[[int, int], list], offset: int, limit: int,
                  snap: Snapshot, *, budget: int | None = None) -> dict:
    """One page of a list for the PLAYER, without the tracks that can't play.

    ``fetch(offset, limit)`` returns the list's rows at those positions (a
    short result = the end of the list).  Reads forward from ``offset`` until
    ``limit`` playable rows are found, the list ends, or ``budget`` rows were
    read.  Every returned row carries ``_pos`` — its position in the list —
    and the reply says where to go on (``next_offset``) and whether the list
    ended, so a player keeps list positions exact while it skips."""
    if budget is None:
        budget = PLAYABLE_SCAN_BUDGET
    out: list[dict] = []
    pos = max(0, int(offset))
    read = 0
    ended = False
    # At most two reads: a first one a few pages long (usually enough), then
    # the rest of the budget in one go — a filtered list re-walks its index
    # from the start on every read, so many small reads deep into it cost
    # offset × reads.
    step = max(1, min(max(int(limit) * 4, 200), int(budget)))
    while len(out) < limit and read < budget:
        step = max(1, min(step, budget - read))
        rows = fetch(pos, step)
        if not rows:
            ended = True
            break
        for i, r in enumerate(rows):
            if snap.kind_of(r):
                continue
            out.append({**r, "_pos": pos + i})
            if len(out) >= limit:
                return {"tracks": out, "next_offset": pos + i + 1, "ended": False}
        read += len(rows)
        pos += len(rows)
        if len(rows) < step:
            ended = True
            break
        step = budget - read
    return {"tracks": out, "next_offset": pos, "ended": ended}


_ANCHOR_CHUNK = 5_000
_ANCHOR_MAX = 2_000_000
_ANCHOR_IDS = 8


async def _anchor_position(fetch: Callable[[int, int], list], after: str) -> int | None:
    """Where the anchor is in the list ``fetch`` reads: ``after`` names up to
    ``_ANCHOR_IDS`` track ids, the preferred first (a queue's last list rows,
    newest first — the newest may itself be hidden now).  The position of the
    most preferred one the list holds, or None.  Read in slices with a turn
    for the event loop between them (once per availability change of a queue)."""
    ids = [i for i in (after or "").split(",") if i][:_ANCHOR_IDS]
    if not ids:
        return None
    rank = {tid: k for k, tid in enumerate(ids)}
    best: tuple[int, int] | None = None                 # (rank, position)
    pos = 0
    while pos < _ANCHOR_MAX:
        rows = fetch(pos, _ANCHOR_CHUNK)
        for i, r in enumerate(rows):
            k = rank.get(r.get("id")) if isinstance(r, dict) else None
            if k is not None and (best is None or k < best[0]):
                best = (k, pos + i)
                if k == 0:
                    return best[1]
        if len(rows) < _ANCHOR_CHUNK:
            break
        pos += len(rows)
        await asyncio.sleep(0)
    return best[1] if best else None


async def player_page_async(fetch: Callable[[int, int], list], offset: int, limit: int,
                            store=None, after: str | None = None) -> dict:
    """``player_page`` with ``after``: start right after the anchor
    (``_anchor_position``) instead of at ``offset`` when the list holds one —
    a queue re-anchors this way once Hide mode's list changed length under
    it (a drive went away or came back), so it neither repeats nor skips."""
    if after:
        at = await _anchor_position(fetch, after)
        if at is not None:
            offset = at + 1
    return player_page(fetch, offset, limit, store)


def player_page(fetch: Callable[[int, int], list], offset: int, limit: int,
                store=None) -> dict:
    """A list endpoint's ``playable=1`` reply (the shape ``playable_page``
    gives): without the tracks that can't play in ``mark`` / ``hide`` mode,
    the plain page otherwise (``player_page_async`` re-anchors)."""
    snap = snapshot(store)
    if mode(store) == "show" or not snap.dead:
        off = max(0, int(offset))
        rows = fetch(off, limit)
        return {"tracks": [{**r, "_pos": off + i} for i, r in enumerate(rows)],
                "next_offset": off + len(rows), "ended": len(rows) < limit}
    return playable_page(fetch, offset, limit, snap)


def state(store=None) -> dict:
    """``GET /api/library/availability``: the setting, the generation token
    and the roots that are offline right now."""
    _touch()
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    m = mode(store)
    snap = snapshot(store)
    roots = snap._roots
    if roots is None:
        # (Once per snapshot: clients poll this.  A snapshot is replaced whenever
        # a dead root's track count or cached copies change, so the counts hold.)
        roots = []
        if snap.dead:
            from soniqboom.core import catalogue
            idx = getattr(store, "_tag_scan_root_hash", {}) or {}
            un = snap.unavailable
            for h, kind in snap.dead.items():
                path = snap.paths.get(h, "")
                ids = idx.get(h) or ()
                roots.append({
                    "path": path, "path_hash": h, "kind": kind,
                    "label": catalogue.label_of(path) if kind == KIND_CATALOGUE else None,
                    "tracks": len(ids),
                    "unavailable": len(un & ids) if ids else 0,
                })
        snap._roots = roots
    roots = [dict(r) for r in roots]
    return {"mode": m, "token": f"{m}:{_BOOT}:{snap.seq}", "seq": snap.seq,
            "unavailable": len(snap.unavailable) if snap.dead else 0,
            "roots": roots}


# ── Change notification ──────────────────────────────────────────────────────

_loop: asyncio.AbstractEventLoop | None = None
_last_sent: str | None = None
_pending: asyncio.TimerHandle | None = None
_watch_task: asyncio.Task | None = None
_NOTIFY_DELAY_S = 0.3
WATCH_INTERVAL_S = 10.0
LOCAL_PROBE_EVERY = 3          # × WATCH_INTERVAL_S: local roots re-probed every 30 s


_tasks: set = set()


def notify_changed() -> None:
    """Something that decides availability changed (a root's status, the
    setting): tell the web clients once things have settled.  Safe to call
    from any thread; a no-op before ``install``."""
    loop = _loop
    if loop is None or loop.is_closed():
        return

    def _fire() -> None:
        t = loop.create_task(_broadcast_if_changed())
        _tasks.add(t)                       # (a reference until it is done)
        t.add_done_callback(_tasks.discard)

    def _arm() -> None:
        global _pending
        if _pending is not None:
            _pending.cancel()
        _pending = loop.call_later(_NOTIFY_DELAY_S, _fire)
    try:
        if _running_loop() is loop:
            _arm()
        else:
            loop.call_soon_threadsafe(_arm)
    except RuntimeError:
        pass


def _running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def _broadcast_if_changed(force: bool = False) -> None:
    global _last_sent, _pending
    _pending = None
    try:
        from soniqboom.core.store import get_store
        store = get_store()
        t = token(store)
    except Exception:                                   # noqa: BLE001
        return
    if not force and t == _last_sent:
        return
    first = _last_sent is None
    _last_sent = t
    if first and not force:
        return                                          # the state at start: nothing to tell
    try:
        from soniqboom.api.library import _broadcast
        await _broadcast({"event": "availability_changed", "token": t,
                          "mode": t.split(":", 1)[0]})
    except Exception:                                   # noqa: BLE001
        log.debug("availability broadcast failed", exc_info=True)


async def _watch() -> None:
    """Every ``WATCH_INTERVAL_S``: notice offline/online changes no status
    write announced (a share's source registered or dropped, a catalogue's
    offline hold running out); every ``LOCAL_PROBE_EVERY`` rounds re-probe
    the local roots (an ejected drive has no event of its own) — only while
    the web UI is in use (a page connected, or a list / the state read in
    the last ``INTEREST_S``): a server nobody browses doesn't keep touching
    its drives (a spun-down disk, a NAS mount)."""
    n = 0
    while True:
        await asyncio.sleep(WATCH_INTERVAL_S)
        n += 1
        if n % LOCAL_PROBE_EVERY == 0 and _in_use():
            try:
                from soniqboom.core.data import refresh_local_root_availability
                await refresh_local_root_availability()
            except asyncio.CancelledError:
                raise
            except Exception:                           # noqa: BLE001
                log.debug("local root probe failed", exc_info=True)
        try:
            await _broadcast_if_changed()
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.debug("availability watch failed", exc_info=True)


def _in_use() -> bool:
    if time.monotonic() - _last_interest < INTEREST_S:
        return True
    try:
        from soniqboom.api.library import _ws_clients
        return bool(_ws_clients)
    except Exception:                                   # noqa: BLE001
        return True


def install(store, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Hook the store's scan-dir changes and start the watcher (main startup)."""
    global _loop, _watch_task, _last_sent
    _loop = loop or asyncio.get_running_loop()
    store._on_scan_dirs_changed = notify_changed
    try:
        _last_sent = token(store)
    except Exception:                                   # noqa: BLE001
        _last_sent = None
    if _watch_task is None or _watch_task.done():
        _watch_task = _loop.create_task(_watch(), name="availability-watch")


def uninstall() -> None:
    global _loop, _watch_task, _pending
    t = _watch_task
    _watch_task = None
    if t is not None and not t.done():
        t.cancel()
    if _pending is not None:
        _pending.cancel()
        _pending = None
    _loop = None


def reset_for_tests() -> None:
    global _seq, _last_sent
    with _state_lock:
        _memo.update(store=None, sig=None, snap=None)
        _members_memo.clear()
        _members_jobs.clear()
    _last_sent = None
