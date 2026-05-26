# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Startup-phase tracker.

The CLI banner used to print "SoniqBoom · ready" before uvicorn even
started — so on a 268K-track library the user saw a "ready" banner and
then 10-20 seconds of silence while the snapshot loaded and indexes
rebuilt.  Bad signal: indistinguishable from a real hang.

This module gives the startup sequence a single source of truth that

  1. **Prints** a friendly progress line to stderr at every phase
     boundary (humans tailing the terminal see what's happening)
  2. **Writes** a small JSON status file to the data dir so external
     watchers — the macOS menubar app, monitoring scripts, anyone — can
     poll the current phase without the HTTP API (which isn't listening
     yet during the slow phases)
  3. Tracks per-phase elapsed time so the final "ready" line can
     report where the wall-clock went

Wire-up is intentionally narrow: ``main.py`` calls :func:`init` from the
lifespan handler, then ``set_phase(...)`` at each major step, then
:func:`mark_ready` at the end.  The slowest phases (snapshot load + index
rebuild) fire from inside ``core/persistence.py`` so they're recorded
exactly where the wall-clock is being spent.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Module-level state — protected by ``_LOCK`` because phases are emitted
# from threads inside ``asyncio.to_thread(...)`` (e.g. the bulk-rebuild
# the snapshot loader hops onto to keep the event loop responsive).
_LOCK = threading.Lock()

_status: dict[str, Any] = {
    "ready":          False,
    "phase":          "initializing",
    "label":          "Starting…",
    "message":        "",
    "started_at":     None,   # epoch seconds when init() was called
    "phase_start":    None,   # epoch seconds when current phase began
    "elapsed_ms":     0,      # cumulative since init
    "phase_elapsed_ms": 0,    # for the current (incomplete) phase
    "phases":         [],     # list of {phase, label, elapsed_ms} completed
    "pid":            os.getpid(),
}

_status_file: Path | None = None


# ── Public API ──────────────────────────────────────────────────────────────


def init(data_dir: Path) -> None:
    """Initialise the tracker.  Idempotent — safe to call once at the top
    of the lifespan handler before anything else.

    Sets ``started_at`` and the status-file path, prints the opening
    banner, and writes the first status snapshot to disk so a menubar app
    that polls is never racing the first real phase.
    """
    global _status_file
    with _LOCK:
        now = time.time()
        _status["started_at"]   = now
        _status["phase_start"]  = now
        _status["elapsed_ms"]   = 0
        _status["phase_elapsed_ms"] = 0
        _status["phases"]       = []
        _status["ready"]        = False
        _status["phase"]        = "initializing"
        _status["label"]        = "Starting…"
        _status["message"]      = ""
        _status_file            = data_dir / "startup-status.json"
    _emit_line("Starting SoniqBoom…", "")
    _write_status_file()


def set_phase(phase: str, label: str, message: str = "") -> None:
    """Mark the start of a new phase.

    Closes the previous phase (with its elapsed time appended to
    ``phases``), opens the new one, prints a line to stderr, and writes
    the status file.

    ``label`` is the short headline ("Building search indexes").  ``message``
    is the optional sub-line ("268,082 tracks").  Both go to stderr — the
    headline as the chip, the message in parentheses after it.
    """
    global _status_file
    now = time.time()
    with _LOCK:
        # Close out the previous phase, if any
        if _status["phase_start"] is not None and _status["phase"] != "initializing":
            elapsed = (now - _status["phase_start"]) * 1000
            _status["phases"].append({
                "phase":      _status["phase"],
                "label":      _status["label"],
                "elapsed_ms": round(elapsed),
            })
        _status["phase"]            = phase
        _status["label"]            = label
        _status["message"]          = message
        _status["phase_start"]      = now
        _status["phase_elapsed_ms"] = 0
        if _status["started_at"]:
            _status["elapsed_ms"]   = round((now - _status["started_at"]) * 1000)
    _emit_line(label, message)
    _write_status_file()


def mark_ready(message: str = "") -> None:
    """Mark startup as complete.  Records final phase + total elapsed,
    prints the closing line, and persists.
    """
    now = time.time()
    with _LOCK:
        # Close the last open phase if there is one
        if _status["phase_start"] is not None and _status["phase"] != "initializing":
            elapsed = (now - _status["phase_start"]) * 1000
            _status["phases"].append({
                "phase":      _status["phase"],
                "label":      _status["label"],
                "elapsed_ms": round(elapsed),
            })
        _status["ready"]          = True
        _status["phase"]          = "ready"
        _status["label"]          = "Ready"
        _status["message"]        = message
        _status["phase_start"]    = now
        if _status["started_at"]:
            _status["elapsed_ms"] = round((now - _status["started_at"]) * 1000)
    # Pretty wall-clock line — match the banner aesthetic of main.py.
    total_s = _status["elapsed_ms"] / 1000.0
    _emit_line(f"Ready ({total_s:.1f}s total)", message)
    _write_status_file()


def get_status() -> dict[str, Any]:
    """Return a snapshot of the current status.  Safe to call from any
    thread; the returned dict is a shallow copy so the caller can mutate
    it without racing the tracker.
    """
    with _LOCK:
        # Refresh phase_elapsed_ms so a long-running phase doesn't appear
        # stuck at 0 to a poller.
        if _status["phase_start"]:
            _status["phase_elapsed_ms"] = round((time.time() - _status["phase_start"]) * 1000)
        return dict(_status)


def status_file_path() -> Path | None:
    """Return the on-disk status file path, or None if init() hasn't run.

    Useful for external watchers (menubar app, monitoring scripts) that
    derive the path themselves but want a single canonical name.
    """
    return _status_file


# ── Internals ───────────────────────────────────────────────────────────────


def _emit_line(label: str, message: str) -> None:
    """Print a friendly progress line to stderr.

    Stderr (not stdout) because uvicorn already owns stdout for its
    request log, and we want the startup chatter to remain visible even
    when stdout is redirected.  We avoid the logger here so the line
    isn't ALSO duplicated into ``soniqboom.log`` (which has its own
    timestamped INFO lines for the same events) — this output is
    explicitly for the human watching the terminal.

    Skipped when stderr isn't a tty so cron / systemd / dockerised
    setups don't get bombarded with unparseable status markers.
    """
    if not getattr(sys.stderr, "isatty", lambda: False)():
        return
    chip = f"  ▸ {label}"
    if message:
        chip = f"{chip}  ({message})"
    try:
        print(chip, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # stderr closed mid-startup is best-effort silence; never raise.
        pass


def _write_status_file() -> None:
    """Atomically write the current status to ``startup-status.json``.

    Atomic via tmp+rename so a polling reader can't catch a half-written
    file.  Failures are swallowed — startup progress reporting must not
    be in the critical path.
    """
    if _status_file is None:
        return
    try:
        snapshot = get_status()
        tmp = _status_file.with_suffix(".json.new")
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _status_file)
    except OSError:
        # Disk full / permission denied / data_dir gone — none of these
        # should kill startup.  The line we already printed to stderr is
        # the human-readable copy.
        pass
