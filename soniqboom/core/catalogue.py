# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalogue roots: remote trees whose library tracks come from an index file
instead of a scan (the Modland catalogue, ``core/modland_catalog.py``).

A catalogue track is a remote track like any other — path
``<root>:/<rel>``, id = ``uuid5`` of that path — but nothing has read its file:
title, composer, format and size come from the index.  The file is fetched
from the archive only when a listener PLAYS the track (and at most one
next-track prewarm per listener).  This module is the single authority every
reader of remote bytes asks:

* ``is_catalogue_root`` / ``is_catalogue_path`` / ``root_of`` — which paths
  belong to a catalogue (a root counts only while it is registered).
* ``is_cached`` — the track's file already sits in the local remote cache, so
  reading it costs the archive nothing.
* ``fetch_ok(path, lane)`` — the guard of every reader that is NOT a play
  (waveforms, VU meters, pattern views, art, duration probes, repair, HVSC,
  cast lookahead …): True for any non-catalogue path; for a catalogue track
  only when its file is cached — or, on the ``"stream"`` lane, while a play's
  download of it is running (the reader then waits for that download instead
  of starting one).
* ``permit_play`` / ``permit_prewarm`` — a play opens a short window in which
  the catalogue's file source may fetch that file and the companion files
  beside it (TFMX ``smpl.*``, ``*.psflib`` …); a prewarm does the same with a
  budget of ONE track per listener (a newer prewarm replaces the older one).
  New downloads are rate-limited (``RATE_MAX`` per ``RATE_WINDOW_S``).
* ``allowed`` — the file source's gate: any read outside such a window raises
  ``CatalogueFetchRefused`` (the backstop behind the per-reader guards).
* ``note_read`` / ``note_refused`` / ``stats`` — a count and a short log of
  every network read and every refusal (Admin status, tests).
* ``subsonic_hidden_ids`` — the ids Subsonic (and DLNA) clients must not see
  while the catalogue's "Show to Subsonic apps" switch is off.

Import-light on purpose: the store imports it.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

# How long a play's (or prewarm's) fetch window stays open.  Long enough for a
# slow download plus the Range re-requests a player makes while it plays.
PERMIT_TTL_S = 15 * 60
# New (uncached) catalogue downloads allowed per window, all listeners
# together — protects the archive from a client that walks the catalogue.
RATE_MAX = 120
RATE_WINDOW_S = 10 * 60
# Reads for tunes shown in a list (the opt-in "For tunes shown in lists" tune
# details): their own, smaller budget inside RATE_MAX, and never the last
# LIST_PLAY_RESERVE downloads of the window — browsing a list can't make a
# play wait for the rate limit.
LIST_RATE_MAX = 60
LIST_PLAY_RESERVE = 40
# Companion files may sit up to this many folders above the tune (AdLib banks
# shared by a collection — ``api/stream._ADLIB_BANK_PARENT_LEVELS``).
_COMPANION_PARENT_LEVELS = 4


class CatalogueFetchRefused(OSError):
    """A catalogue file was asked for outside a play's fetch window."""


@dataclass
class _Root:
    root: str                                   # scan root, no trailing slash
    key: str                                    # "modland"
    label: str                                  # "Modland"
    # rel path → is it a music file (a track candidate) in the index?  The
    # gate lets a play fetch non-music files (companions) near its tune.
    is_music: Callable[[str], bool] = lambda rel: True
    # Whether Subsonic / DLNA clients may see this catalogue's tracks.
    subsonic_visible: Callable[[], bool] = lambda: False
    prefix: str = field(init=False)

    def __post_init__(self) -> None:
        self.prefix = self.root + ":/"


@dataclass
class _Grant:
    root: str
    rel: str
    dirs: frozenset
    expiry: float
    owner: str | None = None


