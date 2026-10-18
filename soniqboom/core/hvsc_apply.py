# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Apply HVSC data to the whole SID library — the shared re-apply pass.

SID metadata extraction runs in ``ProcessPoolExecutor`` workers, and under
``spawn`` (the macOS default) each worker re-imports a FRESH, unconfigured HVSC
singleton — so scan-time HVSC enrichment inside the worker never runs.  What
the worker DOES reliably do is cache the whole-file MD5 on the track
(``sid_md5``), which needs no HVSC.  So durations + STIL are applied here, in
the MAIN process, by joining that cached MD5 against the (single) HVSC index.

This is called:
  * by the admin "Re-extract SID metadata" endpoint (``reload=True`` to pick up
    an updated HVSC release), and
  * automatically at the end of a scan that saw SID files while HVSC is
    configured (``reload=False``) — so an auto-detected (or pre-configured)
    HVSC actually updates the tracks without the user clicking anything.

It is idempotent: only tracks whose data actually changes are written, so the
auto-fire path is cheap to run after every SID-bearing scan.  A scan passes the
SIDs it added or changed (``ids``); the whole SID library is joined in chunks
of at most ``_SLICE_S`` of loop time each, after the HVSC documents are loaded
in a worker thread.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

from soniqboom.core.filesource import is_remote_path

log = logging.getLogger(__name__)

# Loop time one synchronous slice of the join may take, and the pause after
# it (a timer: work that became ready meanwhile runs before the next slice);
# how many tracks one file-reading batch (no cached MD5) / one conversion-cache
# reconcile call takes.
_SLICE_S = 0.004
_PAUSE_S = 0.0005
_IO_BATCH = 256
_PURGE_BATCH = 2048
_PURGE_PASSES = 4


def _ints(seq) -> set[int]:
    out: set[int] = set()
    for v in seq or []:
        try:
            out.add(int(round(float(v))))
        except (TypeError, ValueError):
            pass
    return out


def _durations_equal(a, b) -> bool:
    a = a or []
    b = b or []
    if len(a) != len(b):
        return False
    return all(abs(float(x) - float(y)) < 0.01 for x, y in zip(a, b))


