# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A scan worker job for tests/test_scan_pool_recovery.py: a file named
``crash*`` kills its worker process, ``hang*`` hangs it; any other is
extracted as usual.  Imported by the (real) worker processes."""
from __future__ import annotations

import os
import time


def extract(path):
    if path.name.startswith("crash"):
        os._exit(1)
    if path.name.startswith("hang"):
        time.sleep(3600)
    from soniqboom.core import scanner
    return scanner._extract_one(path)


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
