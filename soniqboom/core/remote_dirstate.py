# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-share folder state for the remote fast walk (freshness polls).

A folder's modification time changes when an entry DIRECTLY inside it is
created, deleted or renamed — not when something deeper changes, and not
when a file's contents are rewritten in place.  That is how POSIX, NTFS,
Samba, FTP servers and plain WebDAV servers behave.  So "skip a folder whose
mtime hasn't changed" only works for folders without subfolders; skipping an
unchanged ``Artist`` folder hides a new file in ``Artist/Album``.

Every remote walk records, per folder: the mtime its parent listed for it,
its entry names and its subfolders.  A fast walk then lists:

* folders that have subfolders (their own mtime says nothing about what is
  below them) — unless the server is seen to propagate changes up the tree
  (Nextcloud / ownCloud style), see below.  All of them up to a budget of
  max(``_DEEP_MIN``, ``_DEEP_SQRT`` × √folders); past that, the ones a fast
  walk listed least recently, round-robin;
* every folder whose listed mtime differs from the recorded one, that is new,
  that has no mtime, or whose mtime is newer than the poll's cap (but not in
  the future);
* a rotating set of leaf folders, max(``_ROTATE_MIN``, ``_ROTATE_SQRT`` ×
  √leaf folders) of them, the ones a fast walk listed least recently — a
  round-robin over every leaf (a full walk does not restart it), the only way
  to see an in-place rewrite without a full walk.

and prunes every other folder.  A fast walk therefore lists at most: the root
+ the folders-with-subfolders budget + the rotation and the folders above it
+ the folders that changed — growing with the square root of the share's
size, not with its size.  A new or renamed entry in a folder whose parent is
listed is found by the next fast poll; one deeper below folders past the
budget within as many fast polls as the round-robin takes to reach them (or
the next full walk); an in-place rewrite by the rotation or the next full
walk.

System folders (``filesource.is_system_folder``: ``@eaDir``, ``#recycle``
…) are neither listed nor recorded — the scanner leaves them out.

Propagation: when a folder whose entry names did not change shows a new mtime
while exactly one of its subfolders changed, the server propagates changes to
parent folders.  A walk in which most known folders show a new mtime (an FTP
switch between MLSD seconds and LIST minutes, LIST dates ageing into the
year-only form, a server clock change) gives no evidence either way.  After ``_PROPAGATION_MIN_YES`` such observations and none against
(a folder whose subfolder changed while its own mtime stayed put), folders
with subfolders are pruned by mtime as well.  One observation against turns
it off for good (for this process).

State lives in memory: after a restart the first fast poll of a share is a
full walk, which records it again.  Only completed listings are recorded; a
folder whose listing failed is forgotten, so the next walk lists it.  The
scanner commits a walk's listings only once its changes are stored, and
forgets the folders of files it could not store (``finish(forget=…)``).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from typing import Iterable

log = logging.getLogger(__name__)

_ROTATE_MIN = 16            # leaf folders re-listed per fast walk, at least…
_ROTATE_SQRT = 1.0          # …or this × √(leaf folders)
_DEEP_MIN = 32              # folders with subfolders listed per fast walk, at least…
_DEEP_SQRT = 1.5            # …or this × √(all folders) (then least recently fast-listed first)
_PROPAGATION_MIN_YES = 3    # observations before trusting propagated folder mtimes
_MAX_SHARES = 2048          # scan roots kept (one-shot subfolder checks add some)
_MASS_SHIFT_FRAC = 0.5      # more known folders than this with a new mtime: no evidence
_FUTURE_SLACK_S = 300.0     # a folder mtime later than now + this is clock / zone skew


def norm(path: str) -> str:
    """Root-relative folder key: ``/`` for the root, ``/A/B`` below it."""
    p = "/" + (path or "").strip("/")
    return p


def container(rel: str) -> str:
    """The real file a stored path lives in: the archive / disk image of an
    archive member (``/x/a.zip::m.mod`` → ``/x/a.zip``,
    ``/A :: B/a.zip::m.mod`` → ``/A :: B/a.zip``), else the path itself — the
    same split as the scanner's (``filesource.split_member``).  A folder or
    file name that merely contains ``::`` is not split."""
    from soniqboom.core.filesource import member_container
    return member_container(rel)


