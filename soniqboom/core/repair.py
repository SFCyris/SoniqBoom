# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Repair task — re-extracts metadata for tracks whose titles/artists/albums
were corrupted by the old ``decode("ascii", errors="replace")`` path in
``metadata.py``.

Background
----------
The tracker / chiptune extractor used to decode fixed-size header bytes
as strict ASCII with ``errors='replace'``.  Every byte ≥ 0x80 (very
common in CP437 / Latin-1 / Shift-JIS demoscene files) was rewritten as
U+FFFD (the diamond ``�``) and persisted to the index.  The decoder is
fixed (`metadata._decode_tracker_str` does UTF-8 → CP437 → Latin-1) but
the index still holds the garbled strings — the scanner's incremental
mtime check would otherwise skip these files forever.

This module finds those tracks and re-runs the extractor in-place so
the corruption clears without forcing a full destructive rescan.  It
broadcasts progress as ``repair_progress`` WS events so the admin UI
can render the same kind of badge the scanner uses.

Identification heuristic
------------------------
A track is a candidate when any of ``title``, ``artist``, ``album``,
``album_artist`` contains the U+FFFD replacement character (a string
that practically never occurs in legitimate audio metadata — when it
does, the entry is almost certainly mojibake from a bad decode).

Scope
-----
The decoder fix only affects tracker / chiptune containers — but the
caller may choose to filter by extension to avoid the network cost of
re-downloading remote FLAC / DSD files that the fix would not change.
Extension filter is opt-in; default is "any U+FFFD-tainted track".
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from soniqboom.core.folder_album import spc_name_completes
from soniqboom.core.store import modland_artist_kept, songdb_fill_kept

log = logging.getLogger("soniqboom.repair")

# Sentinel char inserted by the broken ASCII-replace decode.
_FFFD = "�"

# Extensions where the bad decoder lived.  Useful as an optional filter
# — for everything else the corruption signal would have to come from a
# different bug, and re-extracting wouldn't help.
TRACKER_LIKE_EXTS: frozenset[str] = frozenset({
    # Chiptune / SID family
    ".sid", ".psid", ".rsid",
    # GME family (Game Music Emu containers)
    ".nsf", ".nsfe", ".spc", ".gbs", ".vgm", ".vgz", ".ay", ".kss",
    ".sap", ".gym", ".hes",
    # Tracker formats
    ".mod", ".s3m", ".it", ".xm", ".mtm", ".669", ".med",
    # AHX / HivelyTracker — header parsed by _extract_tracker; .hvl/.ahx
    # were previously mis-titled with the offset-0 format magic.
    ".ahx", ".hvl",
})

# Tracker formats whose offset-0 magic was mis-read as the title by the
# pre-fix ``_extract_tracker`` (it read the first bytes, which are the format
# MAGIC, not the song name).  A file with one of these exact titles is the
# magic-as-title artifact; re-extracting with the corrected parser yields the
# real embedded name.  Without this the four xeron/syphus .hvl modules all kept
# title "HVL", looked identical, and collapsed to one row under duplicate
# filtering.
_MAGIC_TITLE_BUG: dict[str, frozenset[str]] = {
    ".hvl": frozenset({"HVL"}),
    ".ahx": frozenset({"THX", "AHX"}),
}


# ── Progress state ───────────────────────────────────────────────────────────

# Cap the per-run error sample so a 100K-track library with a million
# stale paths doesn't balloon the progress dict.  The UI only needs
# enough to show the operator a representative slice.
_ERROR_SAMPLE_CAP = 50


@dataclass
class RepairProgress:
    running:    bool  = False
    total:      int   = 0
    processed:  int   = 0
    repaired:   int   = 0      # tracks where at least one field changed
    errors:     int   = 0
    # Counts of each error reason — surfaced in the UI so the operator
    # can see "ah, 869 zip-virtual, 2 ftp" rather than just "871".
    error_reasons: dict[str, int] = field(default_factory=dict)
    # First N (path, reason) tuples for the operator to inspect.
    error_samples: list[tuple[str, str]] = field(default_factory=list)
    current_file: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancelled:  bool  = False
    # What the run is: "repair" (Garbled-metadata repair, defect backfill) or
    # "game-names" (the header-game / GAME-tag reads) — the admin UI shows
    # each in its own section.
    kind:       str   = "repair"
    # Internal (not in ``to_dict``): the tracks a re-extract changed (their
    # folder-browse rows are refreshed once at the end), and how many changed
    # a title/artist (re-runs duplicate detection — its group key).
    changed_ids: set = field(default_factory=set)
    dup_key_changes: int = 0
    # Internal: ids whose file could not be reached (``_UNREACHABLE_REASONS``)
    # — the one-time header-game backfill retries them instead of finishing.
    unreachable_ids: set = field(default_factory=set)

    def pct(self) -> int:
        return min(100, int(self.processed / self.total * 100)) if self.total else 0

    def record_error(self, path: str, reason: str) -> None:
        """Bump counters and append to the sample list (capped)."""
        self.errors += 1
        # Take just the prefix before the colon so similar errors group
        # together (e.g. "zip-error: KeyError: 'foo'" all roll up under
        # "zip-error").
        key = reason.split(":", 1)[0] if ":" in reason else reason
        self.error_reasons[key] = self.error_reasons.get(key, 0) + 1
        if len(self.error_samples) < _ERROR_SAMPLE_CAP:
            self.error_samples.append((path, reason))

    def to_dict(self) -> dict:
        return {
            "running":      self.running,
            "total":        self.total,
            "processed":    self.processed,
            "repaired":     self.repaired,
            "errors":       self.errors,
            "error_reasons": dict(self.error_reasons),
            "error_samples": [
                {"path": p, "reason": r} for p, r in self.error_samples
            ],
            "pct":          self.pct(),
            "current_file": self.current_file,
            "started_at":   self.started_at,
            "finished_at":  self.finished_at,
            "cancelled":    self.cancelled,
            "kind":         self.kind,
        }


_progress = RepairProgress()
_task: asyncio.Task | None = None
_cancel_event: asyncio.Event | None = None


def get_progress() -> RepairProgress:
    return _progress


def is_running() -> bool:
    return _progress.running


# One-time backfill runs whose done-callback (``_settle``) has not run yet.
_pending_settles = 0


def _busy() -> bool:
    """A run is going, or the last one is still finishing (its album-cache
    refresh / duplicate re-group, then a one-time backfill's done-callback
    that settles its ids — ``_pending_settles``).  ``start_repair`` and the
    one-time backfills wait for that, so no run starts over a run's ids
    before they are settled (``is_running`` is False during the finish)."""
    return (is_running() or _pending_settles > 0
            or (_task is not None and not _task.done()))


async def wait_idle(timeout: float | None = None) -> bool:
    """Wait until no run is going or finishing (``_busy``); False on timeout."""
    loop = asyncio.get_running_loop()
    end = None if timeout is None else loop.time() + timeout
    while _busy():
        if end is not None and loop.time() >= end:
            return False
        await asyncio.sleep(0.05)
    return True


def request_cancel() -> bool:
    """Ask the running repair task to stop after the current file.
    Returns True if a task was running, False otherwise."""
    if _cancel_event is None or not _progress.running:
        return False
    _cancel_event.set()
    log.info("Repair cancel requested — will stop after current file")
    return True


# ── Identification ───────────────────────────────────────────────────────────

def _has_replacement_char(track: dict) -> bool:
    """True iff title/artist/album/album_artist contains U+FFFD."""
    for key in ("title", "artist", "album", "album_artist"):
        v = track.get(key)
        if isinstance(v, str) and _FFFD in v:
            return True
    return False