async def apply_hvsc_to_library(*, reload: bool = False,
                                ids: "set[str] | None" = None) -> dict:
    """Join HVSC durations (by cached MD5) + STIL (by HVSC-relative path) onto
    every SID track — or only the SID tracks in ``ids`` (a scan's own changed
    tracks).  Returns a stats dict.  No-op (``updated: 0``) when HVSC isn't
    configured."""
    from soniqboom.core.store import get_store
    from soniqboom.core.hvsc import get_hvsc, _file_md5
    hvsc = get_hvsc()
    if not hvsc.is_configured():
        return {"updated": 0, "message": "HVSC is not configured — set the DOCUMENTS path first."}
    if reload:
        hvsc.reload()                       # pick up an updated/auto-detected DB
    store = get_store()
    loop = asyncio.get_running_loop()
    # The documents are read and parsed (megabytes; over the share when the
    # DOCUMENTS path is remote) by the first lookup — in a worker thread
    # here, not by the first lookup of the join on the loop.
    preload = getattr(hvsc, "_ensure_loaded", None)
    if callable(preload):
        await loop.run_in_executor(None, preload)

    # Reuse the maintained _tag_format index (keyed lowercase) instead of
    # materializing + scanning all 270k metas — O(|SID bucket|).
    t0 = time.perf_counter()
    bucket = store._tag_format.get("sid", ())
    # (a track no longer stored is skipped by the join below)
    tids = [tid for tid in ids if tid in bucket] if ids is not None else list(bucket)
    sid_track_ids: list[str] = []

    # ── Resolve each track's whole-file MD5 (cached → compute → fetch) ──
    skipped_missing = 0
    skipped_unreadable = 0
    skipped_catalogue = 0
    io_sem = asyncio.Semaphore(6)           # bound concurrent file/network reads

    async def _resolve(t: dict):
        nonlocal skipped_missing, skipped_unreadable, skipped_catalogue
        path_str = t.get("path") or ""
        from soniqboom.core import catalogue
        if catalogue.is_catalogue_path(path_str):
            # A catalogue SID gets its sid_md5 from its first play's merge
            # (core/modland_catalog.py) — never read here.
            skipped_catalogue += 1
            return t, None, False
        if is_remote_path(path_str):
            if "::" in path_str:
                # SID inside a REMOTE archive ("…/archive.zip::member.sid"):
                # route through the shared resolver, which splits the "::" tail
                # BEFORE the remote fetch (fetch outer container → extract
                # member).  ``lane="scan"`` — this is a bulk background re-apply
                # pass and must not compete with playback on the stream lane.
                from soniqboom.core.source_bytes import read_source_bytes
                async with io_sem:
                    data = await loop.run_in_executor(
                        None, lambda: read_source_bytes(path_str, lane="scan"),
                    )
                if data is None:
                    skipped_unreadable += 1
                    return t, None, False
                return t, hashlib.md5(data).hexdigest(), True
            # Plain remote SID — unchanged scan-lane direct read (no "::" tail,
            # no remote-cache write-through).
            from soniqboom.core.filesource import parse_remote_path, get_source
            try:
                scan_root, rel = parse_remote_path(path_str)
                source = get_source(scan_root)
                if source is None:
                    skipped_unreadable += 1
                    return t, None, False
                async with io_sem:
                    data = await loop.run_in_executor(
                        None, lambda: source.read_file(rel, lane="scan"),
                    )
                return t, hashlib.md5(data).hexdigest(), True
            except Exception:
                skipped_unreadable += 1
                return t, None, False
        p = Path(path_str)
        if not p.is_file():
            skipped_missing += 1
            return t, None, False
        async with io_sem:
            try:
                md5 = await loop.run_in_executor(None, _file_md5, p)
            except Exception:
                skipped_unreadable += 1
                return t, None, False
        return t, md5, True

    # ── Build patches — idempotent: only include fields that actually change ──
    updates: list[tuple[str, dict]] = []
    # The MD5 of each track HVSC has lengths for: its valid render durations
    # are looked up again batch by batch at the conversion-cache reconcile
    # below (one set per track held for the whole pass kept the cyclic GC
    # busy on a full-library join).
    hvsc_md5: dict[str, str] = {}

    def _patch(t: dict, md5, newly: bool) -> None:
        patch: dict = {}
        if newly and md5 and md5 != (t.get("sid_md5") or ""):
            patch["sid_md5"] = md5           # cache for next time (one-time backfill)
        durations = hvsc.lookup_durations_by_md5(md5) if md5 else []
        if durations:
            # The track's duration is its DEFAULT tune's (what the bare id
            # plays): tune 1's unless the scan recorded another start song.
            s = t.get("start_subsong")
            want = (durations[s] if isinstance(s, int) and not isinstance(s, bool)
                    and 0 < s < len(durations) and durations[s] else durations[0])
            if abs(float(t.get("duration") or 0) - want) > 0.01:
                patch["duration"] = want
            if not _durations_equal(t.get("hvsc_lengths"), durations):
                patch["hvsc_lengths"] = durations
            if len(durations) > 1 and t.get("subsongs") != len(durations):
                patch["subsongs"] = len(durations)
            hvsc_md5[t["id"]] = md5
        key = hvsc.stil_key_for(t.get("path") or "")
        stil = hvsc.lookup_stil_by_relpath(key) if key else None
        by_md5 = getattr(hvsc, "path_for_md5", None)
        if stil is None and md5 and by_md5 is not None:
            # A SID outside the HVSC tree: its entry by its checksum's HVSC path.
            rel = by_md5(md5)
            stil = hvsc.lookup_stil_by_relpath(rel) if rel else None
        if stil and stil.get("text") and stil["text"] != (t.get("stil") or ""):
            patch["stil"] = stil["text"]
        if patch:
            updates.append((t["id"], patch))

    # A track with a cached MD5 (the common case — the scan workers cache it)
    # is joined right away, from its stored fields, in slices of at most
    # ``_SLICE_S`` of loop time; the rest are read (file / remote / archive
    # member) in bounded batches, then joined.
    needs_io: list[dict] = []
    for tid in tids:
        t = store._tracks.get(tid)
        if t is None:
            continue                        # (removed meanwhile)
        sid_track_ids.append(tid)
        md5 = (t.get("sid_md5") or "").strip().lower() or None
        if md5:
            _patch(t, md5, False)           # cached — no I/O, nothing to persist
        else:
            needs_io.append(store._meta_dict(tid))
        if time.perf_counter() - t0 >= _SLICE_S:
            await asyncio.sleep(_PAUSE_S)
            t0 = time.perf_counter()
    scanned = len(sid_track_ids)
    # Valid durations of a track without HVSC lengths — kept so its
    # conversion-cache entries survive — from its stored fields (which the
    # join doesn't write for it): read at the reconcile below.
    no_lengths: dict[str, tuple] = {}
    for i in range(0, len(needs_io), _IO_BATCH):
        part = needs_io[i:i + _IO_BATCH]
        resolved = await asyncio.gather(*[_resolve(t) for t in part])
        for t, md5, newly in resolved:
            _patch(t, md5, newly)
            if t["id"] not in hvsc_md5:
                # (the copy read before the files: the track as joined)
                no_lengths[t["id"]] = (t.get("hvsc_lengths"), t.get("duration"))
        await asyncio.sleep(_PAUSE_S)

    if updates:
        # On the loop thread (the store has no lock — a write from a worker
        # thread could race a scan's index maintenance), in chunks with yields
        # and batch mode for a bulk change: the shared chunked writer.
        from soniqboom.core.folder_album import commit_album_updates, refresh_album_caches
        written: list[str] = []
        await commit_album_updates([(tid, patch, None) for tid, patch in updates],
                                   written=written)
        # Durations are baked into the folder-browse + aggregation caches,
        # which are validated by track COUNT — a field-only update like this
        # won't invalidate them, so the UI would keep showing the old (default)
        # lengths.  Refresh the patched tracks' rows and drop the aggregation
        # cache (works for the scan auto-fire AND the admin re-extract endpoint).
        try:
            await refresh_album_caches(written)
        except Exception:
            log.debug("HVSC apply: cache refresh failed", exc_info=True)

    # Reconcile the SID conversion cache against the post-apply durations: a
    # track's HVSC lengths, else the durations it already had.  Only the SIDs
    # with an entry in the cache (one sliced pass over its keys), in batches —
    # each purge call walks the cache once.
    from soniqboom.core import conversion_cache as _cc
    from soniqboom.core.conversion_cache import purge_sid_entries_for
    purge_ids = sid_track_ids
    meta = getattr(_cc, "_meta", None)
    if isinstance(meta, dict):
        cached: set[str] = set()
        keys = list(meta)
        for j in range(0, len(keys), 8_192):
            for k in keys[j:j + 8_192]:
                tid, sep, _rest = k.partition("__sub")
                if sep:
                    cached.add(tid)
            await asyncio.sleep(_PAUSE_S)
        del keys
        purge_ids = []
        for j in range(0, len(sid_track_ids), 8_192):
            purge_ids += [tid for tid in sid_track_ids[j:j + 8_192] if tid in cached]
            await asyncio.sleep(_PAUSE_S)
    purged = 0
    # (at most ``_PURGE_PASSES`` walks of the cache, batches of at least
    # ``_PURGE_BATCH``)
    batch = max(_PURGE_BATCH, -(-len(purge_ids) // _PURGE_PASSES))
    for i in range(0, len(purge_ids), batch):
        keep: dict[str, set[int]] = {}
        for tid in purge_ids[i:i + batch]:
            md5 = hvsc_md5.get(tid)
            if md5 is not None:
                keep[tid] = _ints(hvsc.lookup_durations_by_md5(md5))
                continue
            src = no_lengths.get(tid)
            if src is None:
                t = store._tracks.get(tid) or {}
                src = (t.get("hvsc_lengths"), t.get("duration"))
            valid = _ints(src[0] or [])
            if src[1]:
                valid |= _ints([src[1]])
            if valid:
                keep[tid] = valid
        purged += await purge_sid_entries_for(purge_ids[i:i + batch], keep_duration=keep)
        await asyncio.sleep(_PAUSE_S)

    msg_parts = [f"Updated {len(updates)} of {scanned} SID track(s)."]
    if purged:
        msg_parts.append(f"Purged {purged} stale render(s).")
    if skipped_missing:
        msg_parts.append(f"Skipped {skipped_missing} missing file(s).")
    if skipped_unreadable:
        msg_parts.append(f"Skipped {skipped_unreadable} unreadable file(s).")
    if skipped_catalogue:
        msg_parts.append(f"{skipped_catalogue} catalogue SID(s) are matched when first "
                         "played.")
    return {
        "updated": len(updates),
        "scanned": scanned,
        "skipped_missing": skipped_missing,
        "skipped_unreadable": skipped_unreadable,
        "skipped_catalogue": skipped_catalogue,
        "message": " ".join(msg_parts),
    }