def _budget(n: int, floor: int, k: float) -> int:
    """How many of ``n`` folders one fast walk lists: max(floor, k·√n), at
    most ``n``."""
    return min(n, max(int(floor), math.ceil(max(0.0, float(k)) * math.sqrt(n))))


def _parent(key: str) -> str:
    head = key.rpartition("/")[0]
    return head or "/"


class _Dir:
    __slots__ = ("mtime", "subdirs", "names", "listed", "fast")

    def __init__(self, mtime: float, subdirs: tuple, names: frozenset, listed: int,
                 fast: int = 0):
        self.mtime = mtime          # as the parent listed it when this folder was listed
        self.subdirs = subdirs      # child folder keys
        self.names = names          # every entry name (files and folders)
        self.listed = listed        # walk sequence of the last listing
        self.fast = fast            # walk sequence of the last FAST walk that listed it
                                    # (the round-robin's order: a full walk lists all)


class ShareDirs:
    """What the walks of one scan root have seen."""

    def __init__(self) -> None:
        self.dirs: dict[str, _Dir] = {}
        self.seq = 0
        self.prop_yes = 0
        self.prop_no = 0
        # Folders that listed EMPTY (no error) while they held entries before,
        # in the last walk: not believed until a later walk lists them empty
        # again (``WalkPlan.unconfirmed_empty``).
        self.suspect_empty: set[str] = set()
        self.lock = threading.Lock()

    @property
    def propagates(self) -> bool:
        return self.prop_no == 0 and self.prop_yes >= _PROPAGATION_MIN_YES

    def leaf_count(self) -> int:
        return sum(1 for k, d in self.dirs.items() if not d.subdirs and k != "/")


_states: "OrderedDict[str, ShareDirs]" = OrderedDict()
_states_lock = threading.Lock()


def _get(scan_root: str) -> ShareDirs:
    with _states_lock:
        st = _states.get(scan_root)
        if st is None:
            st = _states[scan_root] = ShareDirs()
            while len(_states) > _MAX_SHARES:
                _states.popitem(last=False)
        else:
            _states.move_to_end(scan_root)
        return st


def has_state(scan_root: str) -> bool:
    with _states_lock:
        st = _states.get(scan_root)
    return st is not None and "/" in st.dirs


def forget(scan_root: str) -> None:
    """Drop a scan root's state (the share was removed)."""
    with _states_lock:
        _states.pop(scan_root, None)


def snapshot(scan_root: str) -> dict:
    """Counts for logs / tests."""
    with _states_lock:
        st = _states.get(scan_root)
    if st is None:
        return {"folders": 0, "leaves": 0, "propagates": False}
    with st.lock:
        return {"folders": len(st.dirs), "leaves": st.leaf_count(),
                "propagates": st.propagates, "prop_yes": st.prop_yes,
                "prop_no": st.prop_no, "walks": st.seq}


