# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DLNA (Beta) links work on every renderer, not only the first that played
them (v2.2.0 review, "also worth fixing").

A DLNA Media Server Browse / Search link was bound to the first IP that
fetched it (``cast_tokens.replay_ok``), so a control point's cached link
moved to a second speaker — or played by the phone itself — answered 404.
Such links are anonymous (any LAN device may Browse for a fresh one), so they
are no longer bound; a link a signed-in sender minted for one cast target
still is.  The addresses handed out follow the renderer too: a cast session
calls back on our address of the interface that routes to the renderer, and
an SSDP search is answered with a LOCATION on the interface that routes to
the searcher (Browse links then use the host the control point used).

In-process only: bare routers, a tiny WAV, no network traffic (a UDP
``connect`` sends no packet).
"""
from __future__ import annotations

import asyncio
import html
import re
import wave
from xml.etree import ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.config import settings
from soniqboom.core import cast_session, cast_tokens
from soniqboom.core import store as store_mod
from soniqboom.core.data import path_hash
from soniqboom.core.dlna_server import DLNAServer
from soniqboom.core.store import TrackStore

_BROWSE = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
           '<s:Body><u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
           '<ObjectID>music/all</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag>'
           '<Filter>*</Filter><StartingIndex>0</StartingIndex><RequestedCount>0</RequestedCount>'
           '<SortCriteria></SortCriteria></u:Browse></s:Body></s:Envelope>')


@pytest.fixture
def dlna_app(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "folder_aliases", {})
    root = tmp_path / "lib"
    root.mkdir()
    wav = root / "tone.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 2 * 4410)
    s = TrackStore()
    s._aof = lambda *a, **k: None
    monkeypatch.setattr(store_mod, "_store", s)
    s.upsert_scan_dir(str(root))
    s.upsert_tracks_batch([{"id": "w1", "path": str(wav), "title": "Tone", "artist": "A",
                            "album": "", "format": "WAV", "duration": 0.1,
                            "scan_root_hash": path_hash(str(root)),
                            "dir_hash": path_hash(str(root))}])
    from soniqboom.api import cast_stream, dlna_upnp
    app = FastAPI()
    app.include_router(dlna_upnp.router)
    app.include_router(cast_stream.router)
    return app


def _client(app, ip):
    return TestClient(app, client=(ip, 40000), raise_server_exceptions=False)


def _browsed_path(c) -> str:
    r = c.post("/dlna/cds/control", content=_BROWSE, headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'})
    assert r.status_code == 200, r.text
    didl = html.unescape(re.search(r"<Result>(.*?)</Result>", r.text, re.S).group(1))
    ns = {"d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"}
    url = ET.fromstring(didl).find("d:item/d:res", ns).text
    assert url.startswith("http://testserver/cast/")        # (the host the request used)
    return "/" + url.split("://", 1)[1].split("/", 1)[1]


def test_a_browsed_link_plays_on_every_renderer(dlna_app):
    phone, speaker, tv = (_client(dlna_app, ip) for ip in
                          ("192.168.1.20", "192.168.1.30", "192.168.1.40"))
    path = _browsed_path(phone)
    assert phone.head(path).status_code == 200
    assert speaker.get(path).status_code == 200
    assert speaker.get(path, headers={"Range": "bytes=100-"}).status_code == 206
    # v2.2.0: 404 "Stream link no longer valid." for both
    r = tv.get(path)
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert phone.get(path).status_code == 200
    assert tv.get(path, headers={"Range": "bytes=0-99"}).status_code == 206


def test_a_sender_link_stays_bound_to_its_renderer():
    claims = cast_tokens.verify_token(cast_tokens.issue_token(track_id="w1", user_id="u1"))
    assert cast_tokens.replay_ok(claims, "192.168.1.30")
    assert cast_tokens.replay_ok(claims, "192.168.1.30")
    assert not cast_tokens.replay_ok(claims, "192.168.1.40")
    dlna = cast_tokens.verify_token(cast_tokens.issue_token(track_id="w1", origin="dlna"))
    assert cast_tokens.replay_ok(dlna, "192.168.1.30")
    assert cast_tokens.replay_ok(dlna, "192.168.1.40")


def test_lan_ip_follows_the_route_to_the_renderer(monkeypatch):
    assert cast_session._route_source_ip("127.0.0.1") == "127.0.0.1"
    assert cast_session._pick_lan_ip("127.0.0.1") == "127.0.0.1"
    routes = {"192.168.7.9": "192.168.7.2", "8.8.8.8": "10.8.0.5"}   # (a VPN holds the default)
    monkeypatch.setattr(cast_session, "_route_source_ip", lambda d: routes.get(d))
    assert cast_session._pick_lan_ip("192.168.7.9") == "192.168.7.2"
    assert cast_session._pick_lan_ip("unresolvable.invalid") == "10.8.0.5"
    assert cast_session._pick_lan_ip(None) == "10.8.0.5"
    routes.clear()
    assert cast_session._pick_lan_ip("192.168.7.9") == "127.0.0.1"


def test_server_base_url_is_picked_and_cached_per_renderer(monkeypatch):
    calls = []
    monkeypatch.setattr(cast_session, "_pick_lan_ip",
                        lambda host=None: calls.append(host) or {"tv": "192.168.7.2",
                                                                  "box": "10.1.1.2"}[host])
    monkeypatch.setattr(cast_session, "_BASE_URL_CACHE", {})
    monkeypatch.setattr(settings, "port", 8099)

    async def run():
        return [await cast_session._server_base_url(h) for h in ("tv", "box", "tv", "box")]

    assert asyncio.run(run()) == ["http://192.168.7.2:8099", "http://10.1.1.2:8099"] * 2
    assert calls == ["tv", "box"]


class _Tr:
    def __init__(self):
        self.sent = []

    def sendto(self, msg, addr):
        self.sent.append((msg.decode(), addr))


def test_ssdp_answers_each_searcher_with_a_reachable_location():
    srv = DLNAServer(base_url="http://10.0.0.5:8080", friendly_name="t", udn="uuid:x")
    assert srv.location_for(None) == "http://10.0.0.5:8080/dlna/device.xml"
    assert srv.location_for("127.0.0.1") == "http://127.0.0.1:8080/dlna/device.xml"
    srv._transport = _Tr()
    srv._send_msearch_reply("upnp:rootdevice", ("127.0.0.1", 50000))
    (msg, addr), = srv._transport.sent
    assert addr == ("127.0.0.1", 50000)
    assert "\r\nLOCATION: http://127.0.0.1:8080/dlna/device.xml\r\n" in msg
    # a searcher whose route can't be read gets the startup address (never
    # a name lookup: SSDP hands us an IPv4 literal)
    assert srv.location_for("not-an-address") == "http://10.0.0.5:8080/dlna/device.xml"
    assert srv.location_for("::1") == "http://10.0.0.5:8080/dlna/device.xml"