def _has_bad_tracker_title(track: dict) -> bool:
    """True for an AHX/HVL track whose stored title is a known mis-extraction
    artifact from the pre-fix ``_extract_tracker``:

      * the bare format MAGIC ("HVL" / "THX" / "AHX") — offset-0 read,
      * a mangled zip-virtual path ("a.zip::b.zip::song") — empty embedded
        name fell back to ``Path(virtual).stem``,
      * empty — nothing was extracted.

    Re-extracting with the corrected parser yields the real embedded name.
    """
    ext = os.path.splitext((track.get("path") or "").lower())[1]
    if ext not in (".hvl", ".ahx"):
        return False
    title = (track.get("title") or "").strip()
    if not title:
        return True
    if title in _MAGIC_TITLE_BUG.get(ext, frozenset()):
        return True
    # Mangled zip-virtual path leaked into the title.
    low = title.lower()
    if "::" in title or ".zip" in low:
        return True
    return False


_SID_EXTS = (".sid", ".psid", ".rsid")


def _has_cp437_corruption(track: dict) -> bool:
    """True for a SID-family track whose text was CP437-mis-decoded from its
    Latin-1 header bytes (e.g. ``Hülsbeck`` → ``Hⁿlsbeck``).

    The old SID decoder tried UTF-8 then **CP437** before Latin-1, so a byte
    like ``0xFC`` (Latin-1 ``ü``) became CP437's ``ⁿ`` (U+207F) — no U+FFFD, so
    :func:`_has_replacement_char` never flagged it.  Re-extracting with the
    corrected Latin-1-native decoder recovers the accented characters.

    Detection without false-positives on *correctly* decoded names: every real
    Latin-1 accented letter encodes to a CP437 byte in 0x80–0xA7 — the SOLE
    exception is ``ß`` at 0xE1.  CP437's glyphs for bytes 0xC0–0xFF (except that
    0xE1) are all box-drawing / Greek / math symbols that never occur in a
    genuine SID name.  So a corrupted string — CP437's rendering of Latin-1
    accented bytes — encodes back to a byte in 0xC0–0xFF (≠0xE1), while a
    correctly-decoded name (``Hülsbeck``, ``Straße``, ``café``) never does.  A
    name with genuine Unicode (emoji, real UTF-8) isn't cp437-encodable → skipped.
    """
    ext = os.path.splitext((track.get("path") or "").lower())[1]
    if ext not in _SID_EXTS:
        return False
    for key in ("title", "artist", "album", "album_artist", "comment"):
        v = track.get(key)
        if not isinstance(v, str) or not v or v.isascii():
            continue
        try:
            raw = v.encode("cp437")
        except UnicodeEncodeError:
            continue                       # genuine Unicode — not a CP437 artifact
        # 0xE1 = CP437 'ß', the one legitimate Latin-1 letter in this byte range.
        if any(0xC0 <= b <= 0xFF and b != 0xE1 for b in raw):
            return True
    return False


def _needs_repair(track: dict) -> bool:
    """A track is a repair candidate if its text was U+FFFD-garbled, it is an
    AHX/HVL track with a mis-extracted title (magic / mangled-path / empty), or
    it is a SID track whose accented header text was CP437-mis-decoded."""
    return (_has_replacement_char(track)
            or _has_bad_tracker_title(track)
            or _has_cp437_corruption(track))


def find_corrupt_tracks(*, tracker_only: bool = False) -> list[dict]:
    """Walk the in-memory store and return candidate track dicts.

    ``tracker_only`` filters to extensions in :data:`TRACKER_LIKE_EXTS`
    so the operator can avoid network I/O on FLAC / DSD shares that the
    decoder fix wouldn't help anyway.
    """
    from soniqboom.core.store import get_store

    store = get_store()
    out: list[dict] = []
    for t in store.all_tracks():
        if not _needs_repair(t):
            continue
        if tracker_only:
            ext = os.path.splitext((t.get("path") or "").lower())[1]
            if ext not in TRACKER_LIKE_EXTS:
                continue
        out.append(t)
    return out


# Formats whose defect is detectable from a lone re-extracted file (no archive
# sibling context needed): YM's corruption check reads only the file header.
# Sonix "partial" needs the archive's Instruments/ dir, which a lone temp-file
# re-extract can't see — those backfill via the play-time path instead.
_DEFECT_BACKFILL_FORMATS = frozenset({"YM"})


# Console-rip formats whose GAME the extractor reads from the header into the
# album (``album_source="tag"``): SPC ID666, NSF / NSFe, GBS, VGM / VGZ GD3,
# and the PSF family's ``game=`` tag (PSF / PSF2 / SSF / DSF / USF / GSF / 2SF
# / NCSF and their mini- files — ``metadata._PSF_EXTS``, and a Dreamcast
# ``.dsf``, see ``find_album_backfill_candidates``), which an older version
# stored as the album without that label.
# Candidates come from the store's format index (keyed on the lower-cased
# ``FORMAT_NAMES`` value of each extension: "NSFe" → "nsfe"), then the path
# extension is checked as before — a few thousand tracks, not the library.
_ALBUM_BACKFILL_EXTS = frozenset({
    ".nsf", ".nsfe", ".spc", ".gbs", ".vgm", ".vgz",
    ".psf", ".minipsf", ".psf2", ".minipsf2", ".usf", ".miniusf",
    ".gsf", ".minigsf", ".2sf", ".mini2sf", ".ssf", ".minissf",
    ".minidsf", ".ncsf", ".minincsf",
})
# Persisted marker: the one-time header-game backfill has completed.
ALBUM_BACKFILL_CONFIG_KEY = "game_album_backfill_v1"
# Persisted marker: the ``scan_root_hash`` of every REMOTE scan root whose
# one-time header-game backfill has completed (``run_remote_album_backfill``).
ALBUM_BACKFILL_ROOTS_CONFIG_KEY = "game_album_backfill_roots"
# Error reasons that mean "the file isn't reachable right now" (an unmounted
# volume, a root that went offline, a share that dropped mid-run) rather than
# "this file can't be read".
_UNREACHABLE_REASONS = frozenset({"local-missing", "local-offline", "local-io",
                                  "read-incomplete", "zip-missing", "remote-download",
                                  "remote-no-source", "remote-archive-extract-failed"})
# Ids the one-time backfill already re-extracted in this process (successfully,
# or with a terminal error) while the run as a whole stayed pending because
# other candidates were unreachable — later runs skip them, so a deferred
# backfill costs nothing per scan until the missing source is back.
_backfill_settled: set[str] = set()
# Persisted: how many one-time backfill runs found each id unreachable (only
# ids not read yet).  After ``_MAX_UNREACHABLE_RUNS`` — counted across
# restarts, as an install without an enrichment index runs the backfill only
# at startup — an id is settled too, so a file that never comes back (a
# corrupt archive member, a file gone from a share) can't keep a one-time
# backfill pending — and its share re-read — forever.  A source marked
# offline is deferred instead, uncounted.
ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY = "game_album_backfill_attempts"
_MAX_UNREACHABLE_RUNS = 3
# A failed run counts toward giving up only this long after the id's last
# counted one: runs come as often as scans end (the folder watcher during a
# copy: seconds), and "given up" should mean "unreachable for hours".
_UNREACHABLE_INTERVAL_S = 3600.0
# Persisted: LOCAL music folders that were away when the local backfill was
# otherwise done (marker written) — read once they are back.
ALBUM_BACKFILL_WAITING_CONFIG_KEY = "game_album_backfill_waiting_roots"


def _attempt(v) -> list:
    """A persisted attempt entry as ``[count, time of the last counted run]``
    (a bare count from an earlier version counts as long ago)."""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return [int(v[0]), float(v[1])]
    return [int(v), 0.0]


