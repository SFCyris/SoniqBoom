# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Runtime index-integrity: a background drift sweep + cached health state.

The TrackStore's derived indexes (``_tag_*``, ``_sorted_*``, ``_agg_*``,
``_word_index``) are maintained incrementally on every
mutation.  If any mutation path mis-maintains one, it drifts out of sync with
``_tracks`` SILENTLY — producing fast-but-wrong results (e.g. a format filter
returning 0) with no error, no hung request, and no log anomaly — until a
restart's full rebuild quietly fixes it.

This module is the missing runtime safety net:

* a low-frequency background **sweep** that, only when something an index
  reads has changed since the last one, checks every derived index against
  ``_tracks`` (``_Audit``), heals what drifted and records whether drift was
  found;
* a cached **health** snapshot for ``GET /admin/stats`` so that endpoint can
  report a real ``index_ok`` instead of a hardcoded ``True``.

Detection without self-healing would just be an alarm; healing without
detection would hide bugs.  The sweep does both and logs a warning when it had
to correct drift, so a recurring incremental-maintenance bug becomes visible.

The check runs in slices of at most ``_SLICE_S`` of loop time and needs no
quiet library:

* the tag / word / aggregate indexes and the browse id sets are rebuilt on a
  private shadow ``TrackStore`` (``_index_track``, the one key derivation);
  while it runs, the store calls its ``_write_watch`` before every track
  write, which records the track — with a copy of what the shadow indexed —
  for a replay onto the shadow (``_Audit._settle``).  With that log replayed
  the shadow IS the full rebuild of the live tracks, and every comparison
  slice runs at such a moment, so a write landing meanwhile is checked like
  any other state instead of voiding the work;
