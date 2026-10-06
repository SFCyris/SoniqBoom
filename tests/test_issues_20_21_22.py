# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub #20 (a progressive SID play kept its temp file's descriptor open),
#21 (pools sized from the host's CPU count, not the container's) and #22
(``uade123 -c`` can't write to uvloop's socketpair stdout on Linux)."""
from __future__ import annotations

import asyncio
import os
import shutil
import struct
import sys
import time

import pytest
import uvloop

from soniqboom.api import stream
from soniqboom.core import conversion_cache, forksafe, procinfo

from test_progressive_renders import SID, _body, _Req, _settle_sid


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


# ── #20 ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_progressive_sid_plays_leave_no_descriptor_open(monkeypatch, tmp_path):
    if not shutil.which("sidplayfp") or not SID.exists():
        pytest.skip("sidplayfp or the SID fixture is missing")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(stream, "_spawn_sid_vu", lambda *a, **k: None)
    plays = 6

    async def play(i):
        key = conversion_cache._cache_key(f"fd{i}", "sid", 0, duration=2)
        r = await stream._serve_sid_progressive(_Req(), SID, 0, 2, key, {}, None, web=False)
        assert r is not None
        assert len(await _body(r)) == 44 + 2 * 88200
        await _settle_sid()

    await play(-1)                                    # warm up lazily opened files
    before = _open_fds()
    for i in range(plays):
        await play(i)
    assert _open_fds() - before <= 1, "a descriptor per play is still open"


# ── #21 ─────────────────────────────────────────────────────────────────────

def test_usable_cpus_follows_the_affinity_not_the_host(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 12)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1, 2, 3}, raising=False)
    monkeypatch.setattr(procinfo, "_cgroup_cpu_limit", lambda: None)
    assert procinfo.usable_cpus() == 4


def test_usable_cpus_is_lowered_to_a_cgroup_quota(monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(12)), raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(procinfo, "_cgroup_cpu_limit", lambda: 3)
    assert procinfo.usable_cpus() == 3


def test_usable_cpus_without_affinity_uses_the_cpu_count(monkeypatch):
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 6)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert procinfo.usable_cpus() == 6


def _fake_cgroup(monkeypatch, files: dict):
    monkeypatch.setattr(procinfo, "_read", lambda path: files.get(path))


@pytest.mark.parametrize("text,want", [("400000 100000", 4), ("150000 100000", 2),
                                       ("max 100000", None), ("50000 100000", 1)])
def test_cgroup_v2_quota_in_a_namespaced_container(monkeypatch, text, want):
    _fake_cgroup(monkeypatch, {"/proc/self/cgroup": "0::/",
                               "/sys/fs/cgroup/cpu.max": text})
    assert procinfo._cgroup_cpu_limit() == want


def test_cgroup_v2_quota_of_the_own_cgroup_and_its_parents(monkeypatch):
    """No cpu.max at the root (a host cgroup namespace, systemd CPUQuota=):
    the process's own cgroup is read, and the smallest quota up the tree wins."""
    _fake_cgroup(monkeypatch, {
        "/proc/self/cgroup": "0::/lxc/101/docker/abc",
        "/sys/fs/cgroup/lxc/101/docker/abc/cpu.max": "max 100000",
        "/sys/fs/cgroup/lxc/101/docker/cpu.max": "600000 100000",
        "/sys/fs/cgroup/lxc/101/cpu.max": "250000 100000",
    })
    assert procinfo._cgroup_cpu_limit() == 3


def test_cgroup_v1_quota(monkeypatch):
    _fake_cgroup(monkeypatch, {
        "/proc/self/cgroup": "4:cpu,cpuacct:/docker/abc\n1:memory:/docker/abc",
        "/sys/fs/cgroup/cpu,cpuacct/docker/abc/cpu.cfs_quota_us": "200000",
        "/sys/fs/cgroup/cpu,cpuacct/docker/abc/cpu.cfs_period_us": "100000",
    })
    assert procinfo._cgroup_cpu_limit() == 2
    _fake_cgroup(monkeypatch, {
        "/proc/self/cgroup": "4:cpu,cpuacct:/",
        "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "-1",
        "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000",
    })
    assert procinfo._cgroup_cpu_limit() is None


