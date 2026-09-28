"""Process starts without ``fork()`` for a Core-Foundation process (macOS).

Forking a process that has initialised Core Foundation (which this server
does the moment any outbound networking, Bonjour, or FSEvents watcher runs)
can crash the child between fork and exec in Network.framework's fork
handler — observed as a storm of ``Fatal Python error: Segmentation fault``
dumps whose crashing frame is ``subprocess.py:_execute_child`` under a
``ThreadPoolExecutor`` worker (219 dumps in one scan, 2026-07-02), and as
"crashed on child side of fork pre-exec" crash reports.  Every process the
server starts goes through this module:

``run()`` — ``subprocess.run`` for threads and start-up code.  CPython (3.12,
subprocess.py:1825) only takes the fork-free ``posix_spawn`` path when ALL of
these hold:

  * ``close_fds`` is False        (the default True forces the fork path)
  * ``cwd`` is None
  * ``preexec_fn`` is None, no ``pass_fds``/uid/gid/umask/session args
  * the executable contains a directory component (no bare PATH names)
  * no standard stream is bound to one of descriptors 0-2

On darwin ``run()`` does not rely on that path: its child is started by
``posix_spawn`` called directly (``_TightPopen``) with
``POSIX_SPAWN_CLOEXEC_DEFAULT`` — as uvloop's libuv does — so the child gets
its three standard streams and nothing else, whatever other threads have
open at that instant (a loop start's pipes, a socket being made); the GIL is
released during the call.  A bare program name is resolved on PATH
(``FileNotFoundError`` when it isn't there — never a fork to find out; the
child's ``argv[0]`` stays as given), and options that need a fork (a working
folder, a session, groups …) are refused.

``spawn()`` — ``asyncio.create_subprocess_exec`` for the event loop (every
renderer / probe / encoder).  The server's loop is uvloop
(``uvicorn[standard]``; the app bundles it): its libuv (1.48) already starts
processes with posix_spawn on macOS and ``POSIX_SPAWN_CLOEXEC_DEFAULT`` (a
working folder and a session of its own included; a fork only for
``preexec_fn`` / ``pass_fds``, which nothing here uses), so there ``spawn``
passes the start through untouched.  On the standard asyncio loop (tests,
``--loop asyncio``, a build without uvloop) it takes CPython's posix_spawn
path as ``run()`` does, and a start whose options still need a fork there (a
working folder, a session of its own) is started again when its child
crashed before it ran the program.

Known limit, standard asyncio loop only (not the server's): ``spawn`` there
takes CPython's posix_spawn path, which passes every descriptor not marked
close-on-exec — the sweep marks them first, but one another thread creates
in between (CPython sets close-on-exec just after creating a pipe or socket
on macOS) can still pass.

On non-darwin platforms both are plain passthroughs.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading

log = logging.getLogger(__name__)

# Held while descriptors are made inheritable for a start (the scanner's
# forkserver relaunch, ``scanner._spawnv_passfds_nofork``), swept back to
# close-on-exec (``_cloexec_locked``), and around a ``run()`` start — none of
# these undoes or leaks into another half-way.
spawn_lock = threading.Lock()

# How often ``spawn`` starts a process that must fork again when its child
# crashed before it ran the program.
FORK_RETRIES = 3
# asyncio's returncode (-signal) of a process that crashed.
_CRASHED = frozenset(-int(getattr(signal, n)) for n in (
    "SIGSEGV", "SIGBUS", "SIGILL", "SIGABRT", "SIGTRAP", "SIGFPE", "SIGSYS") if hasattr(signal, n))
# How long a forked child that never ran its program is waited for: it is
# ending (CPython returned from the start because it exec'd or died) — its
# exit status arrives within milliseconds.  A live child whose command line
# can't be read (a setuid program) costs this much once.
_CRASH_WAIT_S = 0.25


# Every kwarg that silently disqualifies CPython's posix_spawn fast path
# (subprocess.py:1825).
_FORK_FORCING_KWARGS = (
    "cwd", "preexec_fn", "pass_fds", "start_new_session",
    "user", "group", "extra_groups", "umask", "process_group",
)


def _stdio_fd(v) -> "int | None":
    """The descriptor a standard-stream argument binds, None for PIPE /
    DEVNULL / None / STDOUT."""
    if isinstance(v, int):
        return v if v >= 0 else None
    fileno = getattr(v, "fileno", None)
    if fileno is not None:
        try:
            return fileno()
        except (OSError, ValueError):
            return None
    return None


def _needs_fork(kwargs) -> bool:
    """A start option CPython 3.12 only honours on its fork path is set
    (``cwd``, ``start_new_session``, a ``umask`` / ``process_group``, …), or
    a standard stream is bound to one of descriptors 0-2 (``stderr=STDOUT``
    with ``stdout`` inherited, ``stdout=sys.stderr`` …)."""
    for k in _FORK_FORCING_KWARGS:
        v = kwargs.get(k)
        if v is None or v is False or (k in ("umask", "process_group") and v == -1):
            continue
        if k in ("pass_fds", "extra_groups") and not v:
            continue
        return True
    if kwargs.get("stderr") == subprocess.STDOUT and kwargs.get("stdout") is None:
        return True
    return any((fd := _stdio_fd(kwargs.get(k))) is not None and fd <= 2
               for k in ("stdin", "stdout", "stderr"))


def _cloexec_locked() -> None:
    """Every descriptor above stderr marked close-on-exec (``spawn_lock``
    held): ``posix_spawn`` with ``close_fds=False`` passes on each one that
    isn't — Python's own are, a C library's may not be — and a long child
    must not hold the server's port or a pipe open once the server stopped.
    0.3 ms at 1,000 open descriptors uncontended; ``os.listdir`` gives up the
    GIL per entry, so busy Python threads stretch it (the uvloop path skips
    it)."""
    try:
        names = os.listdir("/dev/fd")
    except OSError:
        return
    for n in names:
        try:
            fd = int(n)
            if fd > 2 and os.get_inheritable(fd):
                os.set_inheritable(fd, False)
        except (ValueError, OSError):
            pass                            # the listing's own descriptor, closed meanwhile


def _cloexec_all() -> None:
    """``_cloexec_locked`` under ``spawn_lock``."""
    with spawn_lock:
        _cloexec_locked()


def _resolve(program: str, env=None) -> str:
    """``program`` with a directory (CPython's posix_spawn path needs one): a
    bare name is looked up on PATH — ``env``'s when given, as exec would —
    and FileNotFoundError raised when it isn't there, as the start would
    (without a fork to find out)."""
    if os.path.dirname(program):
        return program
    path = None if env is None else env.get("PATH", os.defpath)
    found = shutil.which(program, path=path)
    if not found:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), program)
    return found


# posix_spawn flags (macOS <spawn.h>)
_POSIX_SPAWN_SETSIGDEF = 0x0004
_POSIX_SPAWN_SETSIGMASK = 0x0008
_POSIX_SPAWN_CLOEXEC_DEFAULT = 0x4000
_libc = None


def _spawn_tight(executable: str, args, env, restore_signals: bool, stdio) -> int:
    """``posix_spawn`` of ``executable`` (macOS, via ctypes — ``os.posix_spawn``
    can't set ``POSIX_SPAWN_CLOEXEC_DEFAULT``): the child gets descriptors
    0-2 — ``stdio`` = ((fd or -1, 0), (…, 1), (…, 2)): each given one dup2'd
    there, an unset one inherited — and nothing else.  Signal mask emptied;
    SIGPIPE / SIGXFSZ to their defaults with ``restore_signals`` (as
    CPython's posix_spawn path).  Returns the pid; OSError as the start's."""
    global _libc
    import ctypes
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    c = _libc
    fa, attr = ctypes.c_void_p(), ctypes.c_void_p()        # opaque pointers on macOS
    if c.posix_spawn_file_actions_init(ctypes.byref(fa)) != 0:
        raise OSError(ctypes.get_errno(), "posix_spawn_file_actions_init failed")
    try:
        if c.posix_spawnattr_init(ctypes.byref(attr)) != 0:
            raise OSError(ctypes.get_errno(), "posix_spawnattr_init failed")
        try:
            for fd, target in stdio:
                if fd == -1 or fd == target:
                    err = c.posix_spawn_file_actions_addinherit_np(ctypes.byref(fa), target)
                else:
                    err = c.posix_spawn_file_actions_adddup2(ctypes.byref(fa), fd, target)
                if err:
                    raise OSError(err, os.strerror(err))
            none = ctypes.c_uint32(0)
            default = ctypes.c_uint32(0)
            if restore_signals:
                for name in ("SIGPIPE", "SIGXFSZ"):
                    n = getattr(signal, name, None)
                    if n is not None:
                        default.value |= 1 << (int(n) - 1)
            c.posix_spawnattr_setsigmask(ctypes.byref(attr), ctypes.byref(none))
            c.posix_spawnattr_setsigdefault(ctypes.byref(attr), ctypes.byref(default))
            c.posix_spawnattr_setflags(ctypes.byref(attr), ctypes.c_short(
                _POSIX_SPAWN_CLOEXEC_DEFAULT | _POSIX_SPAWN_SETSIGMASK | _POSIX_SPAWN_SETSIGDEF))
            argv = [os.fsencode(a) for a in args]
            envp = [os.fsencode(k) + b"=" + os.fsencode(v) for k, v in env.items()]
            c_argv = (ctypes.c_char_p * (len(argv) + 1))(*argv, None)
            c_envp = (ctypes.c_char_p * (len(envp) + 1))(*envp, None)
            pid = ctypes.c_int(0)
            err = c.posix_spawn(ctypes.byref(pid), os.fsencode(executable), ctypes.byref(fa),
                                ctypes.byref(attr), c_argv, c_envp)
            if err:
                raise OSError(err, os.strerror(err), executable)
            return pid.value
        finally:
            c.posix_spawnattr_destroy(ctypes.byref(attr))
    finally:
        c.posix_spawn_file_actions_destroy(ctypes.byref(fa))


class _TightPopen(subprocess.Popen):
    """``subprocess.Popen`` whose child is started by ``_spawn_tight`` — its
    pipes, ``communicate``, ``wait``, ``kill`` are Popen's own.  Only what
    ``run()`` lets through reaches it (no working folder, session, groups,
    ``preexec_fn``, ``pass_fds``)."""

    def _execute_child(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env,
                       startupinfo, creationflags, shell, p2cread, p2cwrite, c2pread, c2pwrite,
                       errread, errwrite, restore_signals, gid, gids, uid, umask,
                       start_new_session, process_group):
        args = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        program = _resolve(os.fsdecode(executable if executable is not None else args[0]), env)
        sys.audit("subprocess.Popen", program, args, cwd, env)
        self.pid = _spawn_tight(program, args, os.environ if env is None else env, restore_signals,
                                ((p2cread, 0), (c2pwrite, 1), (errwrite, 2)))
        self._child_created = True
        self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)


def run(cmd: list[str], *, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """Drop-in ``subprocess.run`` that never forks on macOS (see the module
    docstring): the child started by ``_TightPopen`` (posix_spawn, its
    standard streams only), a bare program resolved on PATH
    (``FileNotFoundError`` without starting anything when it isn't there).
    Only list-form commands (no ``shell=True``); an option that needs a fork
    is refused (``ValueError``)."""
    if sys.platform != "darwin":
        return subprocess.run(cmd, input=input, capture_output=capture_output,
                              timeout=timeout, check=check, **kwargs)
    if kwargs.get("close_fds") is True:
        raise ValueError(
            "forksafe.run() with close_fds=True forces the fork path — "
            "omit it (the helper sets close_fds=False itself)")
    if kwargs.get("shell"):
        raise ValueError("forksafe.run() takes a list command, not shell=True")
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if _needs_fork({k: v for k, v in kwargs.items() if k not in ("stdin", "stdout", "stderr")}):
        raise ValueError("forksafe.run(): these options need a fork (a working folder, a session, "
                         "groups, preexec_fn, pass_fds …) — not allowed")
    kwargs["close_fds"] = False
    process = _TightPopen(list(cmd), **kwargs)
    with process:                           # as subprocess.run (3.12) does
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        except BaseException:
            process.kill()
            raise
        retcode = process.poll()
        if check and retcode:
            raise subprocess.CalledProcessError(retcode, process.args, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(process.args, retcode, stdout, stderr)


_ME: "str | None" = None


async def _crashed_before_exec(proc) -> bool:
    """Whether a forked child crashed before it ran its program — the fork
    child of a process that used Core Foundation / the network can crash in
    Network.framework's fork handler ("crashed on child side of fork
    pre-exec").  CPython returns from a fork start once the child exec'd or
    died (its close-on-exec error pipe closed).  A child whose command line
    is no longer this process's runs the program — answered at once, never
    waited for.  One that still has it, or whose command line can't be read
    (ended — a zombie or reaped — or, rarely, a live setuid program), is
    waited for up to ``_CRASH_WAIT_S``; its exit status tells whether it
    crashed."""
    global _ME
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return False
    from soniqboom.core import procinfo
    if not _ME:
        _ME = procinfo.cmdline(os.getpid()) or None
    now = procinfo.cmdline(pid)
    if now and now != _ME:
        return False
    try:
        rc = await asyncio.wait_for(proc.wait(), _CRASH_WAIT_S)
    except asyncio.TimeoutError:
        return False
    return rc in _CRASHED


def _on_uvloop() -> bool:
    try:
        return type(asyncio.get_running_loop()).__module__.startswith("uvloop")
    except RuntimeError:
        return False


async def spawn(program, *args, **kwargs):
    """``asyncio.create_subprocess_exec`` that never forks on macOS (see the
    module docstring).  The process runs in the background and is handled —
    pipes, ``wait``, ``kill``, cancellation — exactly as
    ``asyncio.create_subprocess_exec``'s; the program keeps its own name as
    ``argv[0]``.

    uvloop (the server's loop): passed through — libuv starts it with
    posix_spawn and ``POSIX_SPAWN_CLOEXEC_DEFAULT``.  Only ``preexec_fn`` /
    ``pass_fds`` make uvloop fork; such a start is checked like a fork below.

    The standard asyncio loop: ``close_fds=False``, descriptors swept to
    close-on-exec, the program resolved on PATH (passed as ``executable``;
    ``FileNotFoundError`` without starting anything when it isn't there) —
    CPython's posix_spawn path.  A start whose options still need a fork
    (``_needs_fork``: ``cwd``, ``start_new_session``, …) forks as before and
    is started again, up to ``FORK_RETRIES`` times, when its child ended by a
    crash signal before it ran the program (``_crashed_before_exec``; a
    program that crashes by itself within milliseconds of starting is
    indistinguishable and started that often too, then its crashed process
    is returned).  Cancelled during that check, the child is killed.

    Elsewhere than macOS: ``asyncio.create_subprocess_exec`` unchanged."""
    if sys.platform != "darwin":
        return await asyncio.create_subprocess_exec(program, *args, **kwargs)
    if kwargs.get("close_fds") is True:
        raise ValueError("forksafe.spawn() with close_fds=True forces a fork — omit it")
    if _on_uvloop():
        if kwargs.get("preexec_fn") is None and not kwargs.get("pass_fds"):
            return await asyncio.create_subprocess_exec(program, *args, **kwargs)
    elif not _needs_fork(kwargs):
        kwargs.setdefault("executable", _resolve(os.fspath(program), kwargs.get("env")))
        kwargs["close_fds"] = False
        _cloexec_all()
        return await asyncio.create_subprocess_exec(program, *args, **kwargs)
    for attempt in range(FORK_RETRIES + 1):
        proc = await asyncio.create_subprocess_exec(program, *args, **kwargs)
        if attempt == FORK_RETRIES:
            return proc
        try:
            crashed = await _crashed_before_exec(proc)
        except BaseException:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            raise
        if not crashed:
            return proc
        log.warning("%s ended at once by signal %d after a fork (the fork's child can crash "
                    "before it runs the program) — starting it again (%d of %d)",
                    os.path.basename(os.fspath(program)), -proc.returncode, attempt + 1, FORK_RETRIES)
    return proc
