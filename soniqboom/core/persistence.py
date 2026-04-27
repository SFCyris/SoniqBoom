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


def replay_aof(state: dict, data_dir: Path) -> int:
    """Replay AOF entries on top of the loaded snapshot.

    Returns the number of entries applied.
    """
    from soniqboom.core.merger import _apply_entry

    aof_path = data_dir / "library.aof"
    if not aof_path.exists() or aof_path.stat().st_size == 0:
        return 0

    applied = 0
    with open(aof_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                _apply_entry(state, entry)
                applied += 1
            except (json.JSONDecodeError, KeyError) as exc:
                log.warning("Skipping corrupt AOF entry: %s", exc)

    if applied:
        log.info("Replayed %d AOF entries", applied)
    return applied


def populate_store(state: dict) -> None:
    """Populate the TrackStore singleton from a loaded snapshot state."""
    from soniqboom.core.store import get_store

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
