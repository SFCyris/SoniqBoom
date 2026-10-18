# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One-time conversion of stored FTP listing times from host-local to UTC.

FTP listing times are wall-clock times with no zone: MLSD ``modify=`` facts
are UTC (RFC 3659), and LIST dates are read as UTC too
(``FTPFileSource._parse_list_mtime_*``).  Builds before that read every one
of them as the HOST's local time (``naive.timestamp()``), so every stored FTP
mtime is ``local_mktime(naive)`` where it should be ``timegm(naive)``.  With
the parser fixed, an un-converted store would see every FTP file as changed
(``scanner._mtime_matches_tolerant`` allows 2 s) and re-download whole
shares.  ``run`` converts the stored values once:

* track records of FTP shares (``mtime`` > 0, paths ``ftp://…``, archive
  members included — they carry their archive's listing time) through the
  store's normal write path (``update_track_fields_batch``: one AOF record,
  replayed all-or-nothing), never the Modland catalogue's tracks (their
  ``mtime`` is a "merged" timestamp, not a listing time) or another
  catalogue's;
* the archive enumeration sidecar (``cache/archive_enum.json``: ``barren`` /
  ``named`` entries of ``ftp://`` scan roots);
* the folder-browse caches are dropped when anything changed (they hold
  copies of the track rows).

Not converted: the remote folder state is memory-only, and the conversion
cache holds no mtime.  The remote cache's index records the ``(size, mtime)``
a copy was made from (``RemoteCache.get_cached(ident=…)``) — written only by
builds that already read FTP times as UTC (released builds before the
conversion record none), so its values are UTC already; FTP ``stat()``
reports no mtime, so subset names carry 0.

Exactness: the old value came from ``datetime(naive).timestamp()`` (fold=0)
under the host's time-zone rules for that date (DST-aware), so the inverse is
``datetime.fromtimestamp``.  Two cases need care:

* the fall-back hour (a wall time that occurs twice): the old parse always
  took the FIRST occurrence (fold=0), and ``fromtimestamp`` gives that same
  wall time back — exact;
* the spring-forward gap (a wall time that does not exist locally): the old
  parse read it with the offset from before the change, which is the same
  instant as the wall time one gap later, so ONE stored value has TWO
  possible wall times.  Rule: take the earlier.  A wrong pick makes that one
  file look changed once (it is read again, its value then correct) — never
  a missed change.

Crash safety: the marker (store config ``MARKER_KEY``) is written
``pending`` — with a few probe tracks' old and new values — before the batch,
and ``done`` after it.  The batch is a single AOF record, so after a crash it
is either fully in the store or not at all; on the next start the probes
tell which, so values are never shifted twice.  The sidecar carries its own
marker in the same atomic file write.  The host's CURRENT zone rules are
used: a host whose zone changed since the values were stored converts with
the wrong offset (one re-read per file).  On a UTC host nothing changes.
"""
from __future__ import annotations

import calendar
import datetime
import json
import logging
import math
import time

log = logging.getLogger(__name__)

MARKER_KEY = "ftp_mtime_utc_v1"
# Top-level key of cache/archive_enum.json once its ftp:// entries are UTC.
SIDECAR_MARKER = "ftp_mtime_utc"
_PROBES = 8
_EPOCH = datetime.datetime(1970, 1, 1)


def local_epoch_to_utc_epoch(value: float) -> float:
    """``timegm(naive)`` for the ``naive`` wall time whose host-local reading
    (``datetime(naive).timestamp()``, fold=0) was ``value`` — see the module
    docstring for the fall-back hour and the spring-forward gap (earlier
    wall time wins)."""
    whole = math.floor(value)
    frac = value - whole
    utc_naive = _EPOCH + datetime.timedelta(seconds=whole)
    cands = set()
    for probe in (whole - 86400, whole, whole + 86400):
        try:
            off = time.localtime(probe).tm_gmtoff
        except (OverflowError, OSError, ValueError):
            continue
        wall = utc_naive + datetime.timedelta(seconds=off)
        try:
            if wall.timestamp() == whole:
                cands.add(wall)
        except (OverflowError, OSError, ValueError):
            continue
    if not cands:                         # not a value the old parse could give
        try:
            cands.add(datetime.datetime.fromtimestamp(whole).replace(fold=0))
        except (OverflowError, OSError, ValueError):
            return value
    wall = min(cands)
    return float(calendar.timegm(wall.timetuple())) + frac


class _Converter:
    """``local_epoch_to_utc_epoch`` with a per-day shortcut: when the host's
    UTC offset is the same at the starts of the day before, the day, and the
    two days after (no change within a day of the value), the result is
    ``value + offset`` — what the exact function gives there.  Any other day
    takes the exact path.  (Checked against the exact function over random
    times and every DST-change day of several zones in the tests.)"""

    def __init__(self) -> None:
        self._days: dict[int, "int | None"] = {}

    def __call__(self, value: float) -> float:
        day = math.floor(value) // 86400
        off = self._days.get(day, False)
        if off is False:
            off = None
            try:
                offs = {time.localtime((day + k) * 86400).tm_gmtoff for k in (-1, 0, 1, 2)}
                if len(offs) == 1:
                    off = offs.pop()
            except (OverflowError, OSError, ValueError):
                off = None
            self._days[day] = off
        if off is None:
            return local_epoch_to_utc_epoch(value)
        return value + off


def _is_ftp_listing_track(path: str) -> bool:
    if not isinstance(path, str) or not path.startswith("ftp://"):
        return False
    try:
        from soniqboom.core import modland_catalog
        if path.startswith(modland_catalog.ROOT + ":"):
            return False
    except Exception:                                   # noqa: BLE001
        pass
    try:
        from soniqboom.core import catalogue
        if catalogue.is_catalogue_path(path):
            return False
    except Exception:                                   # noqa: BLE001
        pass
    return True


def _track_items(store) -> list[tuple[str, float, float]]:
    out = []
    conv = _Converter()
    roots: dict[str, bool] = {}           # scan root → FTP listing times?
    for t in store.all_tracks():
        try:
            m = float(t.get("mtime") or 0)
        except (TypeError, ValueError):
            continue
        path = t.get("path")
        if m <= 0 or not isinstance(path, str) or not path.startswith("ftp://"):
            continue
        cut = path.find(":/", 6)                      # past "ftp://": the scan root
        root = path[:cut] if cut > 0 else path
        ok = roots.get(root)
        if ok is None:
            ok = roots[root] = _is_ftp_listing_track(path)
        if not ok:
            continue
        new = conv(m)
        if new != m and new > 0:
            out.append((t["id"], m, new))
    return out


def _probes_applied(store, probes) -> "bool | None":
    """True: the batch the probes describe is in the store; False: it is
    not; None: no probe track is left to tell."""
    for p in probes or ():
        try:
            tid, old, new = p[0], float(p[1]), float(p[2])
        except (TypeError, ValueError, IndexError):
            continue
        t = store.get_track(tid)
        if not t:
            continue
        cur = float(t.get("mtime") or 0)
        if cur == new:
            return True
        if cur == old:
            return False
    return None


def _convert_sidecar() -> int:
    """Convert ``barren`` / ``named`` entries of ``ftp://`` scan roots in
    ``cache/archive_enum.json`` (once: marked in the same atomic write)."""
    from soniqboom.core import scanner
    from soniqboom.core.durable import atomic_write_bytes
    with scanner._enum_sidecar_lock:
        path = scanner._enum_sidecar_path()
        if not path.exists():
            return 0
        d = scanner._load_enum_sidecar()
        if not d or d.get(SIDECAR_MARKER):
            return 0
        n = 0
        for section in ("barren", "named"):
            entries = d.get(section)
            if not isinstance(entries, dict):
                continue
            for key, rec in entries.items():
                if not (isinstance(key, str) and key.startswith("ftp://")
                        and isinstance(rec, list) and rec):
                    continue
                scan_root = key.split("|", 1)[0]
                if not _is_ftp_listing_track(scan_root + ":/x"):
                    continue
                try:
                    m = float(rec[0])
                except (TypeError, ValueError):
                    continue
                if m > 0:
                    new = local_epoch_to_utc_epoch(m)
                    if new != m:
                        rec[0] = new
                        n += 1
        d[SIDECAR_MARKER] = 1
        atomic_write_bytes(path, json.dumps(d).encode("utf-8"), 0o644)
        return n


def run(store) -> dict:
    """Convert once (see the module docstring); a no-op once ``MARKER_KEY``
    says ``done``.  Call after the store is loaded and its journal attached,
    and before any FTP source is listed.  Returns counts for the log."""
    marker = store.get_config(MARKER_KEY)
    if isinstance(marker, dict) and marker.get("state") == "done":
        return {"skipped": True}
    t0 = time.perf_counter()
    tz = "/".join(time.tzname)
    tracks = 0
    resumed = None
    if isinstance(marker, dict) and marker.get("state") == "pending":
        resumed = _probes_applied(store, marker.get("probes"))
    if resumed is None and isinstance(marker, dict) and marker.get("state") == "pending":
        # A crash after "pending" whose probe tracks are all gone: whether the
        # batch landed is unknown — leave the values (a wrong guess either way
        # only makes files look changed once; a double shift is avoided).
        log.warning("FTP listing times: an earlier conversion was interrupted and "
                    "can't be checked; leaving the stored values")
    elif not resumed:
        items = _track_items(store)
        if items:
            store.set_config(MARKER_KEY, {
                "state": "pending", "tz": tz,
                "probes": [[tid, old, new] for tid, old, new in items[:_PROBES]]})
            tracks = store.update_track_fields_batch(
                [(tid, {"mtime": new}) for tid, _old, new in items])
    sidecar = 0
    try:
        sidecar = _convert_sidecar()
    except Exception:                                   # noqa: BLE001
        log.warning("FTP listing times: the archive list sidecar could not be "
                    "converted (its archives are listed again once)", exc_info=True)
    if tracks or resumed:
        try:
            from soniqboom.api.fstree import invalidate_browse_cache
            invalidate_browse_cache()
        except Exception:                               # noqa: BLE001
            log.debug("browse cache invalidation failed", exc_info=True)
    store.set_config(MARKER_KEY, {"state": "done", "tz": tz, "tracks": tracks,
                                  "sidecar": sidecar, "at": time.time()})
    secs = time.perf_counter() - t0
    if tracks or sidecar:
        log.info("FTP listing times converted to UTC (host zone %s): %d track(s), "
                 "%d archive record(s) in %.2fs", tz, tracks, sidecar, secs)
    return {"tracks": tracks, "sidecar": sidecar, "resumed": bool(resumed),
            "seconds": round(secs, 3)}
