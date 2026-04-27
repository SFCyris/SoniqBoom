# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Background merger process — consolidates AOF into snapshot.

Runs as a daemon subprocess.  Periodically:
  1. Reads new entries from library.aof
  2. Loads library.json (last full snapshot)
  3. Applies AOF entries to the snapshot
  4. Writes library.json.new
  5. Rotates: library.json → library.json.bak, library.json.new → library.json
  6. Truncates library.aof
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _apply_entry(state: dict, entry: dict) -> None:
    """Apply a single AOF record to the snapshot state dict."""
    op = entry.get("op")

    if op == "upsert_track":
        tid = entry["id"]
        state.setdefault("tracks", {})[tid] = entry["data"]

    elif op == "batch_upsert_tracks":
        tracks = state.setdefault("tracks", {})
        for t in entry.get("data", []):
            tracks[t["id"]] = t

    elif op == "delete_tracks":
        tracks = state.get("tracks", {})
        waveforms = state.get("waveforms", {})
        for tid in entry.get("ids", []):
            tracks.pop(tid, None)
            waveforms.pop(tid, None)

    elif op == "update_track_fields":
        t = state.get("tracks", {}).get(entry["id"])
        if t:
            t.update(entry.get("data", {}))

    elif op == "set_rating":
        ratings = state.setdefault("ratings", {})
        rating = entry.get("rating", 0)
        if rating <= 0:
            ratings.pop(entry["id"], None)
        else:
            ratings[entry["id"]] = rating

    elif op == "record_play":
        stats = state.setdefault("play_stats", {})
        tid = entry["id"]
        ts = entry.get("ts", int(time.time()))
        existing = stats.get(tid)
        if existing:
            existing["count"] = existing.get("count", 0) + 1
            existing["last_played"] = ts
        else:
            stats[tid] = {"count": 1, "last_played": ts}

    elif op == "upsert_playlist":
        state.setdefault("playlists", {})[entry["id"]] = entry["data"]

    elif op == "delete_playlist":
        state.get("playlists", {}).pop(entry["id"], None)

    elif op == "push_history":
        history = state.setdefault("history", [])
        history.append(entry["data"])
        max_h = 500
        if len(history) > max_h:
            state["history"] = history[-max_h:]

    elif op == "upsert_scan_dir":
        state.setdefault("scan_dirs", {})[entry["path"]] = entry["data"]

    elif op == "delete_scan_dir":
        state.get("scan_dirs", {}).pop(entry.get("path"), None)

    elif op == "set_config":
        state.setdefault("config", {})[entry["key"]] = entry.get("value")


def _do_merge(data_dir: Path) -> int:
    """Run one merge cycle.  Returns number of entries applied.

    Robust against network-volume quirks:
      • fsync after writing to ensure bytes reach the server before rename
      • shutil.copy2 for the backup (copy then delete, not rename) so the
        snapshot is never absent — a failed copy still leaves the original
      • os.replace for the final swap (atomic on POSIX, overwrites target)
      • If the snapshot is missing (e.g. after a prior crash), fall back to
        the .bak file so we never start from an empty state
    """
    import shutil

    snapshot_path = data_dir / "library.json"
    backup_path   = data_dir / "library.json.bak"
    aof_path      = data_dir / "library.aof"

    if not aof_path.exists() or aof_path.stat().st_size == 0:
        return 0

    with open(aof_path, "r") as f:
        lines = f.readlines()

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Skipping corrupt AOF line: %s", line[:80])
    if not entries:
        return 0

    # Load the current snapshot — fall back to backup if the primary is
    # missing or empty (can happen if a previous merge/shutdown wrote an
    # empty state).  Prefer whichever file has more tracks so we never
    # regress from a populated snapshot to an empty one.
    state = {}
    for src in (snapshot_path, backup_path):
        if not src.exists():
            continue
        try:
            with open(src, "r") as f:
                candidate = json.load(f)
        except json.JSONDecodeError as exc:
            # Handle trailing-garbage corruption (valid JSON + extra bytes)
            if exc.pos and exc.pos > 2:
                try:
                    with open(src, "r") as f:
                        raw = f.read(exc.pos)
                    candidate = json.loads(raw)
                    log.warning("Merger: loaded %s with trailing garbage trimmed", src.name)
                except Exception:
                    log.warning("Merger: could not read %s (%s), trying next", src.name, exc)
                    continue
            else:
                log.warning("Merger: could not read %s (%s), trying next", src.name, exc)
                continue
        except OSError as exc:
            log.warning("Merger: could not read %s (%s), trying next", src.name, exc)
            continue

        cand_tracks = len(candidate.get("tracks", {}))
        curr_tracks = len(state.get("tracks", {}))
        if cand_tracks >= curr_tracks:
            state = candidate
            if cand_tracks > 0:
                break  # found a populated snapshot, use it

    for entry in entries:
        _apply_entry(state, entry)

    # Write the new snapshot and fsync so the data reaches the server
    # before we touch any other files.
    tmp_path = data_dir / "library.json.new"
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        with open(tmp_path, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:
        log.error("Merger: failed to write temp snapshot: %s", exc)
        tmp_path.unlink(missing_ok=True)
        return 0

    # Verify the temp file exists and has content before proceeding.
    # On network volumes (SMB/NFS) fsync may return before the data is
    # fully committed.  Retry a few times with short delays.
    _verified = False
    for _attempt in range(4):
        try:
            if tmp_path.exists() and tmp_path.stat().st_size > 0:
                _verified = True
                break
        except OSError:
            pass
        time.sleep(0.25)
    if not _verified:
        log.error("Merger: temp snapshot missing or empty after write — skipping rotation")
        tmp_path.unlink(missing_ok=True)
        return 0

    # Back up the current snapshot (copy, not rename — the original stays
    # in place until os.replace atomically swaps it).
    if snapshot_path.exists():
        try:
            shutil.copy2(snapshot_path, backup_path)
        except OSError as exc:
            log.warning("Merger: backup copy failed (%s), continuing", exc)

    # Atomic replace: tmp → snapshot.  On POSIX this is a single rename()
    # syscall that overwrites the target.
    try:
        os.replace(tmp_path, snapshot_path)
    except FileNotFoundError:
        log.error("Merger: temp file vanished before replace — skipping this cycle")
        return 0

    with open(aof_path, "w") as f:
        f.truncate(0)

    return len(entries)


def merger_loop(data_dir_str: str, interval: int = 120) -> None:
    """Main loop for the background merger process.

    Designed to be the target of ``multiprocessing.Process``.
    """
    data_dir = Path(data_dir_str)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

    running = True

    def _handle_signal(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Merger started (dir=%s, interval=%ds)", data_dir, interval)

    while running:
        time.sleep(interval)
        if not running:
            break
        try:
            n = _do_merge(data_dir)
            if n:
                log.info("Merger: applied %d AOF entries", n)
        except Exception:
            log.exception("Merger error")

    try:
        n = _do_merge(data_dir)
        if n:
            log.info("Merger final: applied %d AOF entries", n)
    except Exception:
        log.exception("Merger final merge error")

    log.info("Merger stopped")


def start_merger(data_dir: Path, interval: int = 120):
    """Spawn the background merger as a daemon process."""
    import multiprocessing
    proc = multiprocessing.Process(
        target=merger_loop,
        args=(str(data_dir), interval),
        daemon=True,
        name="soniqboom-merger",
    )
    proc.start()
    log.info("Merger process started (pid=%d)", proc.pid)
    return proc