def _settle(ids: list[str], unreachable: set) -> set:
    """Settle this run's ``ids`` (``_backfill_settled``): every one read, and
    every unreachable one on its ``_MAX_UNREACHABLE_RUNS``-th counted run —
    runs at least ``_UNREACHABLE_INTERVAL_S`` apart (a whole music folder
    that is gone is deferred before a run, uncounted —
    ``_offline_local_roots``).  Returns the unreachable ids still pending.
    Runs on the loop."""
    from soniqboom.core.store import get_store
    store = get_store()
    got = store.get_config(ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY)
    before = {k: _attempt(v) for k, v in got.items()} if isinstance(got, dict) else {}
    runs = {k: v for k, v in before.items() if k in store._tracks}   # gone tracks drop
    now = time.time()
    pending = set()
    for i in unreachable:
        if i not in store._tracks:
            continue
        n, last = runs.get(i, [0, 0.0])
        if now - last >= _UNREACHABLE_INTERVAL_S:
            n, last = n + 1, now
        runs[i] = [n, last]
        if n < _MAX_UNREACHABLE_RUNS:
            pending.add(i)
    for i in ids:
        if i not in unreachable:
            runs.pop(i, None)
    if runs != before:
        store.set_config(ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY, runs)
    _backfill_settled.update(i for i in ids if i not in pending)
    return pending


def _count_pending_settle() -> None:
    global _pending_settles
    _pending_settles += 1


def _forget_attempts(done) -> None:
    """Drop the persisted attempt counts of every track ``done(track)`` says
    belongs to a finished backfill (and of tracks gone from the library)."""
    from soniqboom.core.store import get_store
    store = get_store()
    got = store.get_config(ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY)
    if isinstance(got, dict) and got:
        tracks = store._tracks
        keep = {k: v for k, v in got.items()
                if (t := tracks.get(k)) is not None and not done(t)}
        if keep != got:
            store.set_config(ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY, keep)


_LOCAL_PROBE_TIMEOUT = 4.0
# The folder probes' own pool — never the default one (log writes, local
# streaming) nor ``data._probe_executor`` (the folder-status and stream
# checks).  A probe stuck on a hung mount keeps its thread; the next probe of
# that folder is not started while it is (``_probe_inflight``), so at most
# one thread per stuck folder is held.
_probe_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8,
                                                    thread_name_prefix="backfillprobe")
_probe_inflight: dict[str, "asyncio.Future"] = {}


def _local_root_present(path: str) -> bool:
    """Is local music folder ``path`` there — a directory with anything in
    it?  An unplugged drive's mount point (or a container's bind mount of
    one) can stay behind as an empty directory."""
    try:
        with os.scandir(path) as it:
            return next(it, None) is not None
    except OSError:
        return False