class WalkPlan:
    """One walk of one scan root: which folders to list (``skip``), what was
    seen (``record``), and the commit of it into the share's state
    (``finish``).  ``skip``/``record`` run on the walk's thread; ``finish``
    wherever the caller commits (the remote scan: on the event loop, after
    its changes are stored).  A plan that is never finished changes nothing."""

    def __init__(self, scan_root: str, *, cap: "float | None") -> None:
        self.scan_root = scan_root
        self.share = _get(scan_root)
        self.cap = cap if cap and cap > 0 else None
        self.now = time.time()
        with self.share.lock:
            self.share.seq += 1
            self.seq = self.share.seq
            # A fast walk needs what an earlier walk recorded; without it the
            # walk is full (and records it).
            self.fast = cap is not None and "/" in self.share.dirs
            self.propagates = self.share.propagates
            self.force: set[str] = set()
            self.deep: "set[str] | None" = None     # None = every folder with subfolders
            if self.fast:
                self.force = self._rotation()
                self.deep = self._deep_set()
        self.listed: dict[str, tuple[frozenset, dict[str, float]]] = {}
        self.seen_mtime: dict[str, float] = {}
        self.failed: set[str] = set()
        self._suspects: "set[str] | None" = None     # unconfirmed_empty's verdict
        self.stats = {"listed": 0, "pruned": 0, "rotated": 0, "deep": 0,
                      "changed": 0, "new": 0}

    # ── decisions ─────────────────────────────────────────────────────────
    def _rotation(self) -> set[str]:
        # Least recently listed BY A FAST WALK first (ties: by name) — a
        # round-robin over every leaf.  (By the last listing of any walk, a
        # full walk would tie them all and restart it at the first names.)
        leaves = [(d.fast, k) for k, d in self.share.dirs.items()
                  if not d.subdirs and k != "/"]
        if not leaves:
            return set()
        budget = _budget(len(leaves), _ROTATE_MIN, _ROTATE_SQRT)
        leaves.sort()
        out: set[str] = set()
        for _seq, k in leaves[:budget]:
            while k not in out and k != "/":
                out.add(k)                      # the leaf and the folders above it
                k = _parent(k)
        return out

    def _deep_set(self) -> "set[str] | None":
        interior = [(d.fast, k) for k, d in self.share.dirs.items()
                    if d.subdirs and k != "/"]
        budget = _budget(len(self.share.dirs), _DEEP_MIN, _DEEP_SQRT)
        if len(interior) <= budget:
            return None
        interior.sort()
        out: set[str] = set()
        for _seq, k in interior[:budget]:
            while k not in out and k != "/":
                out.add(k)
                k = _parent(k)
        return out

    def skip(self, d) -> bool:
        """``skip_subtree_fn`` for ``walk_with_stat``: True prunes ``d``."""
        key = norm(d.path)
        self.seen_mtime[key] = d.mtime
        if not self.fast:
            return False
        info = self.share.dirs.get(key)
        if info is None:
            self.stats["new"] += 1
            return False
        if d.mtime <= 0 or info.mtime <= 0 or d.mtime != info.mtime:
            self.stats["changed"] += 1
            return False
        if self.cap is not None and self.cap < d.mtime <= self.now + _FUTURE_SLACK_S:
            # Changed around the last poll (a second change within the same
            # minute is invisible to a minute-precision LIST).  A future
            # mtime is a server clock or time-zone offset (a LIST server that
            # prints its local time) — it would re-list the folder on every
            # poll.
            self.stats["changed"] += 1
            return False
        if key in self.force:
            self.stats["rotated"] += 1
            return False
        if info.subdirs and not self.propagates and (self.deep is None or key in self.deep):
            self.stats["deep"] += 1
            return False
        self.stats["pruned"] += 1
        return True

    # ── observations ──────────────────────────────────────────────────────
    def record(self, dirpath: str, dir_entries, file_entries, *, failed: bool) -> None:
        key = norm(dirpath)
        if failed:
            self.failed.add(key)
            return
        self.stats["listed"] += 1
        names = frozenset([e.name for e in dir_entries] + [e.name for e in file_entries])
        subs: dict[str, float] = {}
        for e in dir_entries:
            ck = norm(e.path)
            subs[ck] = e.mtime
            self.seen_mtime.setdefault(ck, e.mtime)
        self.listed[key] = (names, subs)

    def unconfirmed_empty(self, holds) -> set[str]:
        """Folders this walk listed EMPTY, without an error, although they
        held something — entries the last walk recorded, or what ``holds(key)``
        says (tracks stored under it) — and that the walk before did not list
        empty too.  One empty answer is not believed: such a folder counts as
        a failed listing in this walk (its tracks are kept, it is listed again
        next time); the next FULL walk's empty listing confirms it.  Only full
        walks suspect or confirm (a fast walk's empty listing never deletes
        anything, ``confirmed_absent``); recorded into the share by
        ``finish``."""
        out: set[str] = set()
        if self.fast:
            return out
        with self.share.lock:
            suspects = set(self.share.suspect_empty)
            dirs = self.share.dirs
            for key, (names, _subs) in self.listed.items():
                if names or key == "/" or key in self.failed:
                    continue
                old = dirs.get(key)
                if not ((old is not None and old.names) or holds(key)):
                    continue                    # empty before too, or holds nothing
                if key in suspects:
                    continue                    # empty twice in a row: believed
                out.add(key)
        self._suspects = out
        self.failed |= out
        return out

    def finish(self, forget: "Iterable[str]" = ()) -> None:
        """Fold this walk's listings into the share's state.  ``forget``:
        folders (keys or paths) to leave unknown — their listing showed files
        the scan could not store, so the next walk must list them again."""
        sh = self.share
        with sh.lock:
            dirs = sh.dirs
            yes = no = 0
            # Propagation evidence first (against the state before this walk):
            # a folder whose own entries are the same and exactly one of whose
            # subfolders changed — did its own mtime move with it?  A walk in
            # which most known folders moved at once (a precision / clock
            # shift) proves nothing.
            known = moved = 0
            for key in self.listed:
                old = dirs.get(key)
                cur = self.seen_mtime.get(key, 0.0)
                if key == "/" or old is None or old.mtime <= 0 or cur <= 0:
                    continue
                known += 1
                moved += cur != old.mtime
            if known and moved <= known * _MASS_SHIFT_FRAC:
                for key, (names, subs) in self.listed.items():
                    old = dirs.get(key)
                    if old is None or key == "/" or old.names != names or old.mtime <= 0:
                        continue
                    cur = self.seen_mtime.get(key, 0.0)
                    if cur <= 0:
                        continue
                    changed = sum(1 for ck, m in subs.items()
                                  if (o := dirs.get(ck)) is not None and o.mtime > 0
                                  and m > 0 and m != o.mtime)
                    if changed == 1:
                        if cur != old.mtime:
                            yes += 1
                        else:
                            no += 1
                    elif changed > 1 and cur == old.mtime:
                        no += 1
            removed: set[str] = set()
            for key, (names, subs) in self.listed.items():
                old = dirs.get(key)
                if old is not None:
                    removed.update(set(old.subdirs) - set(subs))
                dirs[key] = _Dir(self.seen_mtime.get(key, 0.0) if key != "/" else 0.0,
                                 tuple(subs), names, self.seq,
                                 self.seq if self.fast else (old.fast if old is not None else 0))
            for key in self.failed:
                dirs.pop(key, None)             # unknown now: listed next time
            if self._suspects is not None and not self.fast:
                # What listed empty this full walk is suspect until the next
                # one lists it; what it listed otherwise is no longer.
                sh.suspect_empty = ((sh.suspect_empty - set(self.listed))
                                    | self._suspects)
            for key in forget:
                dirs.pop(norm(key), None)
            if removed:
                removed -= set(self.listed)          # re-created (listed) in this walk
                pre = tuple(k.rstrip("/") + "/" for k in removed)
                for k in [k for k in dirs if k in removed or (pre and k.startswith(pre))]:
                    dirs.pop(k, None)
            was = sh.propagates
            sh.prop_yes += yes
            sh.prop_no += no
            if sh.propagates != was:
                log.info("Remote folders of %s %s: %s", self.scan_root,
                         "propagate changes to parent folders" if sh.propagates
                         else "don't propagate changes to parent folders",
                         "fast polls prune unchanged folders with subfolders"
                         if sh.propagates else "fast polls list every folder with subfolders")

    # ── deletions seen by a fast walk ─────────────────────────────────────
    def confirmed_absent(self, rel: str) -> bool:
        """``rel`` (a root-relative file path, or an archive member) is known
        to be gone: the deepest folder above it that this walk listed has no
        entry for the next path component.  False when that folder was not
        listed, its listing failed or was empty, or the entry is there (an
        archive's members are checked by full walks)."""
        path = norm(container(rel))
        child = path.rpartition("/")[2]
        d = _parent(path)
        while True:
            got = self.listed.get(d)
            if got is not None:
                names = got[0]
                return bool(names) and child not in names
            if d == "/" or d in self.failed:
                return False
            child = d.rpartition("/")[2]
            d = _parent(d)
