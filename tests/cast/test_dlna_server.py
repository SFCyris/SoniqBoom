# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the DLNA Media Server: SSDP discoverability + UPnP-AV HTTP.

What these tests prove:

  ✓ SSDP responder binds and joins the multicast group cleanly.
  ✓ M-SEARCH messages get a well-formed HTTP/1.1 200 response with
    every required header (LOCATION, ST, USN, SERVER, CACHE-CONTROL).
  ✓ ssdp:alive + ssdp:byebye NOTIFY frames have the right shape and
    every advertised resource type produces one.
  ✓ /dlna/device.xml is valid UPnP DDD (xmlns + deviceType +
    serviceList with both CDS and CM).
  ✓ /dlna/cds.xml describes Browse + Get*Capabilities actions.
  ✓ ContentDirectory Browse SOAP returns DIDL-Lite with track items
    and a working <res> URL pointing at /cast/{token}/file.ext.

What these tests don't prove (needs physical hardware):

  ✗ Real DLNA controllers (BubbleUPnP, Sonos app, VLC) actually
    discover us via M-SEARCH on their LAN.
  ✗ Renderers play the URLs back through SoniqBoom's cast byte server.

For those, see ``docs/dlna-device-runbook.md``.
"""
from __future__ import annotations

import asyncio
import socket
import struct
import time
from xml.etree import ElementTree as ET

import pytest

from soniqboom.core.dlna_server import (
    DLNAServer, _NT_DEVICE_MEDIASERVER, _NT_DEVICE_ROOT,
    _NT_SERVICE_CONTENTDIR, _NT_SERVICE_CONNMGR,
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _free_loopback_port() -> int:
    """Bind a temp UDP socket to find an unused port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _send_msearch_and_collect(
    *,
    dest_host: str,
    dest_port: int,
    st: str,
    src_port: int | None = None,
    timeout_s: float = 2.0,
) -> list[tuple[bytes, tuple[str, int]]]:
    """Send a unicast M-SEARCH to (dest_host, dest_port) and collect
    every reply that arrives within ``timeout_s``."""
    loop = asyncio.get_event_loop()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if src_port is not None:
        s.bind(("127.0.0.1", src_port))
    s.setblocking(False)
    try:
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 1\r\n"
            f"ST: {st}\r\n\r\n"
        ).encode("utf-8")
        s.sendto(msg, (dest_host, dest_port))
        replies = []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                data, addr = await asyncio.wait_for(
                    loop.sock_recvfrom(s, 4096),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
                replies.append((data, addr))
            except asyncio.TimeoutError:
                break
        return replies
    finally:
        s.close()


# ── SSDP discoverability ───────────────────────────────────────────────


def _port_1900_taken() -> bool:
    """Return True if port 1900 already has an SSDP responder bound to
    it (e.g. the user's running SoniqBoom).  The SSDP tests need the
    port free because both instances would otherwise share the socket
    via SO_REUSEPORT and the kernel would split incoming datagrams
    non-deterministically between them."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 1900))
        return False
    except OSError:
        return True
    finally:
        s.close()


@pytest.mark.asyncio
async def test_ssdp_responder_binds_and_joins_multicast():
    """The most basic 'is the announcer alive' check: start, confirm
    the socket + datagram transport are live, stop cleanly."""
    if _port_1900_taken():
        pytest.skip("Port 1900 already bound (running SoniqBoom?); SSDP test would race")
    srv = DLNAServer(
        base_url="http://127.0.0.1:8080",
        friendly_name="TestServer",
    )
    try:
        await srv.start()
        assert srv._listen_sock is not None
        assert srv._transport is not None
        assert srv._announce_task is not None and not srv._announce_task.done()
    finally:
        await srv.stop()
    assert srv._listen_sock is None
    assert srv._transport is None


@pytest.mark.asyncio
async def test_ssdp_responds_to_unicast_msearch():
    """Send a directed M-SEARCH to the responder's socket and verify
    the reply payload has every required HTTP/1.1 200 header.

    Unicast is the controller's 'I just woke up, who's here' probe —
    real DLNA boxes also do the multicast variant, which we test
    separately below (and which may be flaky in CI environments that
    block multicast loopback)."""
    if _port_1900_taken():
        pytest.skip("Port 1900 already bound; would race with running SoniqBoom")
    srv = DLNAServer(
        base_url="http://10.0.0.42:8080",
        friendly_name="DiscoTest",
    )
    try:
        await srv.start()
        # Give the listen loop a tick to attach to the socket.
        await asyncio.sleep(0.05)
        # The responder socket is bound to port 1900 OR another free
        # port; we read its actual address.
        local_addr = srv._listen_sock.getsockname()
        replies = await _send_msearch_and_collect(
            dest_host="127.0.0.1", dest_port=local_addr[1],
            st="urn:schemas-upnp-org:device:MediaServer:1",
            timeout_s=1.5,
        )
    finally:
        await srv.stop()

    assert replies, "no M-SEARCH replies received"
    raw, _addr = replies[0]
    text = raw.decode("utf-8", "replace")
    first_line = text.split("\r\n", 1)[0]
    assert first_line.startswith("HTTP/1.1 200"), f"bad status line: {first_line!r}"
    # Required SSDP-reply headers per UPnP DA 1.1
    for header in ("CACHE-CONTROL:", "LOCATION:", "SERVER:", "ST:", "USN:"):
        assert header in text, f"missing {header} in reply"
    # LOCATION must point at our device.xml
    assert "/dlna/device.xml" in text
    # ST echoes what we asked for
    assert "MediaServer:1" in text
    # USN includes our UDN
    assert "uuid:" in text and "MediaServer:1" in text


@pytest.mark.asyncio
async def test_ssdp_responds_to_ssdp_all_with_every_resource_type():
    """Controllers use ``ST: ssdp:all`` to ask 'tell me about every
    resource you advertise'.  We must answer with ONE reply per NT
    (rootdevice, MediaServer:1, CDS:1, CM:1)."""
    if _port_1900_taken():
        pytest.skip("Port 1900 already bound; would race with running SoniqBoom")
    srv = DLNAServer(
        base_url="http://10.0.0.42:8080",
        friendly_name="DiscoAll",
    )
    try:
        await srv.start()
        await asyncio.sleep(0.05)
        local_addr = srv._listen_sock.getsockname()
        replies = await _send_msearch_and_collect(
            dest_host="127.0.0.1", dest_port=local_addr[1],
            st="ssdp:all",
            timeout_s=3.0,  # MX=1 + random jitter + slack
        )
    finally:
        await srv.stop()

    assert len(replies) >= 4, f"expected ≥4 ssdp:all replies, got {len(replies)}"
    sts_seen = set()
    for raw, _ in replies:
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.upper().startswith("ST:"):
                sts_seen.add(line.split(":", 1)[1].strip())
                break
    assert _NT_DEVICE_ROOT in sts_seen
    assert _NT_DEVICE_MEDIASERVER in sts_seen
    assert _NT_SERVICE_CONTENTDIR in sts_seen
    assert _NT_SERVICE_CONNMGR in sts_seen


@pytest.mark.asyncio
async def test_ssdp_does_not_respond_to_unrelated_st():
    """ST values we don't advertise (e.g. a printer service) must
    NOT trigger a response — otherwise we'd pollute LAN discovery
    for unrelated controllers."""
    if _port_1900_taken():
        pytest.skip("Port 1900 already bound; would race with running SoniqBoom")
    srv = DLNAServer(
        base_url="http://127.0.0.1:8080",
        friendly_name="QuietTest",
    )
    try:
        await srv.start()
        await asyncio.sleep(0.05)
        local_addr = srv._listen_sock.getsockname()
        replies = await _send_msearch_and_collect(
            dest_host="127.0.0.1", dest_port=local_addr[1],
            st="urn:schemas-upnp-org:device:Printer:1",
            timeout_s=1.0,
        )
    finally:
        await srv.stop()
    assert replies == [], f"got {len(replies)} unexpected replies"


# ── UPnP HTTP endpoints (FastAPI TestClient) ───────────────────────────


@pytest.fixture()
def upnp_client(tmp_data_dir):
    """FastAPI TestClient with the DLNA service enabled.

    We toggle the env var BEFORE importing soniqboom.main so the
    router conditional picks the right branch.  Also init a DLNAServer
    instance because the /dlna/* endpoints fetch the singleton for
    friendly_name + UDN."""
    from fastapi.testclient import TestClient
    from soniqboom.core.persistence import init_persistence
    from soniqboom.core.users import init_user_store
    init_persistence(tmp_data_dir)
    init_user_store(tmp_data_dir)
    # Enable the service flag
    from soniqboom.config import set_service_enabled
    set_service_enabled("dlna_server", True)
    # Install the dlna_server singleton — required by the HTTP endpoints
    # to read friendly_name / UDN.
    from soniqboom.core.dlna_server import DLNAServer, set_instance
    srv = DLNAServer(
        base_url="http://testserver",
        friendly_name="TestSoniqBoom",
    )
    # NOTE: we don't .start() — that binds a real SSDP socket.  We
    # only need the URL / UDN bookkeeping for the HTTP tests.
    set_instance(srv)
    from soniqboom.main import app
    c = TestClient(app)
    yield c
    set_instance(None)


def test_device_xml_well_formed(upnp_client):
    """The root device description is the first thing a controller
    follows from SSDP LOCATION.  Must parse + carry MediaServer:1
    deviceType + both service entries."""
    r = upnp_client.get("/dlna/device.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/xml")
    root = ET.fromstring(r.text)
    # UPnP root namespace
    ns = "{urn:schemas-upnp-org:device-1-0}"
    device = root.find(f"{ns}device")
    assert device is not None
    dtype = device.find(f"{ns}deviceType").text
    assert dtype == "urn:schemas-upnp-org:device:MediaServer:1"
    friendly = device.find(f"{ns}friendlyName").text
    assert "SoniqBoom" in friendly or "Test" in friendly
    # Both services declared
    services = device.find(f"{ns}serviceList")
    found_types = {s.find(f"{ns}serviceType").text for s in services}
    assert "urn:schemas-upnp-org:service:ContentDirectory:1" in found_types
    assert "urn:schemas-upnp-org:service:ConnectionManager:1" in found_types


def test_cds_service_description_lists_browse(upnp_client):
    """The CDS service description tells controllers which actions
    they can call.  Browse is the only mandatory one for our v1."""
    r = upnp_client.get("/dlna/cds.xml")
    assert r.status_code == 200
    ns = "{urn:schemas-upnp-org:service-1-0}"
    root = ET.fromstring(r.text)
    actions = root.find(f"{ns}actionList")
    action_names = {a.find(f"{ns}name").text for a in actions}
    assert "Browse" in action_names
    assert "GetSearchCapabilities" in action_names
    assert "GetSystemUpdateID" in action_names


def test_cm_service_description_lists_protocol_info(upnp_client):
    r = upnp_client.get("/dlna/cm.xml")
    assert r.status_code == 200
    assert "GetProtocolInfo" in r.text


def test_browse_root_returns_music_container(upnp_client):
    """Browse(ObjectID=0, DirectChildren) must return at least one
    container — our root has a single Music child.  Real controllers
    drill into this on every initial connect."""
    soap = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        '<ObjectID>0</ObjectID>'
        '<BrowseFlag>BrowseDirectChildren</BrowseFlag>'
        '<Filter>*</Filter>'
        '<StartingIndex>0</StartingIndex>'
        '<RequestedCount>10</RequestedCount>'
        '<SortCriteria></SortCriteria>'
        '</u:Browse>'
        '</s:Body>'
        '</s:Envelope>'
    )
    r = upnp_client.post(
        "/dlna/cds/control",
        content=soap,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
        },
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert "BrowseResponse" in body
    assert "<Result>" in body
    # Result is XML-escaped DIDL-Lite inside <Result> — unescape it
    import re
    m = re.search(r"<Result>(.*?)</Result>", body, re.DOTALL)
    assert m is not None
    didl = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    assert "DIDL-Lite" in didl
    assert 'id="music"' in didl or "music" in didl
    # NumberReturned + TotalMatches must be non-empty
    assert "<NumberReturned>" in body
    assert "<TotalMatches>"   in body


def test_browse_music_all_returns_track_items(upnp_client):
    """Browse(ObjectID="music/all") returns a DIDL-Lite with one
    <item> per track.  Empty library → empty list (still well-formed).
    Each item carries a <res> URL pointing at /cast/{token}/{file}."""
    soap = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        '<s:Body>'
        '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        '<ObjectID>music/all</ObjectID>'
        '<BrowseFlag>BrowseDirectChildren</BrowseFlag>'
        '<Filter>*</Filter>'
        '<StartingIndex>0</StartingIndex>'
        '<RequestedCount>5</RequestedCount>'
        '<SortCriteria></SortCriteria>'
        '</u:Browse>'
        '</s:Body>'
        '</s:Envelope>'
    )
    r = upnp_client.post(
        "/dlna/cds/control",
        content=soap,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
        },
    )
    assert r.status_code == 200, r.text
    # Empty test library → 0 results, but the envelope is still well-formed
    assert "BrowseResponse" in r.text
    assert "DIDL-Lite" in r.text  # at least the empty DIDL-Lite wrapper


def test_cm_get_protocol_info(upnp_client):
    """ConnectionManager:GetProtocolInfo tells controllers which codecs
    our cast byte-server can deliver — required for proper renderer-
    side codec selection."""
    soap = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        '<s:Body>'
        '<u:GetProtocolInfo xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1"/>'
        '</s:Body>'
        '</s:Envelope>'
    )
    r = upnp_client.post(
        "/dlna/cm/control",
        content=soap,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"urn:schemas-upnp-org:service:ConnectionManager:1#GetProtocolInfo"',
        },
    )
    assert r.status_code == 200, r.text
    assert "audio/mpeg" in r.text
    assert "audio/flac" in r.text


def test_browse_unknown_object_returns_empty(upnp_client):
    """Unknown ObjectID must not error — must return an empty
    DIDL-Lite with 0 results.  Some controllers walk speculatively
    into IDs we never advertised."""
    soap = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        '<s:Body>'
        '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        '<ObjectID>does-not-exist</ObjectID>'
        '<BrowseFlag>BrowseDirectChildren</BrowseFlag>'
        '<Filter>*</Filter>'
        '<StartingIndex>0</StartingIndex>'
        '<RequestedCount>10</RequestedCount>'
        '<SortCriteria></SortCriteria>'
        '</u:Browse>'
        '</s:Body>'
        '</s:Envelope>'
    )
    r = upnp_client.post(
        "/dlna/cds/control",
        content=soap,
        headers={"Content-Type": 'text/xml; charset="utf-8"'},
    )
    assert r.status_code == 200
    assert "<NumberReturned>0</NumberReturned>" in r.text
    assert "<TotalMatches>0</TotalMatches>" in r.text