_lock = threading.Lock()
_roots: dict[str, _Root] = {}
# Immutable snapshots of the registry (replaced whole on a change): readers in
# worker threads iterate these, never the dict a toggle may be changing.
_root_list: tuple = ()
_prefixes: tuple[str, ...] = ()
_root_hashes: frozenset = frozenset()
_play_grants: dict[tuple[str, str], _Grant] = {}
_prewarm_grants: dict[str, _Grant] = {}          # owner → its one grant
_new_fetches: collections.deque = collections.deque()
_list_fetches: collections.deque = collections.deque()
_stats = {"reads": 0, "bytes": 0, "refused": 0, "permits": 0,
          "prewarm_permits": 0, "rate_limited": 0}
_read_log: collections.deque = collections.deque(maxlen=200)
_refused_log: collections.deque = collections.deque(maxlen=50)


# ── Registry ──────────────────────────────────────────────────────────────────

def _norm_root(root: str) -> str:
    return (root or "").rstrip("/")


def register_root(root: str, *, key: str, label: str,
                  is_music: Callable[[str], bool] | None = None,
                  subsonic_visible: Callable[[], bool] | None = None) -> None:
    """Make ``root`` a catalogue root (idempotent; replaces the callbacks)."""
    r = _Root(_norm_root(root), key, label)
    if is_music is not None:
        r.is_music = is_music
    if subsonic_visible is not None:
        r.subsonic_visible = subsonic_visible
    with _lock:
        _roots[r.root] = r
        _refresh_keys()


def _refresh_keys() -> None:
    global _prefixes, _root_hashes, _root_list
    import hashlib
    lst = tuple(_roots.values())
    _root_list = lst
    _prefixes = tuple(x.prefix for x in lst)
    _root_hashes = frozenset(hashlib.sha256(x.root.encode()).hexdigest()[:16] for x in lst)


def root_hashes() -> frozenset:
    """``path_hash`` of every registered catalogue root (the store's
    ``scan_root_hash`` of its tracks)."""
    return _root_hashes


def unregister_root(root: str) -> None:
    root = _norm_root(root)
    with _lock:
        _roots.pop(root, None)
        _refresh_keys()
        for k in [k for k in _play_grants if k[0] == root]:
            del _play_grants[k]
        for o in [o for o, g in _prewarm_grants.items() if g.root == root]:
            del _prewarm_grants[o]


def roots() -> list[dict]:
    return [{"root": r.root, "key": r.key, "label": r.label} for r in _root_list]


def is_catalogue_root(path) -> bool:
    if not path or not _root_list:
        return False
    p = _norm_root(str(path))
    return any(r.root == p for r in _root_list)


def _owned_by_longer_root(s: str, root: str) -> bool:
    """Does a registered NON-catalogue share root nested under catalogue
    ``root`` own path ``s`` (``ftp://…/pub/modules/SNDH`` — a share of one of
    the catalogue's folders — owns its own tracks and folders)?"""
    try:
        from soniqboom.core.filesource import _active_sources
        keys = list(_active_sources)
    except Exception:                                   # noqa: BLE001
        return False
    for k in keys:
        k = k.rstrip("/")
        if len(k) > len(root) and k.startswith(root + "/") and (
                s == k or s.startswith(k + ":/") or s.startswith(k + "/")):
            return True
    return False


def root_of(path) -> str | None:
    """The catalogue root a track path (``<root>:/<rel>``) or a folder path
    (``<root>`` / ``<root>/<dir>``) belongs to — None for any other path, also
    for the tracks and folders of a share nested under a catalogue root."""
    if not path or not _root_list:
        return None
    s = str(path)
    for r in _root_list:
        if s.startswith(r.prefix):
            return r.root
        if s == r.root or s.startswith(r.root + "/"):
            if ":/" in s[len(r.root):]:
                continue        # <root>/<dir>:/<rel>: a track of a share on <root>/<dir>
            if not _owned_by_longer_root(s, r.root):
                return r.root
    return None


