# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Modland's checksum list follows the collection's index: an index refresh
that found new or replaced files refreshes an installed list too — only when
the server has another list than the installed one, outside the job lock,
and once more at the next refresh — and what joins on the list follows."""
from __future__ import annotations

import io
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_modland_catalog import (  # noqa: E402,F401  (fixture)
    SERVED, _enable, _mod_bytes, _zip, env)

from soniqboom.core import modland_catalog as mc  # noqa: E402
from soniqboom.core import netpolicy  # noqa: E402
from soniqboom.core import scene_metadata as sm  # noqa: E402


# ── The list itself: a download only of another list ────────────────────────

class _Resp:
    def __init__(self, content: bytes, headers: dict, status: int = 200):
        self.content, self.headers, self.status = content, headers, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _Modland:
    """The server's list: ``rows`` (md5 → path) under ``headers``."""

    def __init__(self):
        self.rows: dict = {}
        self.headers: dict = {}
        self.calls: list = []
        self.head_status = 200

    def blob(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("allmods_md5.txt",
                        "".join(f"{m} {p}\n" for m, p in self.rows.items()))
        return buf.getvalue()

    def client(self, *a, **k):
        srv = self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def head(self, url):
                srv.calls.append("HEAD")
                return _Resp(b"", dict(srv.headers), srv.head_status)

            def get(self, url):
                srv.calls.append("GET")
                return _Resp(srv.blob(), dict(srv.headers))
        return _Client()


@pytest.fixture
def modland(tmp_data_dir, monkeypatch):
    import httpx
    srv = _Modland()
    monkeypatch.setattr(httpx, "Client", srv.client)
    monkeypatch.setattr(netpolicy, "blocked", lambda target=None: False)
    monkeypatch.setattr(sm, "_status", {**sm._status, "refreshing": False, "error": None,
                                        "index_rows": 0})
    return srv


def _rows() -> int:
    con = sqlite3.connect(sm._db_path())
    try:
        return con.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    finally:
        con.close()


def test_the_list_is_downloaded_only_when_the_server_has_another(modland):
    modland.rows = {"a" * 32: "Protracker/x/one.mod"}
    modland.headers = {"etag": '"v1"'}
    st = sm.refresh_index(if_changed=True)          # none installed: downloaded
    assert not st.get("error") and not st.get("unchanged")
    assert modland.calls == ["GET"] and _rows() == 1
    assert sm.index_validator() == '"v1"'

    modland.rows["b" * 32] = "Protracker/x/two.mod"  # (the server's file, same tag)
    st = sm.refresh_index(if_changed=True)
    assert st.get("unchanged") is True and not st.get("error")
    assert modland.calls == ["GET", "HEAD"] and _rows() == 1
    assert sm._status["refreshing"] is False

    modland.headers = {"etag": '"v2"'}               # another list: taken
    st = sm.refresh_index(if_changed=True)
    assert not st.get("unchanged") and not st.get("error")
    assert modland.calls == ["GET", "HEAD", "HEAD", "GET"] and _rows() == 2
    assert sm.index_validator() == '"v2"'

    st = sm.refresh_index()                          # asked for by hand: always
    assert modland.calls[-1] == "GET" and len(modland.calls) == 5


def test_a_list_without_a_validator_or_an_unreachable_check_is_downloaded(modland):
    modland.rows = {"a" * 32: "Protracker/x/one.mod"}
    modland.headers = {}                             # the server names none
    sm.refresh_index(if_changed=True)
    assert sm.index_validator() == ""
    st = sm.refresh_index(if_changed=True)           # unknown: downloaded again
    assert not st.get("unchanged") and modland.calls == ["GET", "GET"]

    modland.headers = {"last-modified": "Sat, 14 Nov 2026 01:07:18 GMT"}
    sm.refresh_index(if_changed=True)
    assert sm.index_validator() == "Sat, 14 Nov 2026 01:07:18 GMT"
    modland.head_status = 503                        # the check fails: downloaded
    st = sm.refresh_index(if_changed=True)
    assert not st.get("unchanged") and not st.get("error")
    assert modland.calls[-2:] == ["HEAD", "GET"]


def test_an_index_built_before_validators_reads_as_unknown(modland):
    con = sqlite3.connect(sm._db_path())
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.execute("INSERT INTO mods VALUES (?, ?)", ("a" * 32, "x/y.mod"))
    con.commit(); con.close()
    assert sm.has_index() and sm.index_validator() == ""
    modland.rows = {"a" * 32: "x/y.mod", "b" * 32: "x/z.mod"}
    modland.headers = {"etag": '"v1"'}
    st = sm.refresh_index(if_changed=True)
    assert not st.get("unchanged") and modland.calls == ["GET"] and _rows() == 2


def test_offline_nothing_is_asked(modland, monkeypatch):
    monkeypatch.setattr(netpolicy, "blocked", lambda target=None: True)
    st = sm.refresh_index(if_changed=True)
    assert "Offline" in st["error"] and modland.calls == []
    assert sm._status["refreshing"] is False


def test_a_second_refresh_while_one_runs_is_refused(modland, monkeypatch):
    monkeypatch.setattr(sm, "_status", {**sm._status, "refreshing": True})
    st = sm.refresh_index(if_changed=True)
    assert st["error"] == "refresh already running" and modland.calls == []


# ── The collection's index refresh takes the list along ─────────────────────

@pytest.fixture
def follow(monkeypatch):
    """The list refresh an index refresh starts, recorded: ``answer`` is
    what ``scene_metadata.refresh_index`` returns."""
    from soniqboom.core import scanner
    rec = {"calls": [], "locked": [], "scene": 0, "refill": 0,
           "answer": {"error": None, "index_rows": 3}}

    def _refresh(*, if_changed=False):
        rec["calls"].append(if_changed)
        rec["locked"].append(bool(mc._job_lock and mc._job_lock.locked()))
        return dict(rec["answer"])

    async def _refill():
        rec["refill"] += 1
    monkeypatch.setattr(sm, "refresh_index", _refresh)
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply",
                        lambda: rec.__setitem__("scene", rec["scene"] + 1))
    monkeypatch.setattr(mc, "_refill_quietly", _refill)
    monkeypatch.setattr(mc, "_checksums_task", None)
    return rec


