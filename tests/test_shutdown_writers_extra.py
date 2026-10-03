"""The shutdown's "scans" step also stops one-shot remote freshness checks and
default-tune probes (their writes would otherwise land after the journal is
sealed), and old set-aside AOF records are pruned at startup."""
import asyncio
import os
import time

import pytest

from soniqboom.api import stream
from soniqboom.core import remote_freshness, scanner


@pytest.mark.asyncio
async def test_one_shot_freshness_checks_are_stopped(monkeypatch):
    started = asyncio.Event()

    async def slow_poll(scan_root, *, reason):
        started.set()
        await asyncio.sleep(30)
        return {}
    monkeypatch.setattr(remote_freshness, "_poll_share", slow_poll)
    task = asyncio.ensure_future(remote_freshness.check_now("ftp://h/s:/x", reason="stream_404"))
    await asyncio.wait_for(started.wait(), 2)
    assert task in remote_freshness._oneshot_tasks
    stopped = await scanner.stop_background_writers(timeout=2.0)
    assert stopped >= 1
    assert task.cancelled()
    assert task not in remote_freshness._oneshot_tasks


@pytest.mark.asyncio
async def test_default_tune_probes_are_stopped():
    probe = asyncio.ensure_future(asyncio.sleep(30))
    stream._DEFAULT_PROBES["sw1"] = probe
    try:
        await scanner.stop_background_writers(timeout=2.0)
        assert probe.cancelled()
    finally:
        stream._DEFAULT_PROBES.pop("sw1", None)


def test_old_late_aof_files_are_pruned(tmp_path):
    from soniqboom import main
    old = tmp_path / "library.aof.late-100"
    new = tmp_path / "library.aof.late-200"
    keep = tmp_path / "library.aof"
    for p in (old, new, keep):
        p.write_text("{}\n")
    past = time.time() - main._LATE_AOF_KEEP_S - 60
    os.utime(old, (past, past))
    main._prune_late_aof_files(tmp_path)
    assert not old.exists()
    assert new.exists() and keep.exists()


@pytest.mark.asyncio
async def test_admin_dirs_say_when_a_shares_sign_in_was_refused(monkeypatch):
    from soniqboom.api import admin
    from soniqboom.core import filesource
    import soniqboom.config as config

    async def dirs():
        return [{"path": "ftp://h/s:/", "network_share_id": "s1", "status": "unavailable"},
                {"path": "/local", "status": "ok"}]
    monkeypatch.setattr(admin, "list_scan_dirs", dirs)
    monkeypatch.setattr(config, "load_local_conf",
                        lambda: {"network_shares": {"s1": {"protocol": "ftp", "host": "h"}}})
    monkeypatch.setattr(filesource, "share_retry_wait", lambda key: 600.0)
    got = (await admin.admin_list_dirs(_tok="x"))["dirs"]
    assert got[0]["auth_refused"] is True and got[0]["auth_retry_in_s"] == 600
    assert "auth_refused" not in got[1]
    monkeypatch.setattr(filesource, "share_retry_wait", lambda key: 0.0)
    got = (await admin.admin_list_dirs(_tok="x"))["dirs"]
    assert got[0]["auth_refused"] is False
