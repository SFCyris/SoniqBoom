# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Incremental post-scan enrichment: which tracks a pass has to look at.

The store records every track write a pass could care about in its
enrichment change log (``TrackStore.enrich_changes``): inserts, deletes and
changes to any field but the ones no pass reads (``_ENRICH_EXEMPT_FIELDS``) —
whoever wrote it (a scan, a user edit, a duplicate / HVSC / duration
backfill, another pass).  Each pass keeps its OWN position in that log, so one
pass consuming the changes hides nothing from another, and joins only the
tracks changed since its last run (widened by the pass to the neighbours its
verdict depends on).

A pass records ``(cursor, inputs)`` after a run (``record``): ``inputs`` are
its non-track inputs (index file, settings, title lists …).  The next run
(``changes``) re-joins everything when it has no position (first run of the
process, a load since, the log dropped the entries), when an input changed,
when it is forced (the Admin button, a settings switch) or when more than
``limit`` tracks changed; otherwise it gets the changed ids — an empty set
means nothing to do.

A run that saw no write but its own (``own`` = the log entries its writes
added) moves its position past them; any other write landing meanwhile keeps
it at the run's snapshot, so the next run covers that write (and re-reads its
own, a no-op)."""
from __future__ import annotations

# A delta run past this share of the library is no cheaper than a full one.
_FULL_SHARE = 0.25
_MIN_LIMIT = 1_000


def limit(store) -> int:
    """More changed tracks than this → a full pass."""
    return max(_MIN_LIMIT, int(store.track_count() * _FULL_SHARE))


def changes(store, last, inputs, *, force: bool = False) -> "set[str] | None":
    """The ids changed since the run recorded in ``last`` (see ``record``) —
    None for a full pass (forced, nothing recorded, other ``inputs``, the log
    can't tell, or too many)."""
    if force or last is None or last[1] != inputs:
        return None
    return store.enrich_changes(last[0], limit(store))


def foreign_writes(store, snap: tuple, own: int) -> bool:
    """Did anything but the run's own writes (``own`` log entries) change the
    library since its snapshot at ``snap``?"""
    epoch, seq = snap
    cur = store.enrich_cursor()
    return not (cur[0] is epoch and cur[1] == seq + own)


def record(store, snap: tuple, own: int, inputs) -> tuple:
    """What a run that read the library at ``snap`` (``store.enrich_cursor()``
    taken with its snapshot) and whose writes added ``own`` log entries
    records for the next run."""
    if foreign_writes(store, snap, own):
        return (snap, inputs)
    return (store.enrich_cursor(), inputs)      # nothing but our own writes since


def tracks_of(store, ids) -> list[dict]:
    """The stored tracks of ``ids`` that still exist (deleted ones drop out),
    in id order."""
    get = store.get_track
    return [t for t in map(get, sorted(ids)) if t is not None]
