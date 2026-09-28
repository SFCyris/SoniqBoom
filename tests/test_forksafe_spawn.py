# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``forksafe``: every process the server starts, starts without ``fork()``
on macOS — Network.framework's fork handler can crash a forked child of a
process that used the network before it execs.  ``spawn`` (the event loop)
runs its process in the background exactly as ``asyncio.create_subprocess_exec``
does, on uvloop (the server's loop: libuv's posix_spawn, passed through) and
on the standard asyncio loop (CPython's posix_spawn path); a start whose
options need a fork on the standard loop (a working folder, a session of its
own) forks and is started again, up to three times, when its child crashed
before it ran the program.  ``run`` (threads, start-up) is ``subprocess.run``
without a fork."""
from __future__ import annotations

import asyncio
import ast
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

import pytest

from soniqboom.core import forksafe

darwin = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


@pytest.fixture
def starts(monkeypatch):
    """Counts how CPython starts each child: posix_spawn or fork+exec
    (uvloop's libuv starts bypass both)."""
    got = {"posix_spawn": 0, "fork": 0}
    real_ps, real_fe = os.posix_spawn, subprocess._fork_exec

    def ps(*a, **k):
        got["posix_spawn"] += 1
        return real_ps(*a, **k)

    def fe(*a, **k):
        got["fork"] += 1
        return real_fe(*a, **k)
    monkeypatch.setattr(os, "posix_spawn", ps)
    monkeypatch.setattr(subprocess, "_fork_exec", fe)
    return got


def _on(loop: str, coro_fn):
    if loop == "uvloop":
        import uvloop
        return uvloop.run(coro_fn())
    return asyncio.run(coro_fn())


LOOPS = pytest.mark.parametrize("loop", ["asyncio", "uvloop"])


# ── spawn: fork-free, in the background, as before ─────────────────────────

@darwin
@LOOPS
def test_a_start_does_not_fork_and_keeps_its_name(starts, loop):
    async def go():
        proc = await forksafe.spawn(sys.executable, "-c", "import sys; print(sys.argv[0])",
                                    stdout=asyncio.subprocess.PIPE)
        return await proc.communicate(), proc.returncode
    (out, _), rc = _on(loop, go)
    assert out == b"-c\n" and rc == 0
    async def bare():
        proc = await forksafe.spawn("sh", "-c", 'echo "$0"', stdout=asyncio.subprocess.PIPE)
        return (await proc.communicate())[0]
    assert _on(loop, bare) == b"sh\n"                   # argv[0] stays the bare name
    assert starts["fork"] == 0
    assert starts["posix_spawn"] == (2 if loop == "asyncio" else 0)     # uvloop: libuv's own


@darwin
@LOOPS
def test_a_missing_program_raises_without_a_fork(starts, loop, tmp_path):
    tool = tmp_path / "mytool"
    tool.write_text("#!/bin/sh\necho found\n")
    tool.chmod(0o755)

    async def go():
        with pytest.raises(FileNotFoundError):
            await forksafe.spawn("no-such-renderer-xyz")
        proc = await forksafe.spawn("mytool", env={"PATH": str(tmp_path)}, stdout=asyncio.subprocess.PIPE)
        assert (await proc.communicate())[0] == b"found\n"          # env's PATH, as exec's
        with pytest.raises(ValueError):
            await forksafe.spawn("/bin/echo", close_fds=True)
    _on(loop, go)
    assert starts["fork"] == 0


@darwin
@LOOPS
def test_the_child_gets_no_descriptor_of_the_server(loop):
    r, w = os.pipe()
    try:
        check = f"import os\ntry:\n    os.fstat({w}); print('open')\nexcept OSError:\n    print('closed')\n"

        async def go():
            os.set_inheritable(w, True)                # as a C library's descriptor may be
            proc = await forksafe.spawn(sys.executable, "-c", check, stdout=asyncio.subprocess.PIPE)
            return (await proc.communicate())[0].strip()
        assert _on(loop, go) == b"closed"
        os.set_inheritable(w, True)
        assert forksafe.run([sys.executable, "-c", check], capture_output=True).stdout.strip() == b"closed"
    finally:
        os.close(r)
        os.close(w)


@darwin
def test_the_sweep_leaves_the_standard_streams_alone():
    was = [os.get_inheritable(fd) for fd in (0, 1, 2)]
    forksafe._cloexec_all()
    assert [os.get_inheritable(fd) for fd in (0, 1, 2)] == was


@darwin
def test_the_sweep_waits_for_a_forkserver_relaunch_holding_the_lock():
    done = threading.Event()
    with forksafe.spawn_lock:
        t = threading.Thread(target=lambda: (forksafe._cloexec_all(), done.set()), daemon=True)
        t.start()
        assert not done.wait(0.2)                       # blocked while the lock is held
    assert done.wait(5)


@darwin
@LOOPS
def test_children_run_in_the_background_together_and_can_be_killed(loop):
    async def go():
        ticks = 0
        stop = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)
        t = asyncio.create_task(ticker())
        t0 = time.monotonic()
        procs = [await forksafe.spawn("/bin/sh", "-c", "sleep 0.4; echo done", stdout=asyncio.subprocess.PIPE)
                 for _ in range(3)]
        outs = await asyncio.gather(*(p.communicate() for p in procs))
        took = time.monotonic() - t0
        long = await forksafe.spawn("/bin/sleep", "30")
        long.kill()
        rc = await long.wait()
        stop.set()
        await t
        return outs, took, ticks, rc
    outs, took, ticks, rc = _on(loop, go)
    assert all(o == b"done\n" for o, _ in outs) and took < 1.0      # together, not one after another
    assert ticks >= 20 and rc == -signal.SIGKILL


@darwin
def test_a_start_with_a_working_folder_or_a_session_forks_only_on_the_standard_loop(starts, tmp_path):
    async def go():
        proc = await forksafe.spawn("/bin/pwd", cwd=str(tmp_path), stdout=asyncio.subprocess.PIPE)
        out = (await proc.communicate())[0]
        proc = await forksafe.spawn("/bin/sh", "-c", "ps -o sess= -p $$ >/dev/null; echo ok",
                                    start_new_session=True, stdout=asyncio.subprocess.PIPE)
        return out, (await proc.communicate())[0]
    out, ok = asyncio.run(go())
    assert pathlib.Path(out.decode().strip()).resolve() == tmp_path.resolve() and ok == b"ok\n"
    assert starts == {"posix_spawn": 0, "fork": 2}
    starts.update(posix_spawn=0, fork=0)
    out, ok = _on("uvloop", go)                        # libuv: posix_spawn with chdir / setsid
    assert pathlib.Path(out.decode().strip()).resolve() == tmp_path.resolve() and ok == b"ok\n"
    assert starts == {"posix_spawn": 0, "fork": 0}


@darwin
def test_a_running_forked_child_is_never_waited_for(tmp_path):
    """The crash check reads the child's command line: a child that runs its
    program returns at once (a render isn't held up until it ends)."""
    async def go():
        t0 = time.monotonic()
        proc = await forksafe.spawn("/bin/sleep", "5", cwd=str(tmp_path))
        took = time.monotonic() - t0
        running = proc.returncode is None
        proc.kill()
        await proc.wait()
        return took, running
    took, running = asyncio.run(go())
    assert running and took < 0.2


# ── the crash check and the retry (fake fork starts: no real crash reports) ──

class _Child:
    def __init__(self, pid, rc, wait_s=0.0):
        self.pid, self.rc, self.wait_s = pid, rc, wait_s
        self.returncode, self.waited, self.killed = None, False, False

    async def wait(self):
        self.waited = True
        if self.wait_s:
            await asyncio.sleep(self.wait_s)
        self.returncode = self.rc
        return self.rc

    def kill(self):
        self.killed = True


@pytest.fixture
def forked(monkeypatch):
    """Fake fork starts on the standard loop: each child is (command line,
    exit status[, seconds its end takes]) — the server's own command line =
    it never ran its program."""
    from soniqboom.core import procinfo
    me = "python -m soniqboom"
    plan: list = []
    made: list = []

    async def fake_exec(program, *args, **kw):
        cmd, rc, *wait_s = plan.pop(0)
        made.append(_Child(1000 + len(made), rc, *wait_s))
        made[-1].cmd = cmd
        return made[-1]
    monkeypatch.setattr(forksafe, "_ME", me)
    monkeypatch.setattr(forksafe.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(procinfo, "cmdline", lambda pid: next((c.cmd for c in made if c.pid == pid), ""))
    monkeypatch.setattr(forksafe.sys, "platform", "darwin")
    return me, plan, made


@pytest.mark.parametrize("kw", [{"cwd": "/tmp"}, {"start_new_session": True}])
async def test_a_child_that_crashed_before_it_ran_is_started_again(forked, caplog, kw):
    me, plan, made = forked
    plan += [(me, -signal.SIGSEGV), ("", -signal.SIGBUS), ("uade123 -1 tune.mod", None)]
    proc = await forksafe.spawn("/bin/sh", "-c", "x", **kw)
    assert proc is made[2] and len(made) == 3 and not made[2].waited     # a running one is never waited for
    assert sum("starting it again" in r.getMessage() for r in caplog.records) == 2


@pytest.mark.parametrize("sig", [signal.SIGABRT, signal.SIGTRAP, signal.SIGILL])
async def test_every_crash_signal_counts(forked, sig):
    me, plan, made = forked
    plan += [(me, -sig), ("renderer", None)]
    await forksafe.spawn("/bin/sh", cwd="/tmp")
    assert len(made) == 2


@pytest.mark.parametrize("rc", [-signal.SIGKILL, -signal.SIGTERM, 1, 0])
async def test_a_child_that_ended_otherwise_is_not_started_again(forked, rc):
    me, plan, made = forked
    plan += [("", rc)]
    proc = await forksafe.spawn("/bin/sh", cwd="/tmp")
    assert len(made) == 1 and proc.returncode == rc


async def test_a_program_that_always_crashes_is_tried_four_times(forked):
    me, plan, made = forked
    plan += [(me, -signal.SIGSEGV)] * 4
    proc = await forksafe.spawn("/bin/sh", cwd="/tmp")
    assert len(made) == 4 and proc is made[3] and not proc.waited     # the last one goes to the caller as is
    assert await proc.wait() == -signal.SIGSEGV


async def test_an_unreadable_live_child_costs_the_short_wait_only(forked):
    me, plan, made = forked
    plan += [("", None, 30.0)]                          # a live setuid program: command line unreadable
    t0 = time.monotonic()
    proc = await forksafe.spawn("/bin/sh", cwd="/tmp")
    assert proc is made[0] and time.monotonic() - t0 < 0.5


async def test_a_start_cancelled_during_the_check_kills_its_child(forked, monkeypatch):
    me, plan, made = forked
    plan += [("", None, 30.0)]
    monkeypatch.setattr(forksafe, "_CRASH_WAIT_S", 10.0)
    task = asyncio.create_task(forksafe.spawn("/bin/sh", cwd="/tmp"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert made[0].killed


async def test_a_fake_child_without_a_pid_is_not_checked(forked):
    me, plan, made = forked
    plan += [(me, -signal.SIGSEGV)]
    made_pid = made
    proc_holder = []

    async def exec_without_pid(program, *args, **kw):
        c = _Child(None, -signal.SIGSEGV)
        proc_holder.append(c)
        return c
    forksafe.asyncio.create_subprocess_exec = exec_without_pid
    proc = await forksafe.spawn("/bin/sh", cwd="/tmp")
    assert proc is proc_holder[0] and len(proc_holder) == 1 and not made_pid


def test_on_uvloop_no_start_is_checked_or_retried(monkeypatch, tmp_path):
    checked = []

    async def check(proc):
        checked.append(proc)
        return True
    monkeypatch.setattr(forksafe, "_crashed_before_exec", check)

    async def go():
        proc = await forksafe.spawn("/bin/pwd", cwd=str(tmp_path), stdout=asyncio.subprocess.PIPE)
        await proc.communicate()
    if sys.platform == "darwin":
        _on("uvloop", go)
        assert checked == []


def test_the_fork_options_are_known():
    need = forksafe._needs_fork
    assert not need({}) and not need({"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "env": {}})
    for kw in ({"cwd": "/x"}, {"start_new_session": True}, {"process_group": 0}, {"umask": 0},
               {"pass_fds": (5,)}, {"preexec_fn": print}, {"stderr": subprocess.STDOUT},
               {"stdout": 1}, {"stderr": sys.__stderr__}, {"stdin": 0}):
        assert need(kw), kw
    for kw in ({"start_new_session": False}, {"process_group": -1}, {"umask": -1}, {"pass_fds": ()},
               {"stderr": subprocess.STDOUT, "stdout": subprocess.PIPE}):
        assert not need(kw), kw


@pytest.mark.parametrize("platform", ["linux", "freebsd14"])
async def test_elsewhere_than_macos_the_start_is_unchanged(monkeypatch, platform):
    seen = []

    async def fake_exec(program, *args, **kw):
        seen.append((program, args, kw))
        return None
    monkeypatch.setattr(forksafe.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(forksafe.sys, "platform", platform)
    await forksafe.spawn("ffmpeg", "-i", "x", stdout=-1, cwd="/tmp")
    assert seen == [("ffmpeg", ("-i", "x"), {"stdout": -1, "cwd": "/tmp"})]


# ── run: subprocess.run without a fork ──────────────────────────────────────

@pytest.fixture
def tight(monkeypatch):
    """Counts ``run``'s own posix_spawn starts (ctypes — past os.posix_spawn)."""
    n = []
    real = forksafe._spawn_tight
    monkeypatch.setattr(forksafe, "_spawn_tight", lambda *a, **k: n.append(a[0]) or real(*a, **k))
    return n


@darwin
def test_run_does_not_fork_and_behaves_as_subprocess_run(starts, tight, tmp_path):
    r = forksafe.run(["echo", "hi"], capture_output=True, text=True)
    assert (r.returncode, r.stdout, r.stderr) == (0, "hi\n", "")
    assert forksafe.run(["/bin/sh", "-c", "exit 3"]).returncode == 3
    with pytest.raises(subprocess.CalledProcessError):
        forksafe.run(["/bin/sh", "-c", "exit 3"], check=True)
    r = forksafe.run(["/bin/cat"], input=b"piped", capture_output=True)
    assert r.stdout == b"piped"
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        forksafe.run(["/bin/sleep", "10"], timeout=0.3)
    assert time.monotonic() - t0 < 3
    with pytest.raises(FileNotFoundError):
        forksafe.run(["no-such-tool-xyz"])
    tool = tmp_path / "mytool"
    tool.write_text("#!/bin/sh\necho found\n")
    tool.chmod(0o755)
    assert forksafe.run(["mytool"], env={"PATH": str(tmp_path)}, capture_output=True).stdout == b"found\n"
    assert starts == {"posix_spawn": 0, "fork": 0} and len(tight) == 6    # the missing one started nothing


@darwin
def test_run_refuses_options_that_would_fork():
    for kw in ({"cwd": "/tmp"}, {"umask": 0}, {"process_group": 0}, {"start_new_session": True},
               {"close_fds": True}, {"shell": True}, {"pass_fds": (3,)}):
        with pytest.raises(ValueError):
            forksafe.run(["/bin/echo"], **kw)


# The descriptors open in the child that runs this (fstat of 0-255: opens nothing itself).
_OPEN_FDS = ("import os\nfor fd in range(256):\n    try:\n        os.fstat(fd)\n"
             "        print(fd)\n    except OSError:\n        pass\n")


@darwin
def test_a_run_child_gets_its_standard_streams_only(monkeypatch, tight, capfd):
    """posix_spawn with CLOEXEC_DEFAULT: an inheritable descriptor reaches no
    run() child — the sweep isn't what keeps it out — its argv[0] stays as
    given, ``executable=`` is honoured, stdout is inherited when not captured
    and ``stderr=STDOUT`` merges."""
    monkeypatch.setattr(forksafe, "_cloexec_locked", lambda: None)
    r, w = os.pipe()
    try:
        os.set_inheritable(w, True)
        assert forksafe.run([sys.executable, "-c", _OPEN_FDS], capture_output=True,
                            text=True).stdout.split() == ["0", "1", "2"]
    finally:
        os.close(r)
        os.close(w)
    assert forksafe.run(["sh", "-c", 'echo "$0"'], capture_output=True).stdout == b"sh\n"
    assert forksafe.run(["whatever-name", "-c", 'echo "$0"'], executable="/bin/sh",
                        capture_output=True).stdout == b"whatever-name\n"
    r2 = forksafe.run(["/bin/sh", "-c", "echo out; echo err >&2"], stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT)
    assert r2.stdout == b"out\nerr\n"
    forksafe.run(["/bin/echo", "inherited"])
    assert capfd.readouterr().out == "inherited\n"


@darwin
def test_run_children_get_nothing_of_concurrent_uvloop_starts():
    """uvloop marks a starting child's stdio ends inheritable for a moment;
    run() children started meanwhile in other threads never get them."""
    import uvloop
    stop = threading.Event()
    leaks, runs = [], []

    def worker():
        while not stop.is_set():
            fds = forksafe.run([sys.executable, "-c", _OPEN_FDS], capture_output=True, text=True).stdout.split()
            runs.append(1)
            if fds != ["0", "1", "2"]:
                leaks.append(fds)

    async def storm():
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            procs = [await asyncio.create_subprocess_exec("/usr/bin/true", stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.PIPE) for _ in range(8)]
            await asyncio.gather(*(p.communicate() for p in procs))
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        uvloop.run(storm())
    finally:
        stop.set()
        for t in threads:
            t.join(10)
    assert runs and leaks == []


@darwin
def test_run_starts_never_pass_each_other_their_pipes():
    """Many threads start at once: none of the children holds another's pipe
    (the start is made under the lock), so each read ends at its own
    child's end — none waits for a longer sibling."""
    slow_done = []
    errors = []

    def slow():
        forksafe.run(["/bin/sh", "-c", "sleep 1.5"], capture_output=True)
        slow_done.append(time.monotonic())

    def quick(i):
        try:
            t0 = time.monotonic()
            forksafe.run(["/bin/echo", str(i)], capture_output=True, timeout=5)
            if time.monotonic() - t0 > 1.0:
                errors.append(i)
        except Exception as exc:                        # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=slow) for _ in range(4)]
    threads += [threading.Thread(target=quick, args=(i,)) for i in range(60)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == [] and len(slow_done) == 4


# ── the guard: nothing starts a process past forksafe ──────────────────────

_BANNED = {
    "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "subprocess.getoutput", "subprocess.getstatusoutput", "subprocess.Popen",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "asyncio.subprocess.create_subprocess_exec", "asyncio.subprocess.create_subprocess_shell",
    "os.system", "os.popen", "os.fork", "os.forkpty", "os.spawnl", "os.spawnle", "os.spawnlp",
    "os.spawnlpe", "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
    "pty.spawn", "pty.fork", "webbrowser.open", "webbrowser.open_new", "webbrowser.open_new_tab",
    "webbrowser.get",
}
_WATCHED = ("subprocess", "asyncio", "os", "pty", "webbrowser")
_FORK_KW = {"cwd", "start_new_session", "preexec_fn", "pass_fds", "process_group", "user", "group",
            "extra_groups", "umask"}


def _starts_past_forksafe(src: str) -> list[int]:
    """Lines of ``src`` that start a process past forksafe: a banned call
    through any alias (``import x as y``, ``from x import y as z``,
    ``y = x`` / ``y = x.f``), or any ``.subprocess_exec`` / ``.subprocess_shell``
    (``loop.subprocess_exec``).  A ``subprocess.Popen`` of a literal absolute
    program with ``close_fds=False`` and no fork option takes CPython's
    posix_spawn path itself and is allowed."""
    tree = ast.parse(src)
    alias: dict[str, str] = {}

    def dotted(node) -> "str | None":
        if isinstance(node, ast.Name):
            return alias.get(node.id, node.id if node.id in _WATCHED else None)
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in _WATCHED:
                    alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in _WATCHED:
            for a in n.names:
                alias[a.asname or a.name] = f"{n.module}.{a.name}"
    for _ in range(3):                                  # y = x / y = x.f, a few levels deep
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                d = dotted(n.value)
                if d and (d.split(".")[0] in _WATCHED):
                    alias[n.targets[0].id] = d
    bad = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        d = dotted(n.func)
        if isinstance(n.func, ast.Attribute) and n.func.attr in ("subprocess_exec", "subprocess_shell"):
            bad.append(n.lineno)
            continue
        if d not in _BANNED:
            continue
        if d == "subprocess.Popen":
            kws = {k.arg: k.value for k in n.keywords}
            first = n.args[0] if n.args else None
            prog = first.elts[0] if isinstance(first, ast.List) and first.elts else None
            if (isinstance(kws.get("close_fds"), ast.Constant) and kws["close_fds"].value is False
                    and isinstance(prog, ast.Constant) and isinstance(prog.value, str)
                    and prog.value.startswith("/") and not (_FORK_KW & set(kws))):
                continue
        bad.append(n.lineno)
    return bad


def test_the_guard_catches_every_way_past_forksafe():
    forms = [
        "import subprocess\nsubprocess.run(['x'])",
        "import subprocess as sp\nsp.check_output(['x'])",
        "from subprocess import run as r\nr(['x'])",
        "import subprocess\nsp = subprocess\nsp.run(['x'])",
        "import subprocess\nrun = subprocess.run\nrun(['x'])",
        "import asyncio.subprocess\nasyncio.subprocess.create_subprocess_exec('x')",
        "from asyncio import subprocess as asp\nasp.create_subprocess_exec('x')",
        "import asyncio\nasyncio.create_subprocess_shell('x')",
        "loop.subprocess_exec(f, 'x')",
        "import os\nos.system('x')",
        "import os\nos.popen('x')",
        "from os import popen\npopen('x')",
        "import webbrowser\nwebbrowser.open('http://x')",
        "import pty\npty.spawn('x')",
        "import subprocess\nsubprocess.Popen(['/usr/bin/open'], close_fds=False, cwd='/')",
        "import subprocess\nsubprocess.Popen(['pwd'], close_fds=False)",
    ]
    for f in forms:
        assert _starts_past_forksafe(f), f
    ok = "import subprocess\nsubprocess.Popen(['/usr/bin/open', '-n', x], close_fds=False)\nimport os\nos.posix_spawn(p, a, e)"
    assert _starts_past_forksafe(ok) == []


def test_the_server_and_the_app_start_every_process_through_forksafe():
    pkg = pathlib.Path(forksafe.__file__).resolve().parents[1]
    files = [p for p in pkg.rglob("*.py") if p.name != "forksafe.py"]
    app = pkg.parent / "soniqboom_app.py"               # local-only packaging file, when present
    if app.exists():
        files.append(app)
    bad = [f"{p.name}:{line}" for p in files for line in _starts_past_forksafe(p.read_text(encoding="utf-8"))]
    assert bad == []


def test_the_forkserver_relaunch_and_the_sweep_share_one_lock():
    from soniqboom.core import scanner
    assert scanner._spawn_lock is forksafe.spawn_lock


@darwin
def test_the_forkserver_relaunch_passes_its_descriptors_only(monkeypatch):
    from soniqboom.core import scanner
    stray_r, stray_w = os.pipe()
    pass_r, pass_w = os.pipe()
    seen = {}

    def fake_spawn(path, args, env):
        seen["stray"] = os.get_inheritable(stray_w)
        seen["passed"] = os.get_inheritable(pass_r)
        return 4242
    try:
        os.set_inheritable(stray_w, True)               # a C library's descriptor
        monkeypatch.setattr(scanner.os, "posix_spawn", fake_spawn)
        assert scanner._spawnv_passfds_nofork("/bin/true", ["true"], [pass_r]) == 4242
        assert seen == {"stray": False, "passed": True}
        assert os.get_inheritable(pass_r) is False      # back after the start
    finally:
        for fd in (stray_r, stray_w, pass_r, pass_w):
            os.close(fd)
