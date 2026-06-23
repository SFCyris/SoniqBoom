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
auto-fire path is cheap to run after every SID-bearing scan.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)


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


async def apply_hvsc_to_library(*, reload: bool = False) -> dict:
    """Join HVSC durations (by cached MD5) + STIL (by HVSC-relative path) onto
    every SID track.  Returns a stats dict.  No-op (``updated: 0``) when HVSC
    isn't configured."""
    from soniqboom.core.store import get_store
    from soniqboom.core.hvsc import get_hvsc, _file_md5
    hvsc = get_hvsc()
    if not hvsc.is_configured():
        return {"updated": 0, "message": "HVSC is not configured — set the DOCUMENTS path first."}
    if reload:
        hvsc.reload()                       # pick up an updated/auto-detected DB
    store = get_store()
    loop = asyncio.get_event_loop()

    sids = [t for t in store.all_track_metas()
            if str(t.get("format", "")).upper() == "SID"]
    scanned = len(sids)
    sid_track_ids = [t["id"] for t in sids]

    # ── Resolve each track's whole-file MD5 (cached → compute → fetch) ──
    skipped_missing = 0
    skipped_unreadable = 0
    io_sem = asyncio.Semaphore(6)           # bound concurrent file/network reads

    async def _resolve(t: dict):
        nonlocal skipped_missing, skipped_unreadable
        md5 = (t.get("sid_md5") or "").strip().lower() or None
        if md5:
            return t, md5, False            # cached — no I/O, nothing to persist
        path_str = t.get("path") or ""
        if path_str.startswith(("smb://", "ftp://")):
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

    resolved = await asyncio.gather(*[_resolve(t) for t in sids])

    # ── Build patches — idempotent: only include fields that actually change ──
    updates: list[tuple[str, dict]] = []
    correct_durations: dict[str, set[int]] = {}

    for t, md5, newly in resolved:
        patch: dict = {}
        if newly and md5 and md5 != (t.get("sid_md5") or ""):
            patch["sid_md5"] = md5           # cache for next time (one-time backfill)
        durations = hvsc.lookup_durations_by_md5(md5) if md5 else []
        if durations:
            if abs(float(t.get("duration") or 0) - durations[0]) > 0.01:
                patch["duration"] = durations[0]
            if not _durations_equal(t.get("hvsc_lengths"), durations):
                patch["hvsc_lengths"] = durations
            if len(durations) > 1 and t.get("subsongs") != len(durations):
                patch["subsongs"] = len(durations)
            correct_durations[t["id"]] = _ints(durations)
        else:
            # No HVSC entry — keep whatever durations the track already had so
            # its conversion-cache entries survive.
            valid = _ints(t.get("hvsc_lengths") or [])
            if t.get("duration"):
                valid |= _ints([t["duration"]])
            if valid:
                correct_durations[t["id"]] = valid
        key = hvsc.stil_key_for(t.get("path") or "")
        stil = hvsc.lookup_stil_by_relpath(key) if key else None
        if stil and stil.get("text") and stil["text"] != (t.get("stil") or ""):
            patch["stil"] = stil["text"]
        if patch:
            updates.append((t["id"], patch))

    if updates:
        await loop.run_in_executor(
            None, store.update_track_fields_batch, updates,
        )

    # Reconcile the SID conversion cache against the post-apply durations.
    from soniqboom.core.conversion_cache import purge_sid_entries_for
    purged = await purge_sid_entries_for(
        sid_track_ids, keep_duration=correct_durations,
    )

    msg_parts = [f"Updated {len(updates)} of {scanned} SID track(s)."]
    if purged:
        msg_parts.append(f"Purged {purged} stale render(s).")
    if skipped_missing:
        msg_parts.append(f"Skipped {skipped_missing} missing file(s).")
    if skipped_unreadable:
        msg_parts.append(f"Skipped {skipped_unreadable} unreadable file(s).")
    return {
        "updated": len(updates),
        "scanned": scanned,
        "skipped_missing": skipped_missing,
        "skipped_unreadable": skipped_unreadable,
        "message": " ".join(msg_parts),
    }
