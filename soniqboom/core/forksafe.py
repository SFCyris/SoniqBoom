"""Fork-safe ``subprocess.run`` for threads of a Core-Foundation process.

On macOS, ``subprocess.run`` performs a real ``fork()`` before ``exec``
unless CPython's posix_spawn fast path applies.  Forking a process that
has initialised Core Foundation (which this server does the moment any
outbound networking, Bonjour, or FSEvents watcher runs) from a *worker
thread* segfaults the child between fork and exec — observed as a storm
of ``Fatal Python error: Segmentation fault`` dumps whose crashing frame
is ``subprocess.py:_execute_child`` under a ``ThreadPoolExecutor``
worker (219 dumps in one scan, 2026-07-02).

CPython (3.12, subprocess.py:1825) only takes the fork-free
``posix_spawn`` path when ALL of these hold:

  * ``close_fds`` is False        (the default True forces the fork path)
  * ``cwd`` is None
  * ``preexec_fn`` is None, no ``pass_fds``/uid/gid/umask/session args
  * the executable contains a directory component (no bare PATH names)

``run()`` below arranges exactly that on darwin: it resolves bare
executable names via ``shutil.which`` and defaults ``close_fds=False``.
The fd inheritance this opens up is harmless for the short-lived,
timeout-bounded CLI probes this helper is meant for (openmpt123, lha,
ffprobe, uade123) — but do NOT use it for long-running children that
must not pin the server's sockets, and never pass ``cwd`` through it.

On non-darwin platforms this is a plain ``subprocess.run`` passthrough.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


# Every kwarg that silently disqualifies CPython's posix_spawn fast path
# (subprocess.py:1825).  Passing one through this helper would quietly
# degrade back into the fork hazard — reject them loudly instead.
_FORK_FORCING_KWARGS = (
    "cwd", "preexec_fn", "pass_fds", "start_new_session",
    "user", "group", "extra_groups", "umask", "process_group",
)


def run(cmd: list[str], **kwargs):
    """Drop-in ``subprocess.run`` that avoids ``fork()`` on macOS.

    Only list-form commands are supported (no ``shell=True``).  Any
    kwarg that would silently disable the posix_spawn path is rejected
    loudly rather than degrading back into the fork hazard.
    """
    for k in _FORK_FORCING_KWARGS:
        v = kwargs.get(k)
        if v not in (None, False) and v != -1:
            raise ValueError(
                f"forksafe.run() cannot take {k}={v!r} — it forces the "
                "fork path; use plain subprocess.run from a fork-safe "
                "context instead")
    if kwargs.get("close_fds") is True:
        raise ValueError(
            "forksafe.run() with close_fds=True forces the fork path — "
            "omit it (the helper sets close_fds=False itself)")
    if sys.platform == "darwin":
        kwargs.setdefault("close_fds", False)
        exe = cmd[0]
        if not os.path.dirname(exe):
            resolved = shutil.which(exe)
            if resolved:
                cmd = [resolved, *cmd[1:]]
    return subprocess.run(cmd, **kwargs)
