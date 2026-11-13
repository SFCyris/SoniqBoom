# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Discogs personal access token (gear → Metadata → Discogs): saved to
SoniqBoom.conf encrypted (``discogs_token_enc``) and removed by ``/admin/discogs``,
never sent back, refused while SONIQBOOM_DISCOGS_TOKEN sets it, and checked
with Discogs by ``/admin/discogs/test`` (the typed token, else the saved one;
in the Authorization header, never the URL; nothing sent in offline mode)."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom import config
from soniqboom.api import admin
from soniqboom.config import settings
from soniqboom.core import netpolicy, nowplaying_art

TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789wxyz"


@pytest.fixture
def client(tmp_path, monkeypatch):
    conf = tmp_path / "SoniqBoom.conf"
    conf.write_text(json.dumps({"radio_art_lookup": True, "discogs_token": "OldPlainToken123"}))
    monkeypatch.setattr(nowplaying_art, "forget_misses", lambda: 0)
    real = config.update_local_conf
    monkeypatch.setattr(config, "update_local_conf", lambda fn: real(fn, path=conf))
    monkeypatch.setattr(settings, "discogs_token", "")
    monkeypatch.delenv("SONIQBOOM_DISCOGS_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))          # (no ~/.soniqboom.env)
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    app.dependency_overrides[admin._require_token] = lambda: "t"
    c = TestClient(app)
    c.conf = conf
    return c


@pytest.fixture
def discogs(monkeypatch):
    """Discogs' identity endpoint, faked: records each request; a token is
    accepted when it is ``TOKEN``."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.headers.get("Authorization") == f"Discogs token={TOKEN}":
            return httpx.Response(200, json={"id": 1, "username": "scener"})
        return httpx.Response(401, json={"message": "You must authenticate."})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(netpolicy, "blocked", lambda target=None: False)
    return seen


def test_save_shows_only_the_last_four_and_applies_at_once(client):
    st = client.get("/api/admin/discogs").json()
    assert (st["set"], st["ends_with"], st["from_env"]) == (False, "", False)
    r = client.put("/api/admin/discogs", json={"token": f"  {TOKEN} "})
    assert r.status_code == 200
    assert (r.json()["set"], r.json()["ends_with"]) == (True, "wxyz")
    assert TOKEN not in r.text and TOKEN not in client.get("/api/admin/discogs").text
    conf = json.loads(client.conf.read_text())
    assert TOKEN not in client.conf.read_text()                     # (encrypted)
    assert "discogs_token" not in conf                              # (the plain one replaced)
    from soniqboom.core.credentials import decrypt
    assert decrypt(conf["discogs_token_enc"]) == TOKEN
    assert conf["radio_art_lookup"] is True                         # (kept)
    assert settings.discogs_token == TOKEN


def test_remove(client):
    client.put("/api/admin/discogs", json={"token": TOKEN})
    r = client.put("/api/admin/discogs", json={"token": ""})
    assert r.json()["set"] is False and settings.discogs_token == ""
    conf = json.loads(client.conf.read_text())
    assert "discogs_token" not in conf and "discogs_token_enc" not in conf


@pytest.mark.parametrize("body", [{}, {"token": None}, {"token": 5}, []])
def test_a_body_without_a_token_changes_nothing(client, body):
    assert client.put("/api/admin/discogs", json=body).status_code in (400, 422)
    assert json.loads(client.conf.read_text())["discogs_token"] == "OldPlainToken123"


def test_saving_forgets_the_misses(client, monkeypatch):
    calls = []
    monkeypatch.setattr(nowplaying_art, "forget_misses", lambda: calls.append(1) or 0)
    client.put("/api/admin/discogs", json={"token": TOKEN})
    client.put("/api/admin/discogs", json={"token": ""})
    assert calls == [1]                                 # (only when a token is saved)


def test_forget_misses_keeps_found_covers(tmp_path, monkeypatch):
    monkeypatch.setattr(nowplaying_art, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(nowplaying_art, "_mem", {"a|b": {"found": False}, "c|d": {"found": True}})
    (tmp_path / "a__b.json").write_text(json.dumps({"found": False}))
    (tmp_path / "c__d.json").write_text(json.dumps({"found": True, "result": {}}))
    (tmp_path / "junk.json").write_text("[1, 2]")
    assert nowplaying_art.forget_misses() == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c__d.json", "junk.json"]
    assert list(nowplaying_art._mem) == ["c|d"]


def test_an_unreadable_conf_is_a_409(client):
    client.conf.write_text("{ not json")
    r = client.put("/api/admin/discogs", json={"token": TOKEN})
    assert r.status_code == 409 and "couldn't be read" in r.json()["detail"]


def test_the_saved_token_is_read_back_at_start(monkeypatch):
    from soniqboom.core.credentials import encrypt
    monkeypatch.setattr(settings, "discogs_token", "")
    monkeypatch.setitem(config._local_conf, "discogs_token_enc", encrypt(TOKEN))
    config._discogs_token_from_conf()
    assert settings.discogs_token == TOKEN
    monkeypatch.setattr(settings, "discogs_token", "FromEnvOrPlain1")   # (that one wins)
    config._discogs_token_from_conf()
    assert settings.discogs_token == "FromEnvOrPlain1"


@pytest.mark.parametrize("bad", ["short", "has space inside1234", "x" * 129, "tok;en=1234567"])
def test_a_malformed_token_is_refused(client, bad):
    r = client.put("/api/admin/discogs", json={"token": bad})
    assert r.status_code == 400
    assert "discogs_token_enc" not in json.loads(client.conf.read_text())


def test_the_environment_wins(client, monkeypatch):
    monkeypatch.setenv("SONIQBOOM_DISCOGS_TOKEN", TOKEN)
    assert client.get("/api/admin/discogs").json()["from_env"] is True
    assert client.put("/api/admin/discogs", json={"token": TOKEN}).status_code == 409


def test_the_env_file_counts_too(client, tmp_path):
    (tmp_path / ".soniqboom.env").write_text(f"export SONIQBOOM_DISCOGS_TOKEN={TOKEN}\n")
    assert client.get("/api/admin/discogs").json()["from_env"] is True


def test_test_checks_the_typed_token_in_the_header(client, discogs):
    r = client.post("/api/admin/discogs/test", json={"token": TOKEN}).json()
    assert r == {"ok": True, "username": "scener"}
    req = discogs[-1]
    assert str(req.url) == nowplaying_art.DISCOGS_IDENTITY_URL      # (no token in the URL)
    assert req.headers["User-Agent"].startswith("SoniqBoom/")
    assert "discogs_token_enc" not in json.loads(client.conf.read_text())  # (testing doesn't save)


def test_test_falls_back_to_the_saved_token(client, discogs, monkeypatch):
    monkeypatch.setattr(settings, "discogs_token", "")
    assert client.post("/api/admin/discogs/test", json={}).json()["ok"] is False   # (none)
    assert not discogs
    client.put("/api/admin/discogs", json={"token": TOKEN})
    assert client.post("/api/admin/discogs/test", json={"token": ""}).json()["ok"] is True


@pytest.mark.parametrize("answer", [httpx.Response(200, text="<html>"),
                                    httpx.Response(200, json=[1, 2]),
                                    httpx.Response(200, json={"id": 1})])
def test_an_answer_without_an_account_is_no_ok(client, monkeypatch, answer):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(lambda r: answer), **kw))
    monkeypatch.setattr(netpolicy, "blocked", lambda target=None: False)
    r = client.post("/api/admin/discogs/test", json={"token": TOKEN})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_a_refused_token(client, discogs):
    r = client.post("/api/admin/discogs/test", json={"token": "Wrong0Token0Here0123"}).json()
    assert r == {"ok": False, "error": "Discogs refused this token."}


def test_offline_mode_sends_nothing(client, discogs, monkeypatch):
    monkeypatch.setattr(netpolicy, "blocked", lambda target=None: True)
    r = client.post("/api/admin/discogs/test", json={"token": TOKEN}).json()
    assert r["ok"] is False and "Offline" in r["error"] and not discogs


def test_unreachable(client, monkeypatch):
    def boom(req):
        raise httpx.ConnectError("no route")
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(boom), **kw))
    monkeypatch.setattr(netpolicy, "blocked", lambda target=None: False)
    r = client.post("/api/admin/discogs/test", json={"token": TOKEN}).json()
    assert r == {"ok": False, "error": "Couldn't reach Discogs."}


@pytest.mark.asyncio
async def test_the_cover_search_sends_the_token_in_the_header(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"results": []})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))
    assert await nowplaying_art._discogs_cover("Artist", "Title", TOKEN) is None
    assert TOKEN not in str(seen[0].url)
    assert seen[0].headers["Authorization"] == f"Discogs token={TOKEN}"
