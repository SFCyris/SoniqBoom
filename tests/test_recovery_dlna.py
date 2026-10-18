# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovery (users.json unreadable on a set-up install) locks the anonymous
DLNA Media Server too: no Browse / Search of the library, and its user-less
stream links are refused — until accounts load again.  A fresh install (no
accounts ever) and an unlocked server are unchanged."""
from __future__ import annotations

import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from soniqboom.core import users as users_mod
from soniqboom.core.users import UserStore

PW = "Longenough-1"

_BROWSE = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    '<s:Body><u:{act} xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
    '<ObjectID>0</ObjectID><ContainerID>0</ContainerID><BrowseFlag>BrowseDirectChildren</BrowseFlag>'
    '<SearchCriteria>*</SearchCriteria><Filter>*</Filter><StartingIndex>0</StartingIndex>'
    '<RequestedCount>10</RequestedCount><SortCriteria></SortCriteria>'
    '</u:{act}></s:Body></s:Envelope>')


def _recovery(d):
    UserStore(d).create("admin1", PW, role="admin")
    (d / "users.json").write_bytes(b"{")
    (d / "users.json.bak").write_bytes(b"{")
    st = UserStore(d)
    assert st.recovery_required
    return st


@pytest.fixture()
def dlna(tmp_data_dir, monkeypatch):
    from soniqboom.api import dlna_upnp
    from soniqboom.core.dlna_server import DLNAServer, set_instance
    from soniqboom.core.persistence import init_persistence
    from soniqboom.core import store as store_mod
    monkeypatch.setattr(store_mod, "_store", None)
    init_persistence(tmp_data_dir)
    set_instance(DLNAServer(base_url="http://testserver", friendly_name="T"))
    app = FastAPI()
    app.include_router(dlna_upnp.router)
    c = TestClient(app)

    def call(act="Browse"):
        return c.post("/dlna/cds/control", content=_BROWSE.format(act=act),
                      headers={"Content-Type": 'text/xml; charset="utf-8"',
                               "SOAPAction": f'"urn:schemas-upnp-org:service:ContentDirectory:1#{act}"'})
    yield call
    set_instance(None)


@pytest.mark.parametrize("act", ["Browse", "Search"])
def test_dlna_lists_nothing_in_recovery(dlna, tmp_data_dir, monkeypatch, act):
    monkeypatch.setattr(users_mod, "_instance", _recovery(tmp_data_dir))
    r = dlna(act)
    assert r.status_code == 500 and "<errorCode>501</errorCode>" in r.text
    assert f"{act}Response" not in r.text


@pytest.mark.parametrize("act", ["Browse", "Search"])
def test_dlna_lists_as_before_when_unlocked_or_fresh(dlna, tmp_data_dir, monkeypatch, act):
    monkeypatch.setattr(users_mod, "_instance", UserStore(tmp_data_dir))     # fresh install
    assert f"{act}Response" in dlna(act).text
    st = UserStore(tmp_data_dir)
    st.create("admin1", PW, role="admin")
    monkeypatch.setattr(users_mod, "_instance", st)                           # accounts loaded
    assert f"{act}Response" in dlna(act).text


def test_recovery_ends_and_dlna_lists_again(dlna, tmp_data_dir, monkeypatch, tmp_path_factory):
    st = _recovery(tmp_data_dir)
    monkeypatch.setattr(users_mod, "_instance", st)
    assert "<errorCode>501</errorCode>" in dlna().text
    src = tmp_path_factory.mktemp("good")
    UserStore(src).create("rescue", PW, role="admin")
    (tmp_data_dir / "users.json").write_bytes((src / "users.json").read_bytes())
    assert st.check_restored()
    assert "BrowseResponse" in dlna().text


async def _authorize(token):
    from soniqboom.api import cast_stream
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="10.0.0.40"), headers={})
    return await cast_stream._authorize_cast(token, req, bind=False)


@pytest.mark.parametrize("locked", [True, False])
async def test_a_userless_stream_link_is_refused_in_recovery(tmp_data_dir, monkeypatch, locked):
    from soniqboom.api import cast_stream
    from soniqboom.core import cast_tokens

    async def fake_track(tid):
        return {"id": tid, "path": "/x.mp3", "format": "MP3"}
    monkeypatch.setattr(cast_stream, "get_track", fake_track)
    if locked:
        monkeypatch.setattr(users_mod, "_instance", _recovery(tmp_data_dir))
    else:
        st = UserStore(tmp_data_dir)
        st.create("admin1", PW, role="admin")
        monkeypatch.setattr(users_mod, "_instance", st)
    tok = cast_tokens.issue_token(track_id="t-1", ttl_seconds=600)        # DLNA browse: no uid
    if locked:
        with pytest.raises(HTTPException) as e:
            await _authorize(tok)
        assert e.value.status_code == 503
    else:
        claims, track, tid = await _authorize(tok)
        assert tid == "t-1"
