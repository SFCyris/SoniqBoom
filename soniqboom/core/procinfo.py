# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Facts about other processes, read without forking (``main``'s reaper and
merger stop, ``scanner._kill_pool``), and the CPUs this one may use (pool
sizing)."""

import logging
import math
import os
import sys

log = logging.getLogger(__name__)


_CGROUP_ROOT = "/sys/fs/cgroup"


def _read(path: str) -> "str | None":
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _ancestors(rel: str) -> list[str]:
    """``/a/b`` → ``["/a/b", "/a", ""]``: a cgroup and every parent, to the root."""
    parts = [p for p in rel.strip().split("/") if p]
    return ["/" + "/".join(parts[:i]) if i else "" for i in range(len(parts), -1, -1)]


def _cgroup_cpu_limit() -> "int | None":
    """Whole CPUs a cgroup CPU quota allows (``docker run --cpus``, systemd
    ``CPUQuota=``), rounded up; None without a quota.  The process's own
    cgroup (``/proc/self/cgroup``) and each parent are read — the smallest
    quota wins: cgroup v2 ``cpu.max``, else v1 ``cpu.cfs_quota_us`` /
    ``cpu.cfs_period_us``."""
    v2_rel, v1_rel = "/", None
    for line in (_read("/proc/self/cgroup") or "").splitlines():
        hier, _, rest = line.partition(":")
        ctrls, _, rel = rest.partition(":")
        if hier == "0" and ctrls == "":
            v2_rel = rel or "/"
        elif "cpu" in ctrls.split(","):
            v1_rel = rel or "/"
    best = None

    def take(quota: int, period: int) -> None:
        nonlocal best
        if quota > 0 and period > 0:
            n = max(1, math.ceil(quota / period))
            best = n if best is None else min(best, n)
    for rel in _ancestors(v2_rel):
        raw = _read(f"{_CGROUP_ROOT}{rel}/cpu.max")
        if raw:
            quota, _, period = raw.partition(" ")
            if quota != "max":
                try:
                    take(int(quota), int(period or 100000))
                except ValueError:
                    pass
    if best is None and v1_rel is not None:
        for base in (f"{_CGROUP_ROOT}/cpu,cpuacct", f"{_CGROUP_ROOT}/cpu"):
            for rel in _ancestors(v1_rel):
                q = _read(f"{base}{rel}/cpu.cfs_quota_us")
                per = _read(f"{base}{rel}/cpu.cfs_period_us")
                try:
                    if q and per:
                        take(int(q), int(per))
                except ValueError:
                    pass
            if best is not None:
                break
    return best


def usable_cpus() -> int:
    """The CPUs this process may run on — not ``os.cpu_count()``, which
    counts the host's: the CPU affinity (a container's or LXC's cpuset),
    lowered to a cgroup CPU quota when one is set.  At least 1."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):         # macOS has no sched_getaffinity
        cpus = os.cpu_count() or 4
    if sys.platform.startswith("linux"):
        quota = _cgroup_cpu_limit()
        if quota is not None:
            cpus = min(cpus, quota)
    return max(1, cpus)


def env_count(name: str, default: int, *, minimum: int = 1, maximum: int = 64) -> int:
    """A whole number from environment variable ``name``, kept within
    ``minimum``..``maximum``, else ``default``; an unreadable value is logged
    and ignored."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return min(maximum, max(minimum, int(raw)))
    except ValueError:
        log.warning("%s=%r is not a whole number — using %d", name, raw, default)
        return default


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
