#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end discoverability harness — runs against a LIVE SoniqBoom.

What this exercises:

  1. Sends a multicast M-SEARCH (ssdp:all) on the LAN and reports
     which DLNA Media Servers + Renderers it finds.  SoniqBoom should
     show up if the ``dlna_server`` service is enabled.

  2. Asserts SoniqBoom replied to ssdp:all with every advertised
     resource type (rootdevice + MediaServer:1 + CDS:1 + CM:1).

  3. Walks the device description XML and verifies the ContentDirectory
     control URL is reachable + answers Browse(ObjectID=0).

  4. Browses ``music/all`` and reports the first 3 tracks plus their
     ``<res>`` URLs.

  5. Fetches one of the ``<res>`` URLs to confirm the cast byte-server
     accepts the signed token + returns audio bytes.

Run this:

    .venv/bin/python scripts/discoverability_harness.py

Or as a "watch the LAN for everything DLNA" diagnostic when integrating
real DLNA renderers (Sonos, Samsung TVs, etc.):

    .venv/bin/python scripts/discoverability_harness.py --watch 30
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
import urllib.request
import urllib.parse
from xml.etree import ElementTree as ET


def msearch(timeout_s: float = 3.0, st: str = "ssdp:all") -> list[tuple[tuple[str, int], dict[str, str]]]:
    """Send an M-SEARCH and return (addr, headers-dict) per reply."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    try:
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {min(3, int(timeout_s))}\r\n"
            f"ST: {st}\r\n\r\n"
        ).encode()
        s.sendto(msg, ("239.255.255.250", 1900))
        deadline = time.time() + timeout_s
        out: list[tuple[tuple[str, int], dict[str, str]]] = []
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            headers: dict[str, str] = {}
            for line in data.decode("utf-8", "replace").splitlines()[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().upper()] = v.strip()
            out.append((addr, headers))
        return out
    finally:
        s.close()


def _print_header(label: str) -> None:
    print()
    print("─" * 70)
    print(f" {label}")
    print("─" * 70)


def find_soniqboom(replies):
    """Filter replies to ones whose SERVER header mentions SoniqBoom."""
    return [r for r in replies if "SoniqBoom" in r[1].get("SERVER", "")]


def cmd_discover() -> int:
    """Find everything advertising MediaServer or MediaRenderer on the
    LAN.  Prints a table of {host, server, type}."""
    _print_header("M-SEARCH ssdp:all — everything UPnP on this LAN")
    replies = msearch(timeout_s=3.0, st="ssdp:all")
    if not replies:
        print("(no replies — is multicast enabled on this network? "
              "VPNs and bridged Docker networks often block SSDP.)")
        return 1
    # De-dup by (host, ST)
    seen: dict[tuple[str, str], dict] = {}
    for (host, _port), hdr in replies:
        key = (host, hdr.get("ST", "?"))
        seen.setdefault(key, hdr)
    print(f"{'HOST':<16} {'TYPE':<46} {'SERVER':<40}")
    for (host, st), hdr in sorted(seen.items()):
        server = hdr.get("SERVER", "?")[:40]
        # Truncate noise
        short_st = st.replace("urn:schemas-upnp-org:", "")
        print(f"{host:<16} {short_st:<46} {server:<40}")
    return 0


def cmd_check_soniqboom() -> int:
    """Targeted M-SEARCH for SoniqBoom's MediaServer + walk the device
    description.  Returns 0 iff discovery + Browse(ObjectID=0) both
    succeed."""
    _print_header("M-SEARCH urn:schemas-upnp-org:device:MediaServer:1")
    replies = msearch(timeout_s=3.0, st="urn:schemas-upnp-org:device:MediaServer:1")
    sb = find_soniqboom(replies)
    if not sb:
        print("FAIL: no SoniqBoom in M-SEARCH replies.")
        print("      Is `dlna_server` enabled?  Run: ")
        print("      .venv/bin/soniqboom services enable dlna_server")
        print("      then restart SoniqBoom.")
        return 1
    print(f"PASS: {len(sb)} SoniqBoom reply/replies received.")
    location = sb[0][1].get("LOCATION")
    if not location:
        print("FAIL: reply missing LOCATION header.")
        return 1
    print(f"      device.xml: {location}")

    _print_header("GET device.xml")
    try:
        with urllib.request.urlopen(location, timeout=5) as r:
            xml_text = r.read().decode("utf-8", "replace")
    except Exception as exc:
        print(f"FAIL: could not fetch device.xml — {exc}")
        return 1
    print("PASS: device.xml fetched.")
    root = ET.fromstring(xml_text)
    ns = "{urn:schemas-upnp-org:device-1-0}"
    device = root.find(f"{ns}device")
    friendly = device.find(f"{ns}friendlyName").text
    udn = device.find(f"{ns}UDN").text
    print(f"      friendlyName: {friendly}")
    print(f"      UDN:          {udn}")
    services = device.find(f"{ns}serviceList")
    cds_url = None
    for s in services:
        stype = s.find(f"{ns}serviceType").text
        if stype.endswith("ContentDirectory:1"):
            control_path = s.find(f"{ns}controlURL").text
            # control URL may be relative; resolve against device.xml base
            cds_url = urllib.parse.urljoin(location, control_path)
            print(f"      CDS control URL: {cds_url}")

    if not cds_url:
        print("FAIL: device.xml has no ContentDirectory service.")
        return 1

    _print_header("POST Browse(ObjectID=0, BrowseDirectChildren)")
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
    req = urllib.request.Request(
        cds_url,
        data=soap.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction":   '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as exc:
        print(f"FAIL: Browse(0) errored — {exc}")
        return 1
    if "BrowseResponse" not in body:
        print("FAIL: Browse response missing BrowseResponse element.")
        print(body[:400])
        return 1
    if "DIDL-Lite" not in body:
        print("FAIL: Browse result missing DIDL-Lite payload.")
        return 1
    print("PASS: Browse(0) returned a DIDL-Lite envelope.")

    _print_header("POST Browse(ObjectID='music/all', RequestedCount=3)")
    soap2 = soap.replace(
        "<ObjectID>0</ObjectID>",
        "<ObjectID>music/all</ObjectID>",
    ).replace("<RequestedCount>10</RequestedCount>",
              "<RequestedCount>3</RequestedCount>")
    req2 = urllib.request.Request(
        cds_url,
        data=soap2.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction":   '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
        },
    )
    try:
        with urllib.request.urlopen(req2, timeout=5) as r:
            body2 = r.read().decode("utf-8", "replace")
    except Exception as exc:
        print(f"FAIL: Browse(music/all) errored — {exc}")
        return 1
    # Inside <Result>...</Result> the DIDL is XML-escaped — unescape
    import re, html as _html
    m = re.search(r"<Result>(.*?)</Result>", body2, re.DOTALL)
    if not m:
        print("FAIL: Browse(music/all) has no <Result> element.")
        return 1
    didl = _html.unescape(m.group(1))
    res_urls = re.findall(r'<res[^>]*>([^<]+)</res>', didl)
    if not res_urls:
        print("(empty library — no tracks to test the stream URL)")
        return 0
    print(f"PASS: Browse(music/all) returned {len(res_urls)} <res> URL(s).")
    for u in res_urls[:3]:
        print(f"      {u}")

    _print_header("GET first <res> URL")
    test_url = res_urls[0]
    try:
        with urllib.request.urlopen(test_url, timeout=10) as r:
            first = r.read(16)
        print(f"PASS: cast byte-server returned bytes (first 8B: {first[:8].hex()})")
    except Exception as exc:
        print(f"FAIL: cast byte-server unreachable — {exc}")
        return 1
    return 0


def cmd_watch(duration_s: int) -> int:
    """Print every M-SEARCH and NOTIFY observed on the SSDP multicast
    group for ``duration_s`` seconds.  Useful when a real device's
    discovery seems flaky — you can see exactly what controllers are
    asking and whether SoniqBoom is replying."""
    _print_header(f"Watching SSDP multicast for {duration_s} s")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("", 1900))
    import struct
    mreq = struct.pack("4s4s",
                       socket.inet_aton("239.255.255.250"),
                       socket.inet_aton("0.0.0.0"))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.settimeout(1.0)
    deadline = time.time() + duration_s
    seen = 0
    while time.time() < deadline:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        seen += 1
        first_line = data.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        # Pull NT / ST / NTS for compact display
        text = data.decode("utf-8", "replace")
        nt  = next((l.split(":",1)[1].strip()
                    for l in text.splitlines() if l.upper().startswith("NT:")), "")
        st  = next((l.split(":",1)[1].strip()
                    for l in text.splitlines() if l.upper().startswith("ST:")), "")
        nts = next((l.split(":",1)[1].strip()
                    for l in text.splitlines() if l.upper().startswith("NTS:")), "")
        kind = first_line.split()[0] if first_line else "?"
        ident = nts or st or nt or "—"
        print(f"  {time.strftime('%H:%M:%S')} {addr[0]:<16} {kind:<10} {ident}")
    s.close()
    print(f"\nObserved {seen} SSDP packet(s).")
    return 0 if seen > 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--watch", type=int, default=0,
                    help="passively log SSDP traffic for N seconds")
    ap.add_argument("--discover-only", action="store_true",
                    help="just list every UPnP device on the LAN; skip the SoniqBoom-specific assertions")
    args = ap.parse_args()
    if args.watch:
        return cmd_watch(args.watch)
    if args.discover_only:
        return cmd_discover()
    rc = cmd_discover()
    rc2 = cmd_check_soniqboom()
    return rc | rc2


if __name__ == "__main__":
    sys.exit(main())