def is_catalogue_path(path) -> bool:
    """True for a track path (or a folder path ``<root>/<dir>``) under a
    registered catalogue root, and for the root itself (``root_of``)."""
    if not path or not _prefixes:
        return False
    s = str(path)
    if s.startswith(_prefixes):
        return True
    return root_of(s) is not None


def label_of(root: str) -> str | None:
    p = _norm_root(root)
    return next((r.label for r in _root_list if r.root == p), None)


def key_of(root: str) -> str | None:
    p = _norm_root(root)
    return next((r.key for r in _root_list if r.root == p), None)


def _split(path: str) -> tuple[str, str] | None:
    """``(root, rel)`` of a catalogue track path; ``rel`` without a leading
    slash ("Protracker/4-Mat/x.mod")."""
    s = str(path)
    for r in _root_list:
        if s.startswith(r.prefix):
            return r.root, s[len(r.prefix):].lstrip("/")
    return None


def norm_rel(root: str, remote_path: str, remote_base: str = "") -> str:
    """A source-side path (``/Dir/x.mod`` root-relative, or the server's
    absolute ``/pub/modules/Dir/x.mod`` when ``remote_base`` is given) as the
    index's ``rel`` key."""
    p = (remote_path or "").replace("\\", "/")
    if remote_base and remote_base != "/" and (p == remote_base or p.startswith(remote_base + "/")):
        p = p[len(remote_base):]
    return p.lstrip("/")


# ── Cache state ───────────────────────────────────────────────────────────────

def is_cached(path) -> bool:
    """Is this catalogue track's file in the local remote cache (a read of it
    then costs the archive nothing)?  True for any non-catalogue path."""
    sp = _split(path) if path else None
    if sp is None:
        return True
    try:
        from soniqboom.core.remote_cache import get_cache
        return get_cache().peek_cached(sp[0], "/" + sp[1])
    except Exception:                                   # noqa: BLE001
        return False


def downloading(path) -> bool:
    """Is a download of this catalogue track's file running right now?"""
    sp = _split(path) if path else None
    if sp is None:
        return False
    try:
        from soniqboom.core.remote_cache import get_cache
        return get_cache().downloading(sp[0], "/" + sp[1])
    except Exception:                                   # noqa: BLE001
        return False


def fetch_ok(path, lane: str = "scan") -> bool:
    """May a reader that is NOT a play touch this track's bytes?  Always for
    a non-catalogue path; for a catalogue track only when its file is cached,
    or — ``lane="stream"`` (an interactive reader of the track being played:
    its waveform, VU meter, SID bytes) — while a download of it runs (the
    reader joins that download, the remote cache never starts a second)."""
    if not path or not is_catalogue_path(path):
        return True
    if is_cached(path):
        return True
    return lane == "stream" and downloading(path)


async def wait_fetchable(path, timeout: float = 8.0, grace: float = 1.5) -> bool:
    """Wait until a reader of the track being played may read catalogue track
    ``path`` (``fetch_ok(path, "stream")``): its meter, pattern and SID
    requests arrive before the play's download starts, and a "not there"
    answer then left a cold catalogue tune without them for the whole play
    (a local file's are there at once).  Up to ``timeout`` s while a play /
    prewarm window for it is open; a tune nothing plays answers after
    ``grace`` s.  True at once for any other path."""
    import asyncio
    if fetch_ok(path, "stream"):
        return True
    sp = _split(path)
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < timeout:
        await asyncio.sleep(0.1)
        if fetch_ok(path, "stream"):
            return True
        if loop.time() - start >= grace and not (sp and allowed(sp[0], sp[1])):
            return False
    return False


# ── Fetch windows (plays / prewarms) ──────────────────────────────────────────

def _grant_dirs(rel: str) -> frozenset:
    d = rel.rpartition("/")[0]
    out = []
    for _ in range(_COMPANION_PARENT_LEVELS + 1):
        out.append(d)
        if not d:
            break
        d = d.rpartition("/")[0]
    return frozenset(out)


