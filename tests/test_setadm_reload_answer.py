# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``soniqboom-setadm``'s reload knock, end to end but in-process: the
server's answer to a token-authenticated ``POST /api/auth/reload`` carries the
data dir, and setadm's reading of that answer (``_reload_outcome``) says the
change is live — the documented Docker first run included.  Neither setadm
nor a server process runs (``TestClient`` with a loopback peer)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from soniqboom import cli_setadm, main
from soniqboom.core import startup_status
from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore

_TOK = "the-run-token-0123456789abcdefghij"
PW = "Longenough-1"


@pytest.fixture
def server(tmp_data_dir, monkeypatch):
    monkeypatch.setattr(startup_status, "_status",
                        {**startup_status._status, "reload_token": _TOK})

    def start(pre):
        if pre:
            UserStore(tmp_data_dir).create(pre, PW, "admin")
        st = UserStore(tmp_data_dir, persist_sessions=True)
        monkeypatch.setattr(users_mod, "_instance", st)
        return st
    return start


def _knock(data_dir):
    c = TestClient(main.app, client=("127.0.0.1", 50123))
    return c.post("/api/auth/reload", headers={startup_status.RELOAD_TOKEN_HEADER: _TOK})


@pytest.mark.parametrize("pre", [None, "root1"], ids=["first-admin", "second-user"])
def test_setadm_reports_live_after_a_successful_reload(server, tmp_data_dir, pre):
    st = server(pre)
    UserStore(tmp_data_dir).create("me", PW, "admin")       # setadm's write (another store)
    r = _knock(tmp_data_dir)
    assert r.status_code == 200
    assert r.json()["data_dir"] == str(tmp_data_dir.absolute())
    out = cli_setadm._reload_outcome(r.content, tmp_data_dir, 8471)
    assert out.startswith("Notified the running server (port 8471) — the change is live now")
    assert st.authenticate("me", PW) is not None


def test_setadm_parses_answers_that_are_not_live(tmp_data_dir, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    for raw in (b"not json", b"{}", b'{"data_dir": null}', b"[1]"):
        assert "isn't a SoniqBoom server" in cli_setadm._reload_outcome(raw, tmp_data_dir, 1)
    out = cli_setadm._reload_outcome(('{"data_dir": "%s"}' % other).encode(), tmp_data_dir, 1)
    assert f"serves {other}, not {tmp_data_dir}" in out


def test_the_status_page_still_hides_the_data_dir_from_anonymous_callers(server, tmp_data_dir):
    server("root1")
    lan = TestClient(main.app, client=("192.168.1.50", 50123))
    assert lan.get("/api/auth/status").json()["data_dir"] is None
    assert lan.get("/api/auth/status", params={"cli": "1", "_cli": "1"}).json()["data_dir"] is None