async def _offline_local_roots(store) -> set[str]:
    """The LOCAL music folders that are not there right now
    (``_local_root_present``, each bounded by ``_LOCAL_PROBE_TIMEOUT`` — a
    stalled mount counts as gone), so their files are deferred
    (``_defer_offline_roots``) instead of counted as unreachable.  Writes no
    status: a network share's is the share monitor's, and one slow probe
    must not mark a folder unavailable for the UI."""
    try:
        dirs = [d["path"] for d in store.list_scan_dirs()
                if d.get("path") and not _is_remote(d["path"])
                and d.get("status") != "unavailable"]
    except Exception:                                   # noqa: BLE001
        return set()
    loop = asyncio.get_running_loop()

    async def probe(p: str) -> tuple[str, bool]:
        # A probe of this folder already running (another caller's, or one
        # stuck on a hung mount) is waited on, not started again.
        fut = _probe_inflight.get(p)
        if fut is None or fut.done():
            fut = loop.run_in_executor(_probe_pool, _local_root_present, p)
            _probe_inflight[p] = fut
        try:
            ok = await asyncio.wait_for(asyncio.shield(fut), timeout=_LOCAL_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            ok = False
        return p, ok
    return {p for p, ok in await asyncio.gather(*(probe(p) for p in dirs)) if not ok}


# Tag-bearing formats whose GAME tag the extractor reads (ID3 TXXX:GAME — in
# an AIFF's ID3 chunk too — a Vorbis GAME comment, an MP4 ``----:GAME`` atom,
# APEv2 GAME).
_GAME_TAG_EXTS = frozenset({".mp3", ".aiff", ".aif", ".flac", ".m4a", ".mp4",
                            ".ogg", ".opus", ".wv", ".mpc"})
# Track ids whose re-extract in the running task may change ONLY the game
# (``start_repair(game_only_ids=…)``, reset by every start): a GAME-tag read
# of a modern file must not rewrite its other fields.
_game_only_ids: frozenset[str] = frozenset()


async def find_game_tag_candidates(*, include_remote: bool = True) -> list[dict]:
    """Modern (non-retro) files whose GAME tag "Read game names" re-reads —
    an incremental scan skips unchanged files, so a GAME tag the file had
    before this field existed isn't seen otherwise.  Never a track whose game
    the user edited.  Path order.

    Walks only the non-retro format buckets of the store's format index, in
    chunks that yield to the loop; the one sort by path holds the loop for
    ~30 ms at 70K candidates."""
    from soniqboom.core.retro import is_retro_format
    from soniqboom.core.store import get_store
    store = get_store()
    tracks = store._tracks
    ids: list[str] = []
    for bucket in list(store._tag_format.values()):
        some = next(iter(bucket), None)
        t = tracks.get(some) if some is not None else None
        if t is None or not is_retro_format(t.get("format")):
            ids.extend(bucket)
    keyed: list[tuple[str, str]] = []
    for i in range(0, len(ids), 5000):
        for tid in ids[i:i + 5000]:
            t = tracks.get(tid)
            if t is None or is_retro_format(t.get("format")):
                continue
            path = t.get("path") or ""
            member = path.rsplit("::", 1)[-1]
            if os.path.splitext(member.lower())[1] not in _GAME_TAG_EXTS:
                continue
            ue = t.get("user_edited")
            if isinstance(ue, list) and "game" in ue:
                continue
            if not include_remote and _is_remote(path):
                continue
            keyed.append((path, tid))
        await asyncio.sleep(0)
    keyed.sort()
    return [t for _p, tid in keyed if (t := tracks.get(tid)) is not None]


def find_album_backfill_candidates(*, include_remote: bool = True) -> list[dict]:
    """Tracks eligible for the header-game album backfill re-extract.

    Console rips indexed before the extractor read the game name keep an
    empty (or folder / Modland-derived) album, because an incremental scan
    skips unchanged files.  A re-extract fills it from the header
    (``album_source="tag"``); ``_changed_fields`` lets the real header game
    replace a derived album and keeps the derived one when the header has
    none (or only the 32-byte cut of an SPC game Modland completed), so a
    re-run is idempotent.  Never a track whose album the user edited, nor one
    that already carries the header's game.

    O(console-rip buckets) via the format index — no pass over every track
    on the loop (~150 ms at 263K tracks before).  Path order, so files of one
    folder / archive are read together."""
    from soniqboom.core.metadata import FORMAT_NAMES
    from soniqboom.core.store import get_store

    store = get_store()
    tracks = store._tracks
    fmt_keys = {FORMAT_NAMES.get(e, e.lstrip(".")).lower() for e in _ALBUM_BACKFILL_EXTS}
    out: list[dict] = []
    for tid in {tid for f in fmt_keys for tid in store._tag_format.get(f, ())}:
        t = tracks.get(tid)
        if t is None:
            continue
        path = t.get("path") or ""
        ext = os.path.splitext(path.lower())[1]
        # ``.dsf`` is also Sony's DSD audio: only a Dreamcast PSF rip is one.
        if ext not in _ALBUM_BACKFILL_EXTS and not (
                ext == ".dsf" and t.get("format") == "DSF (Dreamcast)"):
            continue
        if t.get("album_source") == "tag":
            continue
        ue = t.get("user_edited")
        if isinstance(ue, list) and "album" in ue and "game_by_tag" in t:
            continue        # (typed before the header name was recorded: read it once)
        if not include_remote and _is_remote(path):
            continue
        out.append(t)
    out.sort(key=lambda t: t.get("path") or "")
    return out


def _defer_offline_roots(store, candidates: list[dict],
                         offline: frozenset[str] | set[str] = frozenset(),
                         roots_out: set | None = None,
                         ) -> tuple[list[dict], int]:
    """``(reachable, deferred_count)``: candidates under a scan root whose
    CACHED status is ``unavailable``, or whose path is in ``offline`` (a
    probe's verdict, ``_offline_local_roots``), are held back — their root
    paths added to ``roots_out``.  Matched on the track's ``scan_root_hash``,
    else on the longest root path prefix of the outer (pre-``::``) path.  No
    reachability probe and no ``stat`` here — a stat on a dead mount can hang
    the event loop."""
    try:
        dirs = [dict(d, status="unavailable") if d.get("path") in offline else d
                for d in store.list_scan_dirs() if d.get("path")]
    except Exception:                                   # noqa: BLE001
        return candidates, 0
    if not any(d.get("status") == "unavailable" for d in dirs):
        return candidates, 0
    by_hash = {d["path_hash"]: d for d in dirs if d.get("path_hash")}
    longest_first = sorted(dirs, key=lambda d: len(d["path"]), reverse=True)
    keep: list[dict] = []
    deferred = 0
    for t in candidates:
        sd = by_hash.get(t.get("scan_root_hash") or "")
        if sd is None:
            outer = (t.get("path") or "").split("::", 1)[0]
            sd = next((d for d in longest_first
                       if outer == d["path"]
                       or outer.startswith(d["path"].rstrip("/") + "/")), None)
        if sd is not None and sd.get("status") == "unavailable":
            deferred += 1
            if roots_out is not None:
                roots_out.add(sd["path"])
        else:
            keep.append(t)
    return keep, deferred


def _root_path(store, t: dict) -> str | None:
    """The music folder (scan root path) track ``t`` lies in — matched like
    ``_defer_offline_roots``."""
    dirs = [d for d in store.list_scan_dirs() if d.get("path")]
    for d in dirs:
        if d.get("path_hash") and d["path_hash"] == t.get("scan_root_hash"):
            return d["path"]
    outer = (t.get("path") or "").split("::", 1)[0]
    for d in sorted(dirs, key=lambda d: len(d["path"]), reverse=True):
        if outer == d["path"] or outer.startswith(d["path"].rstrip("/") + "/"):
            return d["path"]
    return None


def _waiting_roots(store) -> list[str]:
    v = store.get_config(ALBUM_BACKFILL_WAITING_CONFIG_KEY)
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _finish_local_backfill(store, waiting: set) -> None:
    """The local one-time backfill is done but for the music folders that
    are away (``waiting``): write its marker, remember those folders (their
    tracks are read once they are back — ``run_album_backfill_once``), and
    drop the local attempt counts and settled ids."""
    store.set_config(ALBUM_BACKFILL_CONFIG_KEY, True)
    if sorted(waiting) != _waiting_roots(store):
        store.set_config(ALBUM_BACKFILL_WAITING_CONFIG_KEY, sorted(waiting))
    _forget_attempts(lambda t: not _is_remote(t.get("path") or ""))
    _backfill_settled.difference_update(
        [i for i in _backfill_settled
         if (t := store._tracks.get(i)) is None or not _is_remote(t.get("path") or "")])


async def run_album_backfill_once() -> bool:
    """Start the header-game backfill once per install (config marker
    ``ALBUM_BACKFILL_CONFIG_KEY``), for LOCAL files only — never network I/O
    at startup.  Remote shares get their own one-time pass once a scan of the
    share completes (``run_remote_album_backfill``), or through Admin →
    Metadata → Read game names.  Returns True when a run was started.  No-op
    while another repair runs (the next trigger retries).

    Tracks under a music folder that is away (marked offline, or gone /
    empty — ``_offline_local_roots``) are deferred; once every other
    candidate was read — a file that could not be reached is retried, and
    given up after ``_MAX_UNREACHABLE_RUNS`` (``_settle``) — the marker is
    written and the folders away are remembered
    (``ALBUM_BACKFILL_WAITING_CONFIG_KEY``): later passes read just their
    tracks once they are back.  Retries read only the tracks not yet read in
    this process (``_backfill_settled``)."""
    from soniqboom.core.store import get_store

    store = get_store()
    done = bool(store.get_config(ALBUM_BACKFILL_CONFIG_KEY))
    waiting = _waiting_roots(store)
    if (done and not waiting) or _busy():
        return False
    offline = await _offline_local_roots(store)
    if _busy():
        return False
    candidates = [t for t in find_album_backfill_candidates(include_remote=False)
                  if t["id"] not in _backfill_settled]
    if done:                                   # only the folders that were away
        candidates = [t for t in candidates if _root_path(store, t) in waiting]
    away: set = set()
    candidates, deferred = _defer_offline_roots(store, candidates, offline, away)
    if not candidates:
        if deferred:
            log.debug("Header-game album backfill: %d track(s) deferred until "
                      "their music folder is back", deferred)
        _finish_local_backfill(store, away)
        return False
    if not await start_repair(candidates, kind="game-names"):
        return False
    task, prog = _task, _progress            # this run's (the globals move on)
    ids = [t["id"] for t in candidates]

    def _done(t: asyncio.Task) -> None:
        global _pending_settles
        _pending_settles -= 1
        if t.cancelled() or t.exception() is not None or prog.cancelled:
            return
        unreachable = _settle(ids, prog.unreachable_ids)
        if unreachable:
            log.info("Header-game album backfill: %d of %d track(s) updated; "
                     "%d unreachable — retried", prog.repaired, prog.total,
                     len(unreachable))
            return
        _finish_local_backfill(get_store(), away)
        log.info("Header-game album backfill: %d of %d track(s) updated%s",
                 prog.repaired, prog.total,
                 f"; {deferred} in a music folder that is away — read once it is back"
                 if deferred else "")
    if task is not None:
        _count_pending_settle()
        task.add_done_callback(_done)
    return True


def _backfilled_roots(store) -> list[str]:
    v = store.get_config(ALBUM_BACKFILL_ROOTS_CONFIG_KEY)
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def mark_remote_album_backfill_done(root_hash: str) -> None:
    """Record that remote scan root ``root_hash`` needs no (more) header-game
    backfill.  Idempotent."""
    from soniqboom.core.store import get_store
    store = get_store()
    done = _backfilled_roots(store)
    if root_hash not in done:
        store.set_config(ALBUM_BACKFILL_ROOTS_CONFIG_KEY, [*done, root_hash])
    _forget_attempts(lambda t: t.get("scan_root_hash") == root_hash
                     and _is_remote(t.get("path") or ""))


def remote_album_backfill_done(root_hash: str) -> bool:
    """True once the remote scan root ``root_hash`` had its one-time
    header-game backfill.  One config read."""
    from soniqboom.core.store import get_store
    return root_hash in _backfilled_roots(get_store())


async def run_remote_album_backfill(root_hash: str) -> bool | None:
    """The header-game backfill for the console rips (SPC/NSF/NSFe/GBS/VGM/
    VGZ) of ONE remote scan root, once per root (config marker
    ``ALBUM_BACKFILL_ROOTS_CONFIG_KEY``): a remote rescan skips unchanged
    files, so rips indexed before the extractor read the game name would
    otherwise keep an empty album until someone ran Admin → Metadata → Read
    game names.  Called after a scan of that root completed (it is
    reachable); reads just its candidates — usually a handful, each archive
    once (path order) — on the ``scan`` lane.

    Returns True when a run was started or nothing was left to do (the root
    is marked done), None when it must wait (another repair runs, or the root
    is marked offline) — the caller retries on a later scan of that root.
    The marker is written once every candidate was read: a file that could
    not be reached (``_UNREACHABLE_REASONS``) or a cancelled / failed run
    leaves the root pending, retrying just the unread files — a file is
    given up after ``_MAX_UNREACHABLE_RUNS`` counted runs (``_settle``).
    Must run on the loop."""
    from soniqboom.core.store import get_store

    store = get_store()
    if root_hash in _backfilled_roots(store):
        return True
    if _busy():
        return None
    candidates = [t for t in find_album_backfill_candidates(include_remote=True)
                  if t.get("scan_root_hash") == root_hash
                  and _is_remote(t.get("path") or "")
                  and t["id"] not in _backfill_settled]
    candidates, deferred = _defer_offline_roots(store, candidates)
    if deferred:
        return None
    if not candidates:
        mark_remote_album_backfill_done(root_hash)
        return True
    if not await start_repair(candidates, kind="game-names"):
        return None
    task, prog = _task, _progress            # this run's (the globals move on)
    ids = [t["id"] for t in candidates]

    def _done(t: asyncio.Task) -> None:
        global _pending_settles
        _pending_settles -= 1
        if t.cancelled() or t.exception() is not None or prog.cancelled:
            return
        unreachable = _settle(ids, prog.unreachable_ids)
        if unreachable:
            log.info("Header-game album backfill of a remote share: %d of %d "
                     "track(s) updated; %d unreachable — retried on its next "
                     "scan", prog.repaired, prog.total, len(unreachable))
            return
        mark_remote_album_backfill_done(root_hash)
        log.info("Header-game album backfill of a remote share: %d of %d "
                 "track(s) updated", prog.repaired, prog.total)
    if task is not None:
        _count_pending_settle()
        task.add_done_callback(_done)
    return True


def find_defect_backfill_candidates() -> list[dict]:
    """Tracks eligible for a track-health-defect backfill re-extract.

    Only formats in :data:`_DEFECT_BACKFILL_FORMATS` (currently ``YM``): a
    re-extract runs the same decodability check the play path uses and stamps
    ``defect="corrupt"`` on the undecodable ones.  Already-defective tracks are
    included too so a re-run is idempotent (no change → no store write).
    """
    from soniqboom.core.store import get_store

    store = get_store()
    return [t for t in store.all_tracks()
            if t.get("format") in _DEFECT_BACKFILL_FORMATS]


# ── Per-track re-extraction ──────────────────────────────────────────────────

# Fields the decoder fix can actually improve.  Restricted to the
# text fields the tracker / chiptune extractors run through
# ``_decode_tracker_str``: title, artist, album / album_artist
# (filled from copyright-line parsing), composer / comment / label
# (occasional secondary text headers), year (parsed from copyright
# text), and ``instruments`` (tracker per-instrument name list).
#
# We deliberately do NOT overwrite numeric audio fields
# (``duration``, ``bitrate``, ``sample_rate``, ``channels`` …).
# The decoder fix can't affect them, and rewriting them with a
# fresh extraction risks reverting any user / scanner-side
# corrections that landed after the original ingest.
_REPAIRABLE_FIELDS: tuple[str, ...] = (
    "title", "artist", "album_artist", "album",
    "year",
    "composer", "comment", "label",
    # A modern file's GAME tag (a retro track's game follows its album —
    # ``store.game_follow``), and the header's own game name.
    "game", "game_by_tag",
    "instruments",
    # Track-health defect — re-extraction newly produces this (undecodable YM,
    # Sonix missing instruments).  Included so a re-extract backfills the badge.
    "defect", "defect_detail",
    # A multi-tune file's default tune (SID header start song, SNDH ``!#``),
    # which older scans didn't record.
    "start_subsong",
)


def _changed_fields(old: dict, new: dict) -> dict:
    """Return a sub-dict of *new* whose values differ from *old*.

    Only considers keys in :data:`_REPAIRABLE_FIELDS`.  The point is to
    avoid emitting a no-op AOF record (and busting indexes) for files
    where the fixed decoder produced the same string anyway.
    """
    out: dict = {}
    edited = old.get("user_edited")
    edited = set(edited) if isinstance(edited, list) else set()
    for k in _REPAIRABLE_FIELDS:
        if k not in new:
            continue
        # A store-only hand edit (``user_edited``) always wins over whatever
        # the re-extract read from the file.
        if k in edited:
            continue
        # A field the stored track predates (no key — e.g. ``game`` on a
        # track indexed before it existed), re-extracted empty, is no change.
        if k not in old and new[k] in (None, "", []):
            continue
        if old.get(k) != new[k]:
            # An album an enrichment pass derived (Modland game dir / file
            # name, folder name — ``album_source`` set, not "tag") is kept
            # unless the re-extract now finds a REAL album in the file, which
            # wins (its provenance is written along with it, below).
            if (k == "album" and old.get("album_source") not in (None, "", "tag")
                    and not (new.get("album") or "").strip()):
                continue
            # A derived album (the Modland apply, the song database, the
            # folder name) that completes this SPC's 32-byte-cut header game
            # ("… The World Wa" → "… The World Warrior"): re-reading the cut
            # name — also when the full xid6 name could not be read — must not
            # undo it (the next apply would only redo it).
            if (k == "album" and old.get("album_source") in (
                    "modland", "modland-filename", "folder", "songdb")
                    and (not old.get("file_md5")
                         or new.get("file_md5") in (None, old["file_md5"]))
                    and spc_name_completes(old.get("album") or "", new.get("album") or "",
                                           old.get("format") or new.get("format"))):
                continue
            # Likewise an artist the Modland join filled (the exact credit of
            # the track's ``scene_path``) when the file itself names nobody —
            # clearing it would undo the enrichment and, via the duplicate-key
            # change, trigger a library-wide duplicate recompute.
            if k == "artist" and modland_artist_kept(old, new, absent_md5_is_same=True):
                continue
            # Likewise an artist / label the song database filled
            # (``songdb_fields``) while the file still names nobody.
            if k in ("artist", "label") and songdb_fill_kept(
                    old, new, k, absent_md5_is_same=True):
                continue
            # A context-less re-extract (a lone temp file) can't see an
            # archive's missing-instrument state, so it must never CLEAR an
            # existing defect to None — only set or upgrade one.  (A full
            # re-scan, which has the archive, is what clears a stale defect.)
            if k in ("defect", "defect_detail") and new[k] is None:
                continue
            # A game derived from the album (``game_source`` set) stays when
            # the file carries no GAME tag of its own.
            if (k == "game" and old.get("game_source") not in (None, "")
                    and not (new[k] or "").strip()):
                continue
            # A provenance-stamped year (Demozoo canonical backfill, or a
            # deliberate user edit) outranks whatever the re-extract read from
            # the file — the file never carried the right year to begin with.
            if k == "year" and old.get("year_source") in ("demozoo", "user"):
                continue
            # A song-database year filled a MISSING year: kept while the file
            # still has none (a year the file now carries wins, below).
            if (k == "year" and old.get("year_source") == "songdb"
                    and new[k] is None
                    and new.get("file_md5") in (None, old.get("file_md5"))):
                continue
            out[k] = new[k]
    # A written album carries the re-extract's provenance ("tag" for a header
    # game name, None for an ordinary tag / no album) so a stale derived
    # ``album_source`` never labels a real tag — also when the value is
    # unchanged (a folder guess that happens to equal the real tag must not
    # stay labelled "folder", or switching that option off would clear it).
    # A newly recorded default tune: the track's duration is that tune's own
    # length when the re-extract knew it (HVSC), as a scan would store it.
    if (out.get("start_subsong") and new.get("hvsc_lengths")
            and new.get("duration") and "duration" not in edited):
        out["duration"] = new["duration"]
    # A GAME tag read from the file is the file's own game.
    if (out.get("game") or "").strip():
        out["game_source"] = None
    # A written field is the file's own now: it leaves the song database's
    # provenance (a year the file carries replaces the one it filled).
    if "year" in out and old.get("year_source") == "songdb":
        out["year_source"] = None
        out["year_file"] = None
    sf = old.get("songdb_fields") or []
    if any(f in out for f in sf):
        out["songdb_fields"] = [f for f in sf if f not in out] or None
    if "album" in out and new.get("album_source") != old.get("album_source"):
        out["album_source"] = new.get("album_source")
    elif ("album" in new and "album" not in edited
          and (new.get("album") or "").strip()
          and new.get("album") == old.get("album")
          and old.get("album_source") not in (None, "", "tag")
          and new.get("album_source") != old.get("album_source")):
        out["album_source"] = new.get("album_source")
    elif ("album" not in edited and new.get("album_source") == "tag"
          and (new.get("album") or "").strip()
          and new.get("album") == old.get("album")
          and old.get("album_source") in (None, "")):
        # The header's game, stored as the album before its provenance was
        # recorded (a PSF ``game=`` tag): labelled the file's own now, so the
        # game follows it (``store.game_follow``).
        out["album_source"] = "tag"
    return out


def _re_extract_local_sync(
    path_str: str, track_id: str,
) -> tuple[dict | None, str | None]:
    """Run mutagen / format extractor on a local path.  Sync, blocking.

    Returns ``(extracted_dict, error_reason)``.  Exactly one of the two
    is non-None.  Caller filters the dict down to changed fields.

    Handles three local-style paths:

      * Plain file on disk: ``/Volumes/Music/foo.sid``
      * Zip-contained virtual path: ``/path/a.zip::inner.it``
      * Nested-zip virtual path: ``/path/a.zip::b.zip::track.s3m``

    The previous version called ``Path(path).exists()`` first and
    returned silently when the path didn't exist on the filesystem —
    which made every zip-virtual path look like a missing file (869
    of 871 errors from the first repair run were exactly this case).
    """
    from soniqboom.core.metadata import extract

    # Zip-virtual path: delegate to the scanner's zip extractor which
    # peels off ``::`` segments, reads the innermost member into a
    # tempfile, and calls ``extract()`` on that.
    if "::" in path_str:
        try:
            from soniqboom.core.scanner import _extract_from_zip
            meta = _extract_from_zip(path_str, track_id)
            d = meta.model_dump()
            # ``_extract_from_zip`` already rewrites ``path`` to the
            # virtual path, so we don't need to fix it up here.
            return d, None
        except FileNotFoundError as exc:
            return None, f"zip-missing: {exc}"
        except Exception as exc:
            from soniqboom.core.metadata import _is_io_error
            if _is_io_error(exc):
                return None, f"{_local_io_reason(exc)}: {type(exc).__name__}: {exc}"
            log.warning("Repair: zip extract failed for %s: %s", path_str, exc)
            return None, f"zip-error: {type(exc).__name__}: {exc}"

    # Plain file on disk.  One that can't be read (permissions, an I/O error
    # at any point — ``strict_io``) is an error, never the extractor's stub
    # (the file name as title, no artist / game), which would replace stored
    # fields.  A final one for the run: a file that stays unreadable must not
    # keep the one-time backfill pending (``_UNREACHABLE_REASONS``).
    from soniqboom.core.metadata import _is_io_error
    p = Path(path_str)
    try:
        if not p.exists():
            # A drive mounted inside a music folder can be gone while the
            # folder is there: the file's own folder missing or empty is the
            # volume gone (``local-offline``), not a file gone.
            return None, ("local-missing" if _local_root_present(str(p.parent))
                          else "local-offline")
        with open(p, "rb") as fh:
            fh.read(1)
        meta = extract(p, track_id, strict_io=True)
    except Exception as exc:
        if _is_io_error(exc):
            return None, f"{_local_io_reason(exc)}: {type(exc).__name__}: {exc}"
        log.warning("Repair: local extract failed for %s: %s", path_str, exc)
        return None, f"local-error: {type(exc).__name__}: {exc}"
    return meta.model_dump(), None


# errnos of a volume that went away (a network mount gone stale, a drive
# pulled) rather than of a file that can't be read: retried like an offline
# source (``_UNREACHABLE_REASONS``).
_OFFLINE_ERRNOS = frozenset(getattr(errno, n) for n in (
    "ESTALE", "ENOTCONN", "ETIMEDOUT", "EHOSTDOWN", "EHOSTUNREACH",
    "ENETDOWN", "ENETUNREACH", "ENXIO", "ENODEV", "ECONNRESET", "ECONNABORTED")
    if hasattr(errno, n))


def _local_io_reason(exc: BaseException) -> str:
    """"local-offline" for an I/O error of a volume that went away,
    "local-io" for EIO (a flaky read), "local-missing" for a file gone
    meanwhile — all retried (``_UNREACHABLE_REASONS``, up to
    ``_MAX_UNREACHABLE_RUNS``) — else "local-unreadable" (a permission or
    other file error — final)."""
    e: BaseException | None = exc
    for _ in range(8):
        if e is None:
            break
        if isinstance(e, OSError) and e.errno in _OFFLINE_ERRNOS:
            return "local-offline"
        if isinstance(e, OSError) and e.errno == errno.EIO:
            return "local-io"
        if isinstance(e, OSError) and e.errno == errno.ENOENT:
            return "local-missing"
        e = e.__cause__ or e.__context__
    return "local-unreadable"


def _re_extract_remote_sync(
    file_data: bytes, remote_path: str, track_id: str,
) -> tuple[dict | None, str | None]:
    """Re-run the extractor against an already-downloaded byte buffer.

    Returns ``(extracted_dict, error_reason)`` — same contract as
    :func:`_re_extract_local_sync`.
    """
    from soniqboom.core.metadata import extract
    from soniqboom.core.scanner import _member_stem

    ext = os.path.splitext(remote_path)[1]
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_data)
            tmp_path = Path(tmp.name)
        meta = extract(tmp_path, track_id)
        d = meta.model_dump()
        d["path"] = remote_path
        d["file_size"] = len(file_data)
        if meta.title == tmp_path.stem:
            # The member's own name (``a.zip::b.mod`` → ``b``), like the scan.
            real_stem = _member_stem(remote_path)
            if real_stem:
                d["title"] = real_stem
        return d, None
    except Exception as exc:
        log.warning("Repair: remote extract failed for %s: %s", remote_path, exc)
        return None, f"remote-error: {type(exc).__name__}: {exc}"
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