def _prune(now: float) -> None:
    for k in [k for k, g in _play_grants.items() if g.expiry <= now]:
        del _play_grants[k]
    for o in [o for o, g in _prewarm_grants.items() if g.expiry <= now]:
        del _prewarm_grants[o]
    while _new_fetches and _new_fetches[0] <= now - RATE_WINDOW_S:
        _new_fetches.popleft()
    while _list_fetches and _list_fetches[0] <= now - RATE_WINDOW_S:
        _list_fetches.popleft()


def permit_play(path, *, ttl: float = PERMIT_TTL_S) -> bool:
    """Open the fetch window of a PLAY of catalogue track ``path`` (its file
    and the companion files beside it).  Returns False — the play must be
    refused (429) — only when the file is not cached and the download rate
    limit is reached.  True (no-op) for any non-catalogue path."""
    sp = _split(path) if path else None
    if sp is None:
        return True
    cached = is_cached(path)
    now = time.monotonic()
    with _lock:
        _prune(now)
        key = sp
        g = _play_grants.get(key)
        if g is None and not cached:
            if len(_new_fetches) >= RATE_MAX:
                _stats["rate_limited"] += 1
                log.warning("Catalogue: download rate limit reached (%d in %d min) — "
                            "refusing %s", RATE_MAX, RATE_WINDOW_S // 60, sp[1])
                return False
            _new_fetches.append(now)
        _play_grants[key] = _Grant(sp[0], sp[1], _grant_dirs(sp[1]), now + ttl)
        _stats["permits"] += 1
    return True


def permit_prewarm(path, owner: str, *, ttl: float = PERMIT_TTL_S) -> bool:
    """Open a PREWARM fetch window for catalogue track ``path`` on behalf of
    listener ``owner`` — one per listener: it replaces that listener's
    previous prewarm window.  False when the rate limit refuses it.  True
    (no-op) for any non-catalogue path."""
    sp = _split(path) if path else None
    if sp is None:
        return True
    cached = is_cached(path)
    now = time.monotonic()
    with _lock:
        _prune(now)
        prev = _prewarm_grants.get(owner)
        same = prev is not None and (prev.root, prev.rel) == sp
        if not cached and not same:
            if len(_new_fetches) >= RATE_MAX:
                _stats["rate_limited"] += 1
                return False
            _new_fetches.append(now)
        _prewarm_grants[owner] = _Grant(sp[0], sp[1], _grant_dirs(sp[1]), now + ttl, owner)
        _stats["prewarm_permits"] += 1
    return True


def permit_list(path, *, ttl: float = PERMIT_TTL_S) -> bool:
    """Open a fetch window to read catalogue track ``path`` because it is
    shown in a list (the opt-in list reads).  False — not read — when its
    budget (``LIST_RATE_MAX``) is spent or the window has fewer than
    ``LIST_PLAY_RESERVE`` downloads left for plays.  True (no-op) for a
    cached file or any non-catalogue path."""
    sp = _split(path) if path else None
    if sp is None:
        return True
    cached = is_cached(path)
    now = time.monotonic()
    with _lock:
        _prune(now)
        if not cached:
            if (len(_list_fetches) >= LIST_RATE_MAX
                    or len(_new_fetches) >= RATE_MAX - LIST_PLAY_RESERVE):
                _stats["rate_limited"] += 1
                return False
            _new_fetches.append(now)
            _list_fetches.append(now)
        _prewarm_grants[f"list:{sp[1]}"] = _Grant(sp[0], sp[1], _grant_dirs(sp[1]),
                                                  now + ttl, f"list:{sp[1]}")
        _stats["prewarm_permits"] += 1
    return True


def revoke(path) -> None:
    """Close every fetch window of ``path`` (tests; a track removed)."""
    sp = _split(path) if path else None
    if sp is None:
        return
    with _lock:
        _play_grants.pop(sp, None)
        for o in [o for o, g in _prewarm_grants.items() if (g.root, g.rel) == sp]:
            del _prewarm_grants[o]


def _allows(g: _Grant, root: str, rel: str, now: float) -> bool:
    if g.expiry <= now or g.root != root:
        return False
    if rel == g.rel:
        return True
    return rel.rpartition("/")[0] in g.dirs


def allowed(root: str, rel: str) -> bool:
    """The catalogue file source's gate: may ``rel`` be fetched from the
    archive now?  Inside a play's / prewarm's window: the tune itself, and any
    NON-music file (a companion) in its folder or the few above it."""
    root = _norm_root(root)
    rel = (rel or "").lstrip("/")
    now = time.monotonic()
    with _lock:
        r = _roots.get(root)
        if r is None:
            return False
        grants = [g for g in (*_play_grants.values(), *_prewarm_grants.values())
                  if _allows(g, root, rel, now)]
    if not grants:
        return False
    if any(g.rel == rel for g in grants):
        return True
    try:
        return not r.is_music(rel)
    except Exception:                                   # noqa: BLE001
        return False


# ── Read log ──────────────────────────────────────────────────────────────────

def note_read(root: str, rel: str, op: str, nbytes: int | None = None) -> None:
    with _lock:
        _stats["reads"] += 1
        if nbytes:
            _stats["bytes"] += int(nbytes)
        _read_log.append((time.time(), op, rel, nbytes))
    log.info("Catalogue fetch (%s): %s%s", op, rel,
             f" ({nbytes} bytes)" if nbytes else "")


def note_bytes(nbytes: int) -> None:
    with _lock:
        _stats["bytes"] += int(nbytes or 0)


def note_refused(root: str, rel: str, op: str) -> None:
    with _lock:
        _stats["refused"] += 1
        _refused_log.append((time.time(), op, rel))
    log.info("Catalogue: refused a %s of %s (not a play)", op, rel)


def stats() -> dict:
    with _lock:
        return {**_stats,
                "recent_reads": [{"at": t, "op": op, "rel": rel, "bytes": n}
                                 for t, op, rel, n in list(_read_log)[-20:]],
                "recent_refused": [{"at": t, "op": op, "rel": rel}
                                   for t, op, rel in list(_refused_log)[-10:]]}


def reset_stats() -> None:
    """Tests only."""
    with _lock:
        for k in _stats:
            _stats[k] = 0
        _read_log.clear()
        _refused_log.clear()
        _new_fetches.clear()
        _play_grants.clear()
        _prewarm_grants.clear()


# ── Subsonic / DLNA visibility ────────────────────────────────────────────────

def hidden_roots() -> list[str]:
    """Catalogue roots whose tracks Subsonic / DLNA clients must not see."""
    out = []
    for r in _root_list:
        try:
            vis = bool(r.subsonic_visible())
        except Exception:                               # noqa: BLE001
            vis = False
        if not vis:
            out.append(r.root)
    return out


def subsonic_hidden_path(path) -> bool:
    if not path or not _prefixes:
        return False
    r = root_of(path)
    return r is not None and r in hidden_roots()


def subsonic_hidden_ids(store) -> frozenset | set:
    """The ids of every catalogue track Subsonic / DLNA must not see (the
    store's live per-root id sets — do not mutate)."""
    roots_ = hidden_roots()
    if not roots_:
        return frozenset()
    import hashlib
    sets = []
    for r in roots_:
        h = hashlib.sha256(r.encode()).hexdigest()[:16]
        s = store._tag_scan_root_hash.get(h)
        if s:
            sets.append(s)
    if not sets:
        return frozenset()
    if len(sets) == 1:
        return sets[0]
    out: set = set()
    for s in sets:
        out |= s
    return out