def test_every_pool_is_sized_from_the_usable_cpus(monkeypatch):
    from soniqboom.api import tracks
    from soniqboom.core import scanner
    monkeypatch.setattr(procinfo, "usable_cpus", lambda: 4)
    for k in ("SONIQBOOM_SCAN_WORKERS", "SONIQBOOM_RENDER_SLOTS"):
        monkeypatch.delenv(k, raising=False)
    assert scanner._default_scan_workers() == 2
    assert stream._default_render_slots() == 2
    assert tracks._waveform_workers() == 2
    monkeypatch.setattr(procinfo, "usable_cpus", lambda: 12)
    assert (scanner._default_scan_workers(), stream._default_render_slots(),
            tracks._waveform_workers()) == (6, 6, 4)


def test_env_overrides(monkeypatch):
    from soniqboom.core import scanner
    monkeypatch.setattr(procinfo, "usable_cpus", lambda: 4)
    monkeypatch.setenv("SONIQBOOM_SCAN_WORKERS", "7")
    assert scanner._default_scan_workers() == 7
    monkeypatch.setenv("SONIQBOOM_SCAN_WORKERS", "500")
    assert scanner._default_scan_workers() == 64
    monkeypatch.setenv("SONIQBOOM_SCAN_WORKERS", "lots")
    assert scanner._default_scan_workers() == 2
    monkeypatch.setenv("SONIQBOOM_RENDER_SLOTS", "1")
    assert stream._default_render_slots() == 2
    monkeypatch.setenv("SONIQBOOM_RENDER_SLOTS", "5")
    assert stream._default_render_slots() == 5


# ── #22 ─────────────────────────────────────────────────────────────────────

_STDOUT_KIND = ("import os, stat, sys; m = os.fstat(1).st_mode; "
                "sys.stdout.write('fifo' if stat.S_ISFIFO(m) else "
                "'socket' if stat.S_ISSOCK(m) else 'other')")


def _on_uvloop(coro_fn):
    """Run ``coro_fn`` on a fresh uvloop, after one pipe-read start (the
    loop's first one opens a descriptor it keeps — not one per start)."""
    async def run():
        p = await forksafe.spawn_pipe_stdout(sys.executable, "-c", "")
        await p.stdout.read()
        await p.wait()
        forksafe.close_stdout(p)
        return await coro_fn()
    loop = uvloop.new_event_loop()
    try:
        return loop.run_until_complete(run())
    finally:
        loop.close()


def test_spawn_pipe_stdout_gives_the_program_a_pipe():
    async def main():
        p = await forksafe.spawn_pipe_stdout(sys.executable, "-c", _STDOUT_KIND)
        try:
            out = (await p.stdout.read()).decode()
            await p.wait()
        finally:
            forksafe.close_stdout(p)
        return type(asyncio.get_running_loop()).__module__, out
    loop_mod, out = _on_uvloop(main)
    assert loop_mod.startswith("uvloop")
    assert out == "fifo"


@pytest.mark.parametrize("early", [True, False])
def test_spawn_pipe_stdout_closes_its_descriptor(early):
    big = "import sys; sys.stdout.buffer.write(b'x' * 4_000_000)"

    async def main():
        before = _open_fds()
        for _ in range(5):
            p = await forksafe.spawn_pipe_stdout(sys.executable, "-c", big)
            if early:
                await p.stdout.read(10)               # stop reading at once
                p.kill()
            else:
                while await p.stdout.read(65536):     # read to the end
                    pass
            await p.wait()
            forksafe.close_stdout(p)
            forksafe.close_stdout(p)                  # a second call is a no-op
        await asyncio.sleep(0.05)
        return _open_fds() - before
    assert _on_uvloop(main) <= 1


def test_spawn_pipe_stdout_refuses_a_stdout_argument():
    async def main():
        with pytest.raises(ValueError):
            await forksafe.spawn_pipe_stdout(sys.executable, "-c", "", stdout=None)
    _on_uvloop(main)


