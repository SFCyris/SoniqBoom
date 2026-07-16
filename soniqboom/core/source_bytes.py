# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Canonical source-bytes resolver — the fix for the "remote scheme checked
before the archive member" bug class.

Given a path in any of these shapes, return the raw bytes of the file (or the
archive *member*):

    /local/file.mod
    /local/archive.zip::inner.mod            (local archive member)
    ftp://host/share:/dir/file.mod           (plain remote)
    ftp://host/share:/dir/archive.zip::x.mod (COMPOSITE remote-archive member)

The critical property is that the ``::`` archive tail is partitioned **first**,
so a composite remote-archive path fetches only the OUTER container into the
local remote-cache (a cache hit reuses the copy playback already downloaded —
no re-fetch) and THEN extracts the member.  A resolver that instead tested the
remote scheme first would hand the whole ``…archive.zip::member`` string to the
remote fetch (no such remote file) or read the raw container without extracting
the member — the bug this module exists to eliminate.

Lives in ``core`` (not ``api``) so both API handlers and core services
(hvsc_apply, repair, art_backfill, …) can share it without a layering
violation.  Blocking (file I/O + a possible network fetch) — call it via
``run_in_executor`` from async code.  Never raises: returns ``None`` on any
miss so callers fall back gracefully.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_REMOTE_SCHEMES = ("smb://", "ftp://", "http://", "https://",
                   "webdav://", "webdavs://")


def read_source_bytes(path_str: str, *, lane: str = "stream") -> bytes | None:
    """Raw bytes for a local / archive-virtual / remote / composite path.

    ``lane`` selects the remote-cache I/O pool for the OUTER fetch: the default
    ``"stream"`` (playback-priority) suits interactive callers (VU / waveform /
    art / SID on track-load), while bulk BACKGROUND passes (hvsc re-apply,
    metadata repair, cover backfill) MUST pass ``lane="scan"`` so they borrow
    from the scan pool and don't stall playback.  Ignored for local / archive
    paths that don't hit the network.

    Blocking — run in an executor.  Returns ``None`` when the file can't be
    reached or extracted; never raises.
    """
    try:
        outer, sep, rest = path_str.partition("::")
        if outer.startswith(_REMOTE_SCHEMES):
            # Mirror only the OUTER remote file (the module itself, or the
            # archive that contains it) into the local remote-cache first.
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(outer)
            source = get_source(scan_root) if remote_path else None
            if source is None:
                return None
            local = get_cache().fetch(scan_root, remote_path, source, lane=lane)
            if not local:
                return None
            outer = str(local)
        if sep:
            # Archive member (possibly nested zips / LHA / disk image) — the
            # scanner's reader already handles the chain against a LOCAL outer.
            from soniqboom.core.scanner import _read_from_zip_path
            data, _member = _read_from_zip_path(f"{outer}::{rest}")
            return data
        p = Path(outer)
        return p.read_bytes() if p.exists() else None
    except Exception:
        # Non-fatal: callers fall back (waveform → 404, art → placeholder,
        # hvsc → no length, repair → skip).  Keep the traceback at debug so a
        # genuine remote/archive fetch failure stays diagnosable.
        log.debug("read_source_bytes failed for %s", path_str, exc_info=True)
        return None