* the sorted lists are checked in place, outside batch mode: a copy is
  strictly ascending, holds one entry per track (the primary list one per
  primary, the ``game:`` side list's tracks checked one by one) and every
  entry carries its track's current key — an entry whose track was written
  after the copy is checked against the live list instead.  (Plays feed no
  index: a user's unplayed tracks are read from that user's play stats.)

Nothing moves — no sequence bump, no memo dropped — when nothing drifted.
Only the library being replaced (a load, an import), a scan starting, more
than ``_WATCH_CAP`` track writes during one check, a batch that won't end or
the deadline leave a check unfinished (the next tick retries).
"""
from __future__ import annotations

import asyncio
import bisect
import gc
import itertools
import logging
import operator
import os
import time

log = logging.getLogger("soniqboom.core.index_health")

DEFAULT_INTERVAL_SECONDS = 1800.0  # 30 min; override via SONIQBOOM_INDEX_SWEEP_SECONDS, <=0 disables

# Loop time of one synchronous slice of a check, and the pause after it: a
# timer, not a bare ``sleep(0)`` — work that became ready meanwhile (a
# request's chain of callbacks, a timer's wake-up) runs before the next slice,
# not behind one or two more of them.
_SLICE_S = 0.004
_PAUSE_S = 0.0005
# Track writes recorded for the shadow's replay during one check before it
# gives up (a scan / an apply landed: the next tick checks again).
_WATCH_CAP = 50_000
# A check unfinished after this long gives up.
_DEADLINE_S = 900.0
# How long the sorted-list checks wait for a batch section (a scan commit,
# an apply) to end before they give up.
_QUIET_WAIT_S = 120.0
# Mismatch samples kept per index.
_SAMPLE = 5
# A set this large is compared, and freed, a slice of its elements at a time.
_BIG_SET = 16_384
# Elements compared / freed between two looks at the clock.
_WORK = 2_048

# Cached last-known health.  Seeded optimistic because the boot rebuild
# (populate_store -> store.rebuild_indexes) is authoritative, so indexes are
# correct at startup; ``checked_at=None`` marks this as boot-assumed, not yet
# independently verified.
_state: dict = {
    "index_ok": True,
    "checked_at": None,            # unix ts of the last real verification
    "last_check_kind": "boot",     # boot | sweep | reindex | manual
    "mutation_seq": None,
    "track_count": None,
    "drift_detected_total": 0,
    "auto_heal_total": 0,
    "last_mismatches": [],
}
_task: asyncio.Task | None = None
_last_swept_seq: tuple | None = None   # ``_gate_seq`` at the last completed check
_sim_built_seq: int | None = None      # ``_mutation_seq`` the sample-token index was built at


def _gate_seq(store) -> tuple:
    """The sweep gate: ``(_browse_seq, _duration_seq)``.  One helper for the
    seed, the compare and the stamp, so they can't differ in shape.  The first
    moves on every write to a field an index reads (every insert and delete,
    any field outside ``store._BROWSE_EXEMPT_FIELDS``, which no index reads),
    a sorted list replaced and an index swap; a write confined to fields no
    index reads (a health mark, STIL, cover art) moves neither.  A
    duration-only write (a play-time length backfill) moves only the second,
    and only ``_sorted_duration`` reads it: such a tick checks that list alone.
    Plays (``_play_seq``) are left out: they feed no index."""
    return (getattr(store, "_browse_seq", store._mutation_seq),
            getattr(store, "_duration_seq", 0))


def snapshot() -> dict:
    """Return the last-known index health (used by GET /admin/stats)."""
    return dict(_state)


def record(report: dict, kind: str, *, healed: bool) -> None:
    """Fold a verify/rebuild drift report into the cached health state.

    ``healed`` = did this operation REBUILD the indexes (reindex / sweep)?  If
    so the indexes are correct NOW even when drift was found pre-heal, so
    ``index_ok`` reflects the healthy post-heal state; a non-healing verify
    (``healed=False``) reports the actual current state.  Either way the drift,
    if any, is counted and its mismatches retained.
    """
    drift = not report.get("index_ok", True)
    _state["index_ok"] = True if healed else (not drift)
    _state["checked_at"] = time.time()
    _state["last_check_kind"] = kind
    _state["mutation_seq"] = report.get("mutation_seq")
    _state["track_count"] = report.get("track_count")
    _state["last_mismatches"] = list(report.get("mismatches", []))[:10]
    if drift:
        _state["drift_detected_total"] += 1
        if healed:
            _state["auto_heal_total"] += 1


# ── The check ────────────────────────────────────────────────────────────────

class _Unfinished(Exception):
    """A check that can't finish (its reason: ``args[0]``)."""


class _Watch:
    """The store's ``_write_watch`` during a check: before a write to track
    ``tid``, record it for the shadow's replay — with a copy of the track as
    it is now (= as the shadow indexed it) when the shadow holds it, None when
    it doesn't (not indexed yet, or a new track).  The first record per track
    until its replay wins (the shadow still holds that version)."""

    __slots__ = ("done", "pending", "overrun")

    def __init__(self) -> None:
        self.done: set[str] = set()                 # ids the shadow holds
        self.pending: dict[str, "dict | None"] = {}
        self.overrun = False

    def __call__(self, tid: str, pre: "dict | None") -> None:
        pending = self.pending
        if self.overrun or tid in pending:
            return
        if pre is not None and tid in self.done:
            # (list values copied: a later in-place change must not alter
            # what is unindexed)
            pending[tid] = {k: (v.copy() if v.__class__ is list else v)
                            for k, v in pre.items()}
        else:
            pending[tid] = None
        if len(pending) > _WATCH_CAP:
            self.overrun = True


def _has(lst: list, entry: tuple) -> bool:
    i = bisect.bisect_left(lst, entry)
    return i < len(lst) and lst[i] == entry


def _scanning() -> bool:
    try:
        from soniqboom.core import scanner
        return scanner.is_scanning()
    except Exception:                                   # noqa: BLE001
        return False


class _Audit:
    """One check of the store's derived indexes (see the module docstring).
    ``full`` False: ``_sorted_duration`` only (a duration-only tick).
    ``run`` returns the drift report — ``_diff_indexes``' shape — after
    healing what drifted, or raises ``_Unfinished``."""

    def __init__(self, store, *, full: bool = True) -> None:
        self.store = store
        self.full = full
        self.tracks = store._tracks
        self.epoch = store._enrich_epoch
        self.started = time.monotonic()
        self.mark = time.perf_counter()
        self.scan_checked_at = 0.0
        self.shadow = None
        self.watch: _Watch | None = None
        self.indexes: dict[str, dict] = {}
        self.mismatches: list[dict] = []
        self.healed = False

    # ── pacing ──

    def _due(self) -> bool:
        return time.perf_counter() - self.mark >= _SLICE_S

    async def _turn(self) -> None:
        await asyncio.sleep(_PAUSE_S)
        self.mark = time.perf_counter()
        self._alive()

    def _alive(self) -> None:
        st = self.store
        if st._tracks is not self.tracks or st._enrich_epoch is not self.epoch:
            raise _Unfinished("library-replaced")
        if self.watch is not None and (self.watch.overrun or st._write_watch is not self.watch):
            raise _Unfinished("busy")
        now = time.monotonic()
        if now - self.started > _DEADLINE_S:
            raise _Unfinished("deadline")
        if now - self.scan_checked_at >= 0.25:
            self.scan_checked_at = now
            if _scanning():
                raise _Unfinished("scan-active")

    # ── the shadow ──

    async def _build_shadow(self) -> None:
        from soniqboom.core.store import TrackStore
        st = self.store
        shadow = TrackStore()
        shadow._tracks = self.tracks                 # (read only)
        shadow.enter_batch_mode()
        shadow._mark_all_sorted_dirty()              # (its sorted lists are not built)
        self.shadow = shadow
        w = self.watch = _Watch()
        st._write_watch = w
        ids = list(self.tracks)
        tracks, done, pending, index = self.tracks, w.done, w.pending, shadow._index_track
        for j in range(0, len(ids), 64):
            for tid in ids[j:j + 64]:
                if tid in pending or tid in done:
                    continue
                t = tracks.get(tid)
                if t is not None:
                    index(tid, t)
                    done.add(tid)
            if self._due():
                await self._turn()
                await self._settle()

    async def _settle(self) -> None:
        """Replay the recorded writes onto the shadow until none is left.
        Returns, without an await after its last check, at a moment the
        shadow is the full rebuild of the live tracks."""
        w, shadow, tracks = self.watch, self.shadow, self.tracks
        pending = w.pending
        while pending:
            while pending:
                tid, pre = pending.popitem()
                if pre is not None:
                    shadow._unindex_track(tid, pre)
                t = tracks.get(tid)
                if t is not None:
                    shadow._index_track(tid, t)
                    w.done.add(tid)
                else:
                    w.done.discard(tid)
                if self._due():
                    break
            if pending:
                await self._turn()

    def _note(self, attr: str, live_n: int, exp_n: int, sample: list) -> None:
        ok = not sample and live_n == exp_n
        self.indexes[attr] = {"actual": live_n, "expected": exp_n, "ok": ok}
        if not ok:
            self.mismatches.append({"index": attr, "actual": live_n, "expected": exp_n,
                                    "sample": [str(x) for x in sample[:_SAMPLE]]})

    async def _compare_shadowed(self) -> None:
        from soniqboom.core.store import shadowed_index_attrs
        st = self.store
        for attr in shadowed_index_attrs():
            exp = getattr(self.shadow, attr)
            await self._settle()
            live = getattr(st, attr)
            live_n, exp_n = len(live), len(exp)     # (one moment, shadow current)
            sample: list = []
            is_set = isinstance(exp, set)
            keys = list(exp)
            work = 0                                # (elements compared since the last clock check)
            for k in keys:
                if work >= _WORK:
                    work = 0
                    if self._due():
                        await self._turn()
                        await self._settle()
                        live = getattr(st, attr)
                if is_set:
                    work += 1
                    if k in exp and k not in live and len(sample) < _SAMPLE:
                        sample.append(k)
                    continue
                ev = exp.get(k)
                if ev is None or len(sample) >= _SAMPLE:
                    work += 1
                    continue
                if ev.__class__ is set and len(ev) > _BIG_SET:
                    if not await self._same_big_set(attr, k):
                        sample.append(k)
                    live = getattr(st, attr)
                    work = _WORK                    # (its own slices: check the clock next)
                    continue
                work += len(ev) + 1 if isinstance(ev, (set, dict)) else 1
                if live.get(k) != ev:
                    sample.append(k)
            if live_n != exp_n and len(sample) < _SAMPLE:
                await self._settle()
                live = getattr(st, attr)
                sample += [k for k in live if k not in exp][:_SAMPLE - len(sample)]
            self._note(attr, live_n, exp_n, sample)

    async def _same_big_set(self, attr: str, key) -> bool:
        """``live[attr][key] == shadow[attr][key]`` for a large id set, a
        slice of its elements at a time (each slice with the shadow
        current): the same size at one moment, then every element of the
        shadow's set still in it found in the live one."""
        st = self.store
        await self._settle()
        ev = getattr(self.shadow, attr).get(key)
        lv = getattr(st, attr).get(key)
        if ev is None:
            return True                             # (gone meanwhile: nothing to compare)
        if lv is None or len(lv) != len(ev):
            return False
        elems = list(ev)
        for j in range(0, len(elems), 4096):
            if self._due():
                await self._turn()
                await self._settle()
            ev = getattr(self.shadow, attr).get(key)
            lv = getattr(st, attr).get(key)
            if ev is None:
                return True
            if lv is None:
                return False
            for x in elems[j:j + 4096]:
                if x in ev and x not in lv:
                    return False
        return True

    # ── sorted lists, in place ──

    async def _quiet(self) -> None:
        """Wait (yielding) for a moment outside batch mode with no sorted list
        pending its deferred rebuild — returns at such a moment."""
        st = self.store
        t0 = time.monotonic()
        while st._batch_mode or st._dirty_sorted:
            if time.monotonic() - t0 > _QUIET_WAIT_S:
                raise _Unfinished("batch")
            await asyncio.sleep(0.05)
            self.mark = time.perf_counter()
            self._alive()

    async def _check_sorted(self, name: str) -> None:
        from soniqboom.core.store import _SORTED_SPECS
        st, tracks = self.store, self.tracks
        value = _SORTED_SPECS[name][1]
        while True:
            await self._quiet()
            if self.shadow is not None:
                await self._settle()            # (its _nonprimary_ids: the count below)
            if not (st._batch_mode or st._dirty_sorted):
                break
        # (one moment, outside batch mode)
        copy = getattr(st, name).copy()
        n_tracks = len(tracks)
        if name == "_sorted_added_at_primary":
            # one entry per primary: every track but the non-primary copies —
            # the shadow's count of those (the live set is checked on its own)
            nonprimary = (self.shadow._nonprimary_ids if self.shadow is not None
                          else st._nonprimary_ids)
            want = n_tracks - len(nonprimary)
        elif name == "_sorted_title_fold":
            want = None                             # (its tracks are checked one by one)
        else:
            want = n_tracks
        n = len(copy)
        disorder: list = []
        suspects: list[tuple] = []
        lt = operator.lt
        for j in range(0, n, 512):
            part = copy[j:j + 513]
            if not all(map(lt, part, itertools.islice(part, 1, None))) and len(disorder) < _SAMPLE:
                k = next(i for i in range(len(part) - 1) if not part[i] < part[i + 1])
                disorder.append(f"@{j + k}: {part[k]!r} >= {part[k + 1]!r}")
            for e in part[:512]:
                t = tracks.get(e[1])
                if t is None or value(t) != e[0]:
                    suspects.append(e)
            if self._due():
                await self._turn()
        if want is None:
            ids = list(tracks)
            for j in range(0, len(ids), 256):
                await self._quiet()
                live = getattr(st, name)
                for tid in ids[j:j + 256]:
                    t = tracks.get(tid)
                    v = value(t) if t is not None else None
                    if v is not None and not _has(live, (v, tid)):
                        suspects.append((v, tid))
                if self._due():
                    await self._turn()
        # A suspect is drift only if the LIVE list is wrong about its track now
        # (its track may simply have been written since the copy).
        drifted: list = []
        for j in range(0, len(suspects), 256):
            await self._quiet()
            live = getattr(st, name)
            for k, tid in suspects[j:j + 256]:
                t = tracks.get(tid)
                v = value(t) if t is not None else None
                if (v is not None and not _has(live, (v, tid))) or (v != k and _has(live, (k, tid))):
                    drifted.append((k, tid))
            if self._due():
                await self._turn()
        self._note(name, n, n if want is None else want, disorder + drifted)

    # ── heal ──

    async def _heal(self) -> None:
        """Make the drifted indexes right again: the shadow's copy of a
        drifted tag / word / aggregate index or id set swapped in (at a moment
        it is current), the sorted lists rebuilt
        (the scanner's yielding rebuild) — then every memo and sequence moves
        (``_library_replaced``)."""
        from soniqboom.core.store import SORTED_LISTS, shadowed_index_attrs
        st = self.store
        drifted = {m["index"] for m in self.mismatches}
        swap = [a for a in shadowed_index_attrs() if a in drifted]
        if swap:
            await self._settle()
        # (no await from here until the sorted rebuild)
        st._write_watch = None
        self.watch = None
        for attr in swap:
            obj = getattr(self.shadow, attr)
            setattr(st, attr, obj)
            setattr(self.shadow, attr, type(obj)())  # (the store's now: not freed below)
        if "_word_index" in swap:
            st._word_list_dirty = True              # (no account of what changed)
        st._library_replaced()
        self.healed = True
        if drifted & set(SORTED_LISTS):
            from soniqboom.core import scanner
            st.enter_batch_mode()
            st._mark_all_sorted_dirty()
            # (shielded: a cancelled sweep still finishes the rebuild — left
            # half-way, every list would stay marked for a rebuild no exit runs)
            await asyncio.shield(scanner._async_exit_batch_mode(st))

    async def _drop_shadow(self) -> None:
        """Free the shadow's indexes a slice at a time (dropped at once, a
        word index of 680K tracks was a ~0.1 s deallocation)."""
        shadow, self.shadow = self.shadow, None
        if shadow is None:
            return
        from soniqboom.core.store import shadowed_index_attrs
        for attr in shadowed_index_attrs():
            obj = getattr(shadow, attr)
            setattr(shadow, attr, None)
            pop = obj.popitem if isinstance(obj, dict) else obj.pop
            work = 0
            while obj:
                v = pop()
                v = v[1] if isinstance(obj, dict) else None
                if isinstance(v, (set, dict)):
                    if len(v) > _BIG_SET:
                        await self._free_gradually(v)
                    else:
                        work += len(v)
                del v
                work += 1
                if work >= _WORK:
                    work = 0
                    if self._due():
                        await asyncio.sleep(_PAUSE_S)
                        self.mark = time.perf_counter()

    async def _free_gradually(self, obj) -> None:
        """Empty a large set / dict a slice at a time (its last reference is
        the caller's)."""
        pop = obj.popitem if isinstance(obj, dict) else obj.pop
        while obj:
            for _ in range(4096):
                if not obj:
                    break
                pop()
            if self._due():
                await asyncio.sleep(_PAUSE_S)
                self.mark = time.perf_counter()

    # ── the whole check ──

    async def run(self) -> dict:
        st = self.store
        was = gc.isenabled()
        gc.disable()            # (the shadow's ~1M containers: no full collections walking them)
        try:
            try:
                self._alive()
                if self.full:
                    await self._build_shadow()
                    await self._compare_shadowed()
                    from soniqboom.core.store import SORTED_LISTS
                    for name in SORTED_LISTS:
                        await self._check_sorted(name)
                else:
                    await self._check_sorted("_sorted_duration")
                if self.mismatches:
                    await self._heal()
            finally:
                if st._write_watch is self.watch:
                    st._write_watch = None
                self.watch = None
                # (freed a slice at a time on every way out: dropped with
                # this object it would go at once)
                await self._drop_shadow()
        finally:
            if was:
                gc.enable()
        return {
            "index_ok": not self.mismatches,
            "track_count": len(st._tracks),
            "mutation_seq": st._mutation_seq,
            "indexes": self.indexes,
            "mismatches": self.mismatches,
            "mode": "full" if self.full else "duration",
            "healed": self.healed,
            "seconds": round(time.monotonic() - self.started, 2),
        }


_audit_lock: asyncio.Lock | None = None
_audit_lock_loop = None


def _lock() -> asyncio.Lock:
    global _audit_lock, _audit_lock_loop
    loop = asyncio.get_running_loop()
    if _audit_lock is None or _audit_lock_loop is not loop:
        _audit_lock = asyncio.Lock()
        _audit_lock_loop = loop
    return _audit_lock


async def audit(store=None, *, full: bool = True) -> "dict | None":
    """Check (and heal) the store's derived indexes — the report, or None
    when the check couldn't finish (``_Unfinished``; logged at debug)."""
    if store is None:
        from soniqboom.core.store import get_store
        store = get_store()
    async with _lock():
        try:
            return await _Audit(store, full=full).run()
        except _Unfinished as exc:
            log.debug("Index integrity check unfinished (%s) — retried next tick", exc.args[0])
            return None


async def _refresh_sim_tokens(store) -> None:
    """Rebuild the retro sample-token index (``store._sim_tok_*``, batch-built,
    not maintained per write) after track writes since its last build."""
    global _sim_built_seq
    seq = store._mutation_seq
    if _sim_built_seq == seq:
        return
    for _ in store._iter_build_sim_token_index(chunk=2_000):
        await asyncio.sleep(_PAUSE_S)
    _sim_built_seq = seq


async def _sweep_once() -> dict | None:
    """One sweep tick, while no scan is running: checks (and heals) the
    indexes iff something an index reads changed since the last check, and
    records health; rebuilds the sample-token index iff any track write
    (``_mutation_seq`` — an ``instruments`` write included, which no checked
    index reads) landed since its last build.  Returns the drift report, or
    None if the check was skipped or unfinished."""
    global _last_swept_seq
    from soniqboom.core.store import get_store
    store = get_store()
    # Don't fight an in-progress scan: it churns _tracks heavily; we'll
    # re-check next tick once it settles.
    if _scanning():
        return None
    report = None
    seq = _gate_seq(store)
    if _last_swept_seq != seq:
        full = _last_swept_seq is None or _last_swept_seq[0] != seq[0]
        report = await audit(store, full=full)
        if report is not None:
            # The gate as the check started: a write during the check moves
            # it, so the next tick looks again — after a heal, as the heal
            # left it.  (Unfinished: the gate stays, the next tick retries.)
            _last_swept_seq = _gate_seq(store) if report.get("healed") else seq
            drift = not report.get("index_ok", True)
            record(report, kind="sweep", healed=True)   # the sweep healed it; index_ok is True now
            if drift:
                log.warning(
                    "Index integrity sweep DETECTED DRIFT and auto-healed it "
                    "(track_count=%s): %s",
                    report.get("track_count"), report.get("mismatches"),
                )
    try:
        await _refresh_sim_tokens(store)
    except Exception:                                   # noqa: BLE001
        log.warning("Sample-token index refresh failed", exc_info=True)
    return report


async def _sweep_loop(interval: float) -> None:
    log.info("Index integrity sweep running every %.0fs", interval)
    # First tick sooner so drift introduced by an early-session mutation (before
    # one full interval elapses) is caught within minutes — narrows the window
    # where /admin/stats reports the optimistic boot-assumed ``index_ok``.
    # Still gated on ``_gate_seq``, so a quiescent library does nothing.
    delay = min(interval, 300.0)
    while True:
        try:
            await asyncio.sleep(delay)
            delay = interval
            await _sweep_once()
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Index integrity sweep iteration failed (continuing)")


def start(interval: float | None = None) -> asyncio.Task | None:
    """Start the background sweep (idempotent).  Seeds the gate with the
    current sequences so a freshly-booted, healthy library is not checked
    until something actually changes."""
    global _task, _last_swept_seq, _sim_built_seq
    if _task is not None and not _task.done():
        return _task
    if interval is None:
        try:
            interval = float(os.environ.get("SONIQBOOM_INDEX_SWEEP_SECONDS", DEFAULT_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL_SECONDS
    if interval <= 0:
        log.info("Index integrity sweep disabled (interval <= 0)")
        return None
    try:
        from soniqboom.core.store import get_store
        _store = get_store()
        _last_swept_seq = _gate_seq(_store)
        _sim_built_seq = _store._mutation_seq     # (built by the boot's rebuild)
    except Exception:
        _last_swept_seq = None
    _task = asyncio.create_task(_sweep_loop(interval))
    return _task


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