# ── Orchestrator ─────────────────────────────────────────────────────────────

async def _broadcast_progress(p: RepairProgress) -> None:
    """Best-effort broadcast — never raises.  The error samples ride only the
    final event (the UI shows them once the run ends; a run over the whole
    library sends thousands of events to every client)."""
    try:
        from soniqboom.api.library import _broadcast
        d = p.to_dict()
        if p.running:
            d.pop("error_samples", None)
        await _broadcast({"event": "repair_progress", **d})
    except Exception as exc:  # pragma: no cover
        log.debug("repair_progress broadcast failed: %s", exc)


async def _process_local(t: dict) -> tuple[bool, bool, str | None]:
    """Re-extract a local track.

    Returns ``(success, applied_change, error_reason)``.  ``success`` is
    False only on hard error; ``applied_change`` says whether we wrote
    anything to the store; ``error_reason`` carries a short tag the
    progress sampler can group on.
    """
    tid = t["id"]
    path_str = t.get("path") or ""

    loop = asyncio.get_running_loop()
    new_meta, err = await loop.run_in_executor(
        None, _re_extract_local_sync, path_str, tid,
    )
    if new_meta is None:
        return False, False, err
    return _apply_re_extract(tid, new_meta)


def _apply_re_extract(tid: str, new_meta: dict) -> tuple[bool, bool, str | None]:
    """Diff a re-extract against the track as it is NOW and write the delta.

    The candidate dict the run started from may be stale: a rescan replaces
    the stored dict and a later edit (``user_edited``) lands on the new one,
    so the comparison baseline is re-read here, on the loop, with no ``await``
    between the check and the write.  A track deleted since is skipped."""
    from soniqboom.core.store import get_store
    cur = get_store().get_track(tid)
    if cur is None:
        return True, False, None
    delta = _changed_fields(cur, new_meta)
    if tid in _game_only_ids:
        delta = {k: v for k, v in delta.items() if k in ("game", "game_source")}
    if not delta:
        return True, False, None
    if _read_lost_data(cur, new_meta, delta):
        return False, False, "read-incomplete"
    if _write_delta(tid, delta):
        return True, True, None
    return True, False, "store-rejected"


