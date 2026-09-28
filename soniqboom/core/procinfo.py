# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Facts about other processes, read without forking (``main``'s reaper and
merger stop, ``scanner._kill_pool``)."""

import os
import sys


def cmdline(pid: int) -> str:
    """A process's command line WITHOUT forking (the server must not fork a
    helper like ``ps`` on macOS once it used the network — Network.framework's
    fork handler can crash the child): ``sysctl(KERN_PROCARGS2)`` on macOS,
    ``/proc`` on Linux; "" when unknown."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            return ""
    if sys.platform != "darwin":
        return ""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        argmax = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(argmax))
        if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax),
                       ctypes.byref(size), None, 0) != 0:        # KERN_ARGMAX
            return ""
        buf = ctypes.create_string_buffer(argmax.value)
        size = ctypes.c_size_t(argmax.value)
        if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buf,
                       ctypes.byref(size), None, 0) != 0:        # KERN_PROCARGS2
            return ""
    except Exception:                                       # noqa: BLE001
        return ""
    raw = buf.raw[:size.value]
    argc = int.from_bytes(raw[:4], "little")
    args: list[str] = []
    rest = raw[4:].split(b"\0")
    for part in rest[1:]:                   # after the executable path and its padding
        if not part and not args:
            continue
        args.append(part.decode("utf-8", "replace"))
        if len(args) >= argc:
            break
    return " ".join(args)


def is_our_forkserver_child(pid: int) -> bool:
    """``pid`` runs and is a process forked from SoniqBoom's multiprocessing
    forkserver (its command line is the forkserver's)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    cmd = cmdline(pid)
    return "multiprocessing" in cmd and ("SoniqBoom" in cmd or "'soniqboom'" in cmd)
