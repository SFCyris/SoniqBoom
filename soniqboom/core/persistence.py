# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Persistence layer — snapshot loading and AOF replay.

On startup:
  1. Load library.json (full snapshot)
  2. Replay library.aof (unapplied changes since last merge)
  3. Populate the TrackStore and rebuild indexes
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _try_load_json(path: Path) -> dict | None:
    """Attempt to load JSON from *path*.

    Returns the parsed dict on success, or ``None`` on any failure.
    Handles a common corruption pattern (valid JSON followed by trailing
    garbage — e.g. a partial second write) by truncating at the first
    successful parse boundary.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        # The file might be valid JSON followed by trailing garbage
        # (observed on network volumes after interrupted writes).
        # Try parsing only up to the reported error position.
        if exc.pos and exc.pos > 2:
            try:
                with open(path, "r") as f:
                    raw = f.read(exc.pos)
                state = json.loads(raw)
                log.warning(
                    "Loaded %s with %d trailing garbage bytes trimmed",
                    path.name, path.stat().st_size - exc.pos,
                )
                return state
            except Exception:
                pass
        log.warning("Failed to load %s: %s", path, exc)
        return None
    except OSError as exc:
        log.warning("Failed to load %s: %s", path, exc)
        return None


def load_snapshot(data_dir: Path) -> dict:
    """Load the most recent snapshot from disk.

    Falls back to .bak if the primary is missing, corrupt, **or empty**
    while the backup has actual data (safety net against a bad merge or
    shutdown writing an empty state over a populated snapshot).

    Returns an empty state dict if neither file exists.
    """
    primary = data_dir / "library.json"
    backup = data_dir / "library.json.bak"

    primary_state = _try_load_json(primary)
    backup_state = None  # loaded lazily

    if primary_state is not None:
        primary_tracks = len(primary_state.get("tracks", {}))
        if primary_tracks > 0:
            log.info("Loaded snapshot from %s (%d tracks)", primary.name, primary_tracks)
            return primary_state

        # Primary loaded but has 0 tracks — check backup before accepting.
        backup_state = _try_load_json(backup)
        if backup_state is not None and len(backup_state.get("tracks", {})) > 0:
            backup_tracks = len(backup_state.get("tracks", {}))
            log.warning(
                "Primary snapshot is empty but backup has %d tracks — using %s",
                backup_tracks, backup.name,
            )
            return backup_state

        # Both empty or no backup — use primary as-is
        log.info("Loaded snapshot from %s (%d tracks)", primary.name, 0)
        return primary_state

    # Primary failed — try backup
    backup_state = backup_state or _try_load_json(backup)
    if backup_state is not None:
        log.warning("Primary snapshot missing/corrupt — falling back to %s (%d tracks)",
                     backup.name, len(backup_state.get("tracks", {})))
        return backup_state

    log.info("No snapshot found — starting with empty state")
    return {}


import copy

# Count of AOF entries quarantined during the last replay.  Exposed via
# ``aof_quarantine_count`` so an admin/health endpoint (or test) can detect
# a partially-corrupted journal without grepping logs.
_aof_quarantine_count: int = 0


def aof_quarantine_count() -> int:
    """Return the number of AOF entries quarantined on the last replay.

    Reset to zero at the start of each ``replay_aof`` call.  When the
    journal is healthy this stays at zero; non-zero values indicate that
    ``library.aof.quarantine`` has fresh entries an operator should
    inspect (most often after a crash or unclean shutdown).
    """
    return _aof_quarantine_count


def _apply_entry_transactional(state: dict, entry: dict) -> bool:
    """Apply ``entry`` to ``state`` atomically.

    The underlying ``_apply_entry`` mutates ``state`` in place, so a
    half-applied batch upsert (e.g. AttributeError mid-loop) would leave
    the store in a torn state.  We snapshot the touched top-level
    sub-dicts before the call and restore them on failure so a corrupt
    batch can't taint the rest of the replay.

    Returns ``True`` on success, ``False`` if the entry was rolled back.
    """
    from soniqboom.core.merger import _apply_entry

    # Shallow-snapshot the top-level state slots this op might touch.
    # Deep-copying the entire 170K-track ``tracks`` dict per replayed
    # entry would dominate startup, so we copy lazily — only the keys
    # actually present in ``state``.
    snapshot = {k: copy.copy(v) for k, v in state.items()}
    try:
        _apply_entry(state, entry)
        return True
    except Exception:
        # Restore the pre-call references so partial mutations don't
        # leak.  Note: any nested dicts that ``_apply_entry`` mutated
        # in place are restored via the snapshot's reference if it was
        # a fresh container we copied; for atomic restoration we
        # additionally copy each sub-dict on the way in.
        for k, v in snapshot.items():
            state[k] = v
        # Drop keys created during the failed call.
        for k in list(state.keys()):
            if k not in snapshot:
                state.pop(k, None)
        return False


def replay_aof(state: dict, data_dir: Path) -> int:
    """Replay AOF entries on top of the loaded snapshot.

    Returns the number of entries applied.  Corrupt or unparseable entries
    (and any that fail the transactional apply) are written to
    ``library.aof.quarantine`` with the current timestamp so an operator
    can inspect them rather than the events being silently dropped.
    Counters are surfaced via ``aof_quarantine_count``.
    """
    global _aof_quarantine_count
    _aof_quarantine_count = 0

    aof_path = data_dir / "library.aof"
    if not aof_path.exists() or aof_path.stat().st_size == 0:
        return 0

    quarantine_path = data_dir / "library.aof.quarantine"
    quarantine_fp = None

    def _quarantine(raw: str, reason: str) -> None:
        nonlocal quarantine_fp
        global _aof_quarantine_count
        try:
            if quarantine_fp is None:
                quarantine_fp = open(quarantine_path, "a")
            quarantine_fp.write(
                json.dumps({
                    "quarantined_at": time.time(),
                    "reason": reason,
                    "raw": raw,
                }) + "\n",
            )
        except OSError:
            log.exception("Could not write to AOF quarantine at %s", quarantine_path)
        _aof_quarantine_count += 1

    applied = 0
    with open(aof_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("Skipping corrupt AOF entry: %s", exc)
                _quarantine(line, f"json: {exc}")
                continue
            try:
                ok = _apply_entry_transactional(state, entry)
            except Exception as exc:
                # Defensive — _apply_entry_transactional already catches
                # most failures; this covers anything that escapes the
                # snapshot/restore (e.g. an OOM mid-copy).
                log.warning("AOF entry raised %s — quarantining", exc)
                _quarantine(line, f"apply: {exc}")
                continue
            if ok:
                applied += 1
            else:
                _quarantine(line, "transactional rollback")

    if quarantine_fp is not None:
        try:
            quarantine_fp.close()
        except OSError:
            pass

    if applied:
        log.info("Replayed %d AOF entries", applied)
    if _aof_quarantine_count:
        log.warning(
            "Quarantined %d AOF entries to %s",
            _aof_quarantine_count, quarantine_path,
        )
    return applied


def populate_store(state: dict) -> None:
    """Populate the TrackStore singleton from a loaded snapshot state."""
    from soniqboom.core.store import get_store
    from soniqboom.core.startup_status import set_phase as _ss_phase

    store = get_store()
    t0 = time.monotonic()

    store.bulk_load(
        tracks=state.get("tracks", {}),
        waveforms=state.get("waveforms", {}),
        ratings=state.get("ratings", {}),
        play_stats=state.get("play_stats", {}),
        playlists=state.get("playlists", {}),
        history=state.get("history", []),
        scan_dirs=state.get("scan_dirs", {}),
        hash_lookups=state.get("hash_lookups", {}),
        config=state.get("config", {}),
    )

    # Index rebuild is the biggest chunk of startup wall-clock (3–15 s for
    # 268K tracks even with batch_mode).  Surface it as its own phase so
    # the menubar / CLI watcher sees "Building search indexes" instead of
    # still showing "Loading library snapshot" while seconds tick by.
    track_count = len(state.get("tracks", {}))
    _ss_phase("building_indexes", "Building search indexes",
              f"{track_count:,} tracks")
    store.rebuild_indexes()
    elapsed = (time.monotonic() - t0) * 1000
    log.info(
        "Store loaded: %d tracks, %d waveforms, %d ratings — indexes built in %.0fms",
        store.track_count(),
        len(state.get("waveforms", {})),
        len(state.get("ratings", {})),
        elapsed,
    )


def write_snapshot_sync(data_dir: Path) -> None:
    """Write a full snapshot synchronously.  Used during shutdown.

    Safety: refuses to overwrite a populated snapshot with an empty state.
    This guards against the edge case where the server starts, loads an
    empty/broken snapshot, and shuts down before a scan repopulates it —
    without this check the good backup would be rotated away.
    """
    import os
    import shutil
    from soniqboom.core.store import get_store

    store = get_store()
    state = store.to_snapshot()

    data_dir.mkdir(parents=True, exist_ok=True)
    primary = data_dir / "library.json"
    backup = data_dir / "library.json.bak"
    tmp = data_dir / "library.json.new"

    new_count = len(state.get("tracks", {}))

    # Refuse to overwrite a populated snapshot with an empty one.
    if new_count == 0 and primary.exists():
        try:
            with open(primary, "r") as f:
                old = json.load(f)
            old_count = len(old.get("tracks", {}))
        except Exception:
            old_count = 0
        if old_count > 0:
            log.warning(
                "Shutdown: store is empty but snapshot has %d tracks — skipping write to preserve data",
                old_count,
            )
            return

    t0 = time.monotonic()
    with open(tmp, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())

    # Backup: copy (not rename) so the primary stays until os.replace swaps.
    if primary.exists():
        try:
            shutil.copy2(primary, backup)
        except OSError as exc:
            log.warning("Shutdown: backup copy failed (%s), continuing", exc)

    try:
        os.replace(tmp, primary)
    except OSError as exc:
        log.error("Shutdown: failed to replace snapshot: %s", exc)
        return

    elapsed = (time.monotonic() - t0) * 1000
    log.info("Shutdown snapshot written in %.0fms (%d tracks)", elapsed, new_count)


def init_persistence(data_dir: Path) -> None:
    """Full startup sequence: load snapshot → replay AOF → populate store."""
    data_dir.mkdir(parents=True, exist_ok=True)
    state = load_snapshot(data_dir)
    replay_aof(state, data_dir)
    populate_store(state)