# Fields a failed read comes back without: an extractor that swallowed an I/O
# error (several retro ones, AIFF) returns the file name as title and no tags.
_LOSS_TEXT_FIELDS = ("artist", "album", "album_artist", "composer", "game", "game_by_tag")


def _read_lost_data(cur: dict, new: dict, delta: dict) -> bool:
    """Is re-extract ``new`` of stored track ``cur`` a read that failed
    part-way?  An extractor that swallows an I/O error (PSF, GME / VGM, SPC
    xid6, NSFe, AIFF …) returns its stub — the file name as title and no
    artist / album / album artist / composer / game / year — so a result of
    that shape is not applied when ``delta`` would take a stored tag away.
    A read that names anything is applied, also when it empties a field (a
    placeholder artist, a composer Demozoo filled, a removed GAME tag); a
    stored bad tracker title (``_has_bad_tracker_title``) and a value holding
    U+FFFD (the Garbled repair's targets) may become the file name."""
    from soniqboom.core.scanner import _member_stem
    stem = _member_stem(cur.get("path") or "")      # the extract's own fallback
    if (new.get("title") or "") != stem or new.get("year") is not None:
        return False
    if any(isinstance(new.get(k), str) and new[k].strip() for k in _LOSS_TEXT_FIELDS):
        return False
    if _has_bad_tracker_title(cur):
        return False

    def kept(v) -> bool:
        return isinstance(v, str) and bool(v.strip()) and "\ufffd" not in v
    for k in _LOSS_TEXT_FIELDS:
        if k in delta and kept(cur.get(k)) and not (delta[k] or "").strip():
            return True
    if "year" in delta and cur.get("year") is not None and delta["year"] is None:
        return True
    return "title" in delta and kept(cur.get("title")) and delta["title"] == stem