def test_a_fork_while_the_pipe_is_made_does_not_hold_it_open(monkeypatch):
    """A Python fork (the scanner's process pool on Linux) between the pipe's
    creation and the parent's close of its write end: the forked child must
    not keep the write end, or the reader waits for that child to exit."""
    real_spawn = forksafe.spawn
    kids = []

    async def spawn_with_a_fork(*a, **k):
        pid = os.fork()
        if pid == 0:                                   # the forked child lingers
            time.sleep(3)
            os._exit(0)
        kids.append(pid)
        return await real_spawn(*a, **k)

    async def main():
        monkeypatch.setattr(forksafe, "spawn", spawn_with_a_fork)
        p = await forksafe.spawn_pipe_stdout(sys.executable, "-c", "print(1)")
        monkeypatch.setattr(forksafe, "spawn", real_spawn)
        t0 = time.monotonic()
        try:
            out = await asyncio.wait_for(p.stdout.read(), 10)
            await p.wait()
        finally:
            forksafe.close_stdout(p)
        return out, time.monotonic() - t0
    try:
        out, took = _on_uvloop(main)
    finally:
        for pid in kids:
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except OSError:
                pass
    assert out.strip() == b"1"
    assert took < 1.5, f"the end of the pipe took {took:.1f}s (held by the fork)"


def test_cancelled_while_connecting_stays_a_cancel_and_kills_the_child():
    async def main():
        results, before = [], _open_fds()
        for spins in range(4):
            task = asyncio.ensure_future(
                forksafe.spawn_pipe_stdout(sys.executable, "-c", "import time; time.sleep(30)"))
            while not task.done() and spins >= 0:
                await asyncio.sleep(0)
                spins -= 1
            task.cancel()
            try:
                p = await task
            except asyncio.CancelledError:
                results.append("cancelled")
                continue
            results.append("connected")
            p.kill()
            await p.wait()
            forksafe.close_stdout(p)
        await asyncio.sleep(0.2)
        return results, _open_fds() - before
    results, delta = _on_uvloop(main)
    assert set(results) <= {"cancelled", "connected"}
    assert delta <= 1


def _probe_mod(path):
    """A 4-channel ProTracker module: one audible square note, ~8 s."""
    samples = b"square".ljust(22, b"\0") + struct.pack(">HBBHH", 16, 0, 64, 0, 16)
    samples += (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
    hdr = b"probe".ljust(20, b"\0") + samples + bytes([1, 127]) + b"\0" * 128 + b"M.K."
    pat = bytes([0x01, 0xAC, 0x10, 0x00]) + b"\0" * (64 * 16 - 4)
    path.write_bytes(hdr + pat + bytes([0x7F] * 16 + [0x81] * 16))
    return path


def test_uade_live_render_and_probe_use_a_real_pipe(tmp_path, monkeypatch):
    """Both uade123 ``-c`` runs go through ``spawn_pipe_stdout`` (on macOS a
    socket works too, so the route itself is what's checked) and render."""
    if not shutil.which("uade123"):
        pytest.skip("uade123 is missing")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "uade123_path", shutil.which("uade123"))
    mod = _probe_mod(tmp_path / "probe.mod")
    routed = []
    real = forksafe.spawn_pipe_stdout

    async def spy(*a, **k):
        routed.append(os.path.basename(str(a[0])))
        return await real(*a, **k)
    monkeypatch.setattr(forksafe, "spawn_pipe_stdout", spy)

    async def main():
        before = _open_fds()
        for _ in range(2):
            wav = await stream._render_uade_file(mod, 0, with_vu=False)
            size = wav.stat().st_size
            wav.unlink()
            audible = await stream._tune_audible_run("uade", mod, 0)
        await asyncio.sleep(0.05)
        return size, audible, _open_fds() - before
    size, audible, fd_delta = _on_uvloop(main)
    assert routed.count("uade123") == 4
    assert size > 44 + 5 * 176400                     # several seconds of PCM
    assert audible is True
    assert fd_delta <= 1
