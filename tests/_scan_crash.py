# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan worker jobs for tests/test_scan_pool_recovery.py: a file named
``crash*`` kills its worker process (``os._exit``), ``libcrash*`` does so
only while the worker reads tracker durations in-process, ``kill*`` SIGKILLs it,
``segv*`` crashes it in native code (a NULL read, as a crashing decoder
library would), ``hang*`` hangs it; any other is extracted as usual.
Imported by the (real) worker processes."""
from __future__ import annotations

import os
import time


def _maybe_die(name: str) -> None:
    if name.startswith("libcrash"):
        # as a module that crashes libopenmpt in-process would: only while
        # this worker reads durations in-process (not ``_Isolated``)
        from soniqboom.core import metadata
        if metadata._LIBOPENMPT_IN_PROCESS:
            os._exit(1)
        return
    if name.startswith("crash"):
        os._exit(1)
    if name.startswith("kill"):
        import signal
        os.kill(os.getpid(), signal.SIGKILL)
    if name.startswith("segv"):
        import ctypes
        ctypes.string_at(0)
    if name.startswith("hang"):
        time.sleep(3600)


def extract(path, isolate=False):
    from soniqboom.core import scanner
    if isolate:
        with scanner._Isolated():
            return extract(path)
    _maybe_die(path.name)
    return scanner._extract_one(path)


def extract_tracked(path, progress):
    from soniqboom.core import scanner
    scanner._mark_progress(progress, 1)
    _maybe_die(path.name)
    return scanner._extract_one(path)


def extract_remote(file_data, remote_path, track_id, pc_program_archive=False, isolate=False):
    from soniqboom.core import scanner
    if isolate:
        with scanner._Isolated():
            return extract_remote(file_data, remote_path, track_id, pc_program_archive)
    _maybe_die(remote_path.rsplit("/", 1)[-1])
    return scanner._extract_one_remote(file_data, remote_path, track_id, pc_program_archive)


def incremental(files_strs, mtime_size_map, by_path=False):
    for s in files_strs:
        _maybe_die(os.path.basename(s))
    from soniqboom.core import scanner
    return scanner._compute_incremental(files_strs, mtime_size_map, by_path)


def dups(all_tracks):
    """Crashes its worker while the marker file named by the first track's
    ``title`` (``crash-once:<path>``) doesn't exist yet — creating it —
    and ``crash-always`` every time."""
    title = (all_tracks[0].get("title") or "") if all_tracks else ""
    if title == "crash-always":
        os._exit(1)
    if title.startswith("crash-once:"):
        marker = title.split(":", 1)[1]
        if not os.path.exists(marker):
            open(marker, "w").close()
            os._exit(1)
    from soniqboom.core import scanner
    return scanner._compute_duplicates_in_process(all_tracks)


def nested_cache_ready() -> bool:
    """This worker reads nested zips through its own cache."""
    from soniqboom.core import scanner
    return isinstance(scanner._NESTED, scanner._NestedZips)


def libopenmpt_in_process() -> bool:
    """This worker reads tracker durations with libopenmpt in-process."""
    from soniqboom.core import metadata
    return metadata._LIBOPENMPT_IN_PROCESS


def worker_facts():
    """(leads its own process group, the descriptors above stderr a child it
    starts would inherit)."""
    fds = []
    for n in os.listdir("/dev/fd"):
        fd = int(n)
        if fd > 2:
            try:
                if os.get_inheritable(fd):
                    fds.append(fd)
            except OSError:
                pass
    return os.getpgid(0) == os.getpid(), fds


def start_child():
    """A decoder-like child process of this worker; its pid."""
    import subprocess
    return subprocess.Popen(["/bin/sleep", "120"]).pid