def _write_delta(tid: str, delta: dict) -> bool:
    """Write a re-extract delta and note what kind of fields it touched."""
    from soniqboom.core.store import get_store
    if not get_store().update_track_fields(tid, delta):
        return False
    _progress.changed_ids.add(tid)
    if "title" in delta or "artist" in delta:
        _progress.dup_key_changes += 1
    return True


async def _process_remote(t: dict, source_lookup) -> tuple[bool, bool, str | None]:
    """Re-extract a remote track via FTP/SMB/WebDAV.

    Same ``(success, applied_change, error_reason)`` shape as
    :func:`_process_local`.

    ``source_lookup`` is kept as a parameter for test stub-ability but
    is no longer used — the canonical lookup pattern in the rest of
    the codebase (stream.py / tracks.py / art.py) is
    ``parse_remote_path`` → ``get_source(scan_root)``.  The earlier
    ``find_source_for_path`` prefix-match treated the share/path
    separator (``:``) as part of the URL host+path, so FTP paths of
    the form ``ftp://h/share:/relative`` never matched the
    registered scan-root key ``ftp://h/share``.  This was the
    "remote-no-source" failure for the 2 Suara DSD tracks.
    """
    from soniqboom.core.filesource import parse_remote_path, get_source

    tid = t["id"]
    path_str = t.get("path") or ""

    try:
        scan_root, remote_subpath = parse_remote_path(path_str)
    except ValueError:
        # Not a recognised remote URL — shouldn't reach this branch
        # because the caller already checked _is_remote(), but guard
        # anyway so the repair task doesn't crash on a malformed row.
        return False, False, "remote-malformed-url"

    if not remote_subpath:
        # URL points at the share root, not a file.  No-op.
        return False, False, "remote-share-root-only"

    source = get_source(scan_root)
    if source is None:
        return False, False, "remote-no-source"

    loop = asyncio.get_running_loop()

    if "::" in remote_subpath:
        # Composite remote-archive member (``ftp://…/archive.zip::member``):
        # the ``::`` tail is NOT a real remote file, so handing
        # ``remote_subpath`` whole to ``source.read_file`` would 550/ENOENT
        # (or, if the container name alone resolved, read the raw archive
        # without extracting the member).  Route it through the shared
        # resolver, which partitions ``::`` first, mirrors only the OUTER
        # container into the remote-cache, then extracts the member.  Plain
        # (non-archive) remote paths keep the scan-lane read_file() path
        # below unchanged.
        from soniqboom.core.source_bytes import read_source_bytes
        # ``lane="scan"`` mirrors the plain-remote branch below — a bulk repair
        # pass must borrow the scan pool, not the playback stream pool.
        data = await loop.run_in_executor(
            None, lambda: read_source_bytes(path_str, lane="scan"))
        if data is None:
            return False, False, "remote-archive-extract-failed"
    else:
        # A GAME-tag-only read takes just the tag bytes when they read
        # cleanly (``_tag_window_extract``); anything else reads the file
        # whole.
        if tid in _game_only_ids:
            meta = await _tag_window_extract(source, remote_subpath, path_str, t)
            if meta is not None:
                return _apply_re_extract(tid, meta)
        try:
            # ``lane='scan'`` so this borrows from the scan pool, not the
            # streaming pool — keeps audio playback responsive while the
            # repair churns through hundreds of files.
            data = await loop.run_in_executor(
                None, lambda: source.read_file(remote_subpath, lane="scan"),
            )
        except Exception as exc:
            log.warning("Repair: download failed for %s: %s", path_str, exc)
            return False, False, f"remote-download: {type(exc).__name__}: {exc}"

    new_meta, err = await loop.run_in_executor(
        None, _re_extract_remote_sync, data, path_str, tid,
    )
    if new_meta is None:
        return False, False, err
    return _apply_re_extract(tid, new_meta)


async def _tag_window_extract(source, remote_subpath: str, path_str: str,
                              t: dict) -> dict | None:
    """The re-extract of a network-share file's tag bytes alone
    (``tag_window``), or None when the file must be read whole instead: the
    source has no real range reads (``tag_window.supports_ranges``), the
    format or layout has no window, a range read failed, or the window did
    not read cleanly — no audio stream found (duration 0: junk or a second
    tag past the first), or no GAME tag where the stored track has the
    file's own.  Only its game is applied (``_game_only_ids``)."""
    from soniqboom.core.tag_window import tag_window
    loop = asyncio.get_running_loop()
    ext = os.path.splitext(remote_subpath.lower())[1]
    size = t.get("file_size") or 0
    try:
        data = await loop.run_in_executor(
            None, lambda: tag_window(source, remote_subpath, ext, size))
    except Exception as exc:                            # noqa: BLE001
        log.debug("Repair: tag window of %s failed (%s) — reading it whole",
                  path_str, exc)
        return None
    if data is None:
        return None
    meta, _err = await loop.run_in_executor(
        None, _re_extract_remote_sync, data, path_str, t["id"])
    if meta is None or not (meta.get("duration") or 0) > 0:
        return None
    from soniqboom.core.store import get_store
    cur = get_store().get_track(t["id"]) or t
    if ((cur.get("game") or "").strip() and cur.get("game_source") is None
            and not (meta.get("game") or "").strip()):
        return None
    return meta