async def _on(env, follow) -> None:
    """The catalogue on (its build's own fill not counted), a list installed."""
    await _enable(env)
    follow["refill"] = follow["scene"] = 0
    _install_list()


def _install_list() -> None:
    con = sqlite3.connect(sm._db_path())
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.execute("INSERT INTO mods VALUES (?, ?)", ("a" * 32, "x/y.mod"))
    con.commit(); con.close()


async def _settle() -> None:
    t = mc._checksums_task
    if t is not None:
        await t


def _due(env) -> int:
    return int(env["store"].get_config(mc.CONFIG_CHECKSUMS_DUE, 0) or 0)


@pytest.mark.asyncio
async def test_a_refresh_that_found_changes_refreshes_the_list_twice(env, follow):
    await _on(env, follow)
    mc._checksums_follow({"source": "changelogs", "index_added": 2})
    assert _due(env) == 2
    await _settle()
    assert follow["calls"] == [True]                 # only another list is downloaded
    assert (follow["scene"], follow["refill"], _due(env)) == (1, 1, 1)

    # The next refresh finds nothing new: the list of the night after is taken.
    mc._checksums_follow({"source": "changelogs", "added": 0, "index_added": 0})
    await _settle()
    assert len(follow["calls"]) == 2
    assert (follow["scene"], follow["refill"], _due(env)) == (2, 2, 0)

    # … and then the list is left alone until the index changes again.
    mc._checksums_follow({"source": "changelogs", "added": 0, "removed": 3})
    await _settle()
    assert len(follow["calls"]) == 2 and _due(env) == 0


@pytest.mark.parametrize("res", [
    {"source": "changelogs", "added": 1},
    {"source": "changelogs", "changed": 1},
    {"source": "changelogs", "moved": 1},
    {"source": "changelogs", "index_resized": 1},
    {"source": "changelogs", "index_updated": 1},     # replaced in place, same size
    {"source": "full index"},
])
@pytest.mark.asyncio
async def test_what_counts_as_a_change(env, follow, res):
    await _on(env, follow)
    mc._checksums_follow(res)
    await _settle()
    assert follow["calls"] == [True] and _due(env) == 1


@pytest.mark.asyncio
async def test_the_same_list_or_a_failed_download_stays_due(env, follow):
    await _on(env, follow)
    follow["answer"] = {"error": None, "unchanged": True}
    mc._checksums_follow({"source": "changelogs", "index_added": 1})
    await _settle()
    assert follow["calls"] == [True] and _due(env) == 2
    assert (follow["scene"], follow["refill"]) == (0, 0)

    follow["answer"] = {"error": "Offline mode is on — the Modland index can't be downloaded"}
    mc._checksums_follow({"source": "changelogs"})
    await _settle()
    assert len(follow["calls"]) == 2 and _due(env) == 2
    assert (follow["scene"], follow["refill"]) == (0, 0)

    follow["answer"] = {"error": None, "index_rows": 9}
    mc._checksums_follow({"source": "changelogs"})
    await _settle()
    assert _due(env) == 1 and (follow["scene"], follow["refill"]) == (1, 1)


@pytest.mark.asyncio
async def test_no_installed_list_is_never_installed(env, follow):
    await _enable(env)
    assert not sm.has_index()
    mc._checksums_follow({"source": "full index", "added": 5})
    await _settle()
    assert follow["calls"] == [] and _due(env) == 0


@pytest.mark.asyncio
async def test_nothing_follows_a_refresh_that_did_not_run_or_a_catalogue_off(env, follow):
    _install_list()
    mc._checksums_follow({"source": "full index"})   # the catalogue is off
    mc._checksums_follow(None)                       # the refresh failed
    await _settle()
    assert follow["calls"] == []
    await _enable(env)
    mc._checksums_follow(None)
    await _settle()
    assert follow["calls"] == [] and _due(env) == 0


@pytest.mark.asyncio
async def test_the_index_refresh_starts_it_after_its_job(env, follow):
    await _on(env, follow)
    files = dict(SERVED)
    files["Protracker/Test Artist/new one.mod"] = _mod_bytes(b"new one")
    st = await mc.refresh(local_zip=_zip(env["tmp"] / "allmods2.zip", files))
    assert st["last_result"]["added"] == 1
    await _settle()
    assert follow["calls"] == [True]
    assert follow["locked"] == [False]               # not inside the catalogue's job
    # (Two fills: the index refresh's own, and the one over the new list.)
    assert _due(env) == 1 and follow["refill"] == 2 and follow["scene"] == 1


@pytest.mark.asyncio
async def test_a_failed_index_refresh_leaves_the_list_alone(env, follow):
    await _on(env, follow)
    st = await mc.refresh(local_zip=env["tmp"] / "missing.zip")
    assert st.get("error")
    await _settle()
    assert follow["calls"] == [] and _due(env) == 0
