# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-3 review fixes in the admin API:

* a signed-in NON-admin gets 403 from the admin routes (401 stays for a
  missing or invalid session), so the web client doesn't mistake it for an
  expired session and show the sign-in overlay;
* the log viewer redacts credential query strings at read time, so lines
  logged before the access-log filter existed don't leak passwords/tokens;
* switching "Guess game from Modland file names" ON while an apply is
  running queues a follow-up apply instead of reporting a failure;
* /admin/reindex retries an index rebuild whose swap was skipped for
  concurrent store writes, and never claims a heal for a skipped swap."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.api import admin
from soniqboom.core import folder_album as fa
from soniqboom.core import scene_metadata as sm
from soniqboom.core import users
from soniqboom.core.store import TrackStore


async def _no_refresh(_ids):
    """Stand-in for ``folder_album.refresh_album_caches`` (which would touch
    the real data dir's browse cache file)."""
    return None


# ── r3-enr-13: admin routes answer 403 to a non-admin session ────────────────

@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    ustore = users.UserStore(tmp_path)
    monkeypatch.setattr(users, "_instance", ustore)
    toks = {}
    for name, role in (("adm1", "admin"), ("ed1", "edit"), ("ro1", "readonly")):
        u = ustore.create(name, "Password123!x", role)
        toks[role] = ustore.issue_session(u.id)[0]
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    return TestClient(app), toks


@pytest.mark.parametrize("role,code", [("admin", 200), ("edit", 403), ("readonly", 403)])
def test_admin_route_status_by_role(admin_client, role, code):
    client, toks = admin_client
    client.cookies.set("sb_session", toks[role])
    r = client.get("/api/admin/metadata/repair-status")
    assert r.status_code == code, r.text


def test_admin_route_without_a_valid_session_is_401(admin_client):
    client, _ = admin_client
    assert client.get("/api/admin/metadata/repair-status").status_code == 401
    client.cookies.set("sb_session", "not-a-session")
    assert client.get("/api/admin/metadata/repair-status").status_code == 401


# ── r3-enr-15: the log viewer redacts credentials at read time ───────────────

def test_log_viewer_redacts_old_credential_lines(admin_client, tmp_path, monkeypatch):
    client, toks = admin_client
    logdir = tmp_path / "data" / "log"
    logdir.mkdir(parents=True)
    (logdir / "soniqboom.log").write_text(
        'INFO GET /rest/stream.view?u=bob&p=hunter2&t=abc123&s=salt9&c=dsub HTTP/1.1" 200\n'
        "INFO plain line without a query\n"
        "WARNING fetch failed for /api/cast/stream?token=tok-secret-77 (timeout)\n")
    monkeypatch.setattr("soniqboom.config.get_data_dir", lambda: tmp_path / "data")
    client.cookies.set("sb_session", toks["admin"])
    lines = client.get("/api/admin/logs").json()["lines"]
    text = "\n".join(lines)
    for secret in ("hunter2", "abc123", "salt9", "tok-secret-77"):
        assert secret not in text
    assert "u=bob&p=***&t=***&s=***&c=dsub" in text
    assert "token=***" in text
    assert "plain line without a query" in text


# ── r3-enr-7: switch-on while an apply is running ───────────────────────────

@pytest.fixture
def busy_env(tmp_path, monkeypatch):
    s = TrackStore()
    for target in ("soniqboom.core.store.get_store", "soniqboom.core.data.get_store"):
        monkeypatch.setattr(target, lambda: s)
    monkeypatch.setattr(sm, "has_index", lambda: True)
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    s.set_config(sm.MODLAND_FILENAME_CONFIG_KEY, False)
    monkeypatch.setitem(sm._status, "applying", True)       # an apply holds the join
    spawned = []
    from soniqboom.core import scanner
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda: spawned.append(1))
    return s, spawned


async def test_switch_on_during_a_running_apply_queues_a_follow_up(busy_env):
    s, spawned = busy_env
    res = await admin.update_settings({"modland_filename_game": True})
    assert "album_pass_error" not in res
    assert res["started"] is True
    assert spawned == [1]
    assert s.get_config(sm.MODLAND_FILENAME_CONFIG_KEY) is True


async def test_other_apply_errors_are_still_reported(busy_env, monkeypatch):
    s, spawned = busy_env
    monkeypatch.setitem(sm._status, "applying", False)

    async def failing(**kw):
        return {"error": "apply failed: index unreadable"}
    monkeypatch.setattr(sm, "apply_to_library", failing)
    res = await admin.update_settings({"modland_filename_game": True})
    assert res["album_pass_error"] == "apply failed: index unreadable"
    assert "started" not in res and spawned == []


# ── r3-enr-1 (admin part): a skipped index swap is retried, then reported ────

@pytest.mark.parametrize("skips,expect_calls,reindexed", [(0, 1, True), (2, 3, True),
                                                          (5, 3, False)])
async def test_reindex_retries_a_swap_skipped_for_concurrent_writes(
        monkeypatch, skips, expect_calls, reindexed):
    from soniqboom.core import index_health
    calls, recorded = [], []

    async def rebuild():
        calls.append(1)
        if len(calls) <= skips:
            return {"index_ok": True, "skipped": "concurrent-mutation", "mismatches": []}
        return {"index_ok": True, "mismatches": [], "track_count": 1, "mutation_seq": 9}

    async def no_dirs():
        return []
    monkeypatch.setattr(admin, "rebuild_indexes", rebuild)
    monkeypatch.setattr(admin, "list_scan_dirs", no_dirs)
    monkeypatch.setattr(index_health, "record", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr("soniqboom.api.library.invalidate_agg_cache", lambda: None)
    res = await admin.admin_reindex()
    assert len(calls) == expect_calls
    assert res["reindexed"] is reindexed
    assert res["reindex_skipped"] == (None if reindexed else "concurrent-mutation")
    assert len(recorded) == (1 if reindexed else 0)      # no heal claimed for a skip