def _is_remote(path: str) -> bool:
    return path.startswith(("ftp://", "ftps://", "smb://", "webdav://",
                            "webdavs://", "http://", "https://"))


async def _run_repair(
    candidates: list[dict],
    *,
    cancel_event: asyncio.Event,
    on_progress: Callable[[RepairProgress], Awaitable[None]] | None = None,
    progress_every: int = 10,
) -> None:
    """Inner driver — must be called only from :func:`start_repair`.

    Note: the global ``_progress`` is initialised by :func:`start_repair`
    *before* the task is scheduled — that way callers polling
    ``is_running()`` immediately after ``start_repair`` see the right
    state without having to await the task's first instruction.
    """
    from soniqboom.core.filesource import find_source_for_path

    if on_progress:
        await on_progress(_progress)

    if not candidates:
        _progress.running = False
        _progress.finished_at = time.time()
        if on_progress:
            await on_progress(_progress)
        return

    try:
        for t in candidates:
            if cancel_event.is_set():
                _progress.cancelled = True
                break

            path_str = t.get("path") or ""
            _progress.current_file = os.path.basename(path_str) or path_str

            try:
                if _is_remote(path_str):
                    ok, applied, err = await _process_remote(t, find_source_for_path)
                else:
                    ok, applied, err = await _process_local(t)
            except Exception as exc:
                log.exception("Repair: unexpected error for %s: %s",
                              path_str, exc)
                ok, applied, err = False, False, f"unexpected: {type(exc).__name__}: {exc}"

            if not ok:
                _progress.record_error(path_str, err or "unknown")
                if (err or "").split(":", 1)[0] in _UNREACHABLE_REASONS:
                    _progress.unreachable_ids.add(t.get("id"))
            if applied:
                _progress.repaired += 1
            _progress.processed += 1

            if on_progress and (
                _progress.processed % progress_every == 0
                or _progress.processed == _progress.total
            ):
                await on_progress(_progress)
    finally:
        # This run's progress: once ``running`` is False a new run may replace
        # the global during the awaits below.
        prog = _progress
        prog.running = False
        prog.finished_at = time.time()
        if on_progress:
            await on_progress(prog)
        log.info(
            "Repair finished: total=%d processed=%d repaired=%d errors=%d cancelled=%s",
            prog.total, prog.processed, prog.repaired,
            prog.errors, prog.cancelled,
        )
        # The album aggregation + folder-browse caches are validated by track
        # count, which a field update doesn't move: refresh the changed tracks'
        # browse rows (title, artist, album …) and drop the aggregations, once.
        if prog.changed_ids:
            try:
                from soniqboom.core.folder_album import refresh_album_caches
                await refresh_album_caches(prog.changed_ids)
            except Exception:
                log.debug("Repair: album cache refresh failed", exc_info=True)
        # A changed title/artist alters the duplicate-group signature
        # (``_group_key(title, artist, duration)``).  Without recomputing,
        # the stale ``is_duplicate_primary`` flags keep collapsing
        # now-distinct tracks under duplicate-filtering — e.g. four .hvl
        # modules that were all titled "HVL" stay merged into one row even
        # after they get their real names.  Re-run detection so the flags
        # match the repaired metadata.  (An album-only change can't move the
        # group key, so it doesn't pay for a library-wide recompute.)
        if prog.dup_key_changes > 0:
            try:
                n = await recompute_duplicate_groups_now()
                log.info("Repair: recomputed duplicate groups (%d tracks) after %d re-titles",
                         n, prog.repaired)
            except Exception:
                log.warning("Repair: duplicate-group recompute failed", exc_info=True)


# The fields ``duplicates.compute_duplicate_groups`` reads (group key: title,
# artist/album_artist, duration; primary pick: format, bitrate, added_at).
_DUP_FIELDS = ("id", "title", "artist", "album_artist", "duration", "format",
               "bitrate", "added_at")


async def recompute_duplicate_groups_now() -> int:
    from soniqboom.core.scanner import _dup_lock
    async with _dup_lock():         # never land an older snapshot over a newer re-group
        return await _recompute_duplicate_groups_now_locked()


async def _recompute_duplicate_groups_now_locked() -> int:
    """Recompute every track's duplicate-group annotation in-process and apply
    it to the store.  Used after a repair (changed titles alter the group key)
    and exposed via ``POST /admin/metadata/recompute-duplicates``.

    Deliberately runs ``compute_duplicate_groups`` IN-PROCESS rather than via
    the scanner's ProcessPoolExecutor path — that subprocess hop silently
    no-op'd when invoked from the repair task's ``finally``.  The loop only
    copies the few fields the algorithm reads (in chunks, yielding); the
    O(N log N) grouping runs in a worker thread on that private copy, and the
    annotations are written back on the loop by the shared chunked writer
    (``folder_album.commit_album_updates``: only changed annotations are
    written, in batched AOF records, yielding on a time budget, in batch mode
    for a bulk change — one ``update_track_fields`` per track wrote an AOF
    record each and stalled the loop for hundreds of ms per 1000 changes).
    The changed tracks' folder-browse rows are refreshed (their duplicate
    flags feed the folder listings' duplicate filter).  Returns the number of
    tracks annotated.
    """
    from soniqboom.core.store import get_store
    from soniqboom.core.duplicates import compute_duplicate_groups

    store = get_store()
    tracks = store.all_tracks()
    if not tracks:
        return 0
    snap: list[dict] = []
    for i in range(0, len(tracks), 20_000):
        snap.extend({k: t[k] for k in _DUP_FIELDS if k in t}
                    for t in tracks[i:i + 20_000])
        await asyncio.sleep(0)
    annotations = await asyncio.to_thread(compute_duplicate_groups, snap)
    from soniqboom.core import folder_album
    written: list[str] = []
    await folder_album.commit_album_updates(
        [(tid, {"duplicate_group_id": ann["duplicate_group_id"],
                "format_score": ann["format_score"],
                "is_duplicate_primary": ann["is_duplicate_primary"]}, None)
         for tid, ann in annotations.items()],
        written=written)
    if written:
        await folder_album.refresh_album_caches(written)
    n = len(annotations)
    log.info("Duplicate-group recompute: annotated %d tracks (%d changed)",
             n, len(written))
    return n


async def start_repair(candidates: list[dict], *, game_only_ids=frozenset(),
                       kind: str = "repair") -> bool:
    """Start the repair task in the background.

    Returns False if a repair is already running (the caller should
    cancel first or wait for completion).  True if we kicked off a
    fresh task.

    The global ``_progress`` is initialised *before* ``create_task``
    returns — so callers that immediately poll :func:`is_running` see
    ``True`` rather than racing the task's first ``await``.

    ``game_only_ids``: the candidates whose re-extract may change only the
    game (a modern file's GAME tag — see ``find_game_tag_candidates``).
    ``kind``: ``RepairProgress.kind``.
    """
    global _task, _cancel_event, _progress, _game_only_ids

    if _busy():                     # also while the last run is still finishing
        return False
    _game_only_ids = frozenset(game_only_ids)

    _progress = RepairProgress(
        running=True,
        total=len(candidates),
        started_at=time.time(),
        kind=kind,
    )

    _cancel_event = asyncio.Event()
    _task = asyncio.create_task(
        _run_repair(
            candidates,
            cancel_event=_cancel_event,
            on_progress=_broadcast_progress,
        ),
        name="soniqboom.repair",
    )
    return True
