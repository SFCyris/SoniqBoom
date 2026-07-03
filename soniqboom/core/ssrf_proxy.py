# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A local SSRF-validating forward proxy for internet-radio relay egress.

Station stream URLs ultimately come from a community directory anyone can
edit, and two egress paths can't validate themselves:

  * **ffmpeg** following the variant/segment URLs listed *inside* an HLS
    playlist — those hosts are chosen by the (hostile) playlist body.
  * **DNS rebinding** — a host that resolves public at check-time and private
    at connect-time (a getaddrinfo TOCTOU plain validation can't close).

Both ffmpeg (``-http_proxy``) and httpx (``proxy=``) route through this proxy,
which supports the two forward-proxy methods they use:

  * ``CONNECT host:port`` (https) — validate the host, then tunnel to the
    *validated IP* (so a rebind after the check can't take effect; the
    end-to-end TLS/SNI/cert check still happens client↔origin).
  * absolute-form ``GET http://host/…`` (cleartext http, e.g. BBC's HLS) —
    validate the host, forward an origin-form request, stream the response.

A host is rejected unless it is *globally routable* (``ip.is_global``), which
excludes RFC-1918, loopback, link-local (incl. cloud metadata 169.254/16),
CGNAT / shared-address 100.64/10 (Alibaba/OpenStack metadata 100.100.100.200),
IPv6 ULA/link-local, reserved and unspecified ranges in one check.

Bound to 127.0.0.1 on an ephemeral port; started at app boot, best-effort
(callers fall back to direct egress if it isn't up).  A concurrency cap and an
overall header-phase deadline bound the resource blast radius.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

log = logging.getLogger("soniqboom.ssrf_proxy")

_server: asyncio.AbstractServer | None = None
_port: int | None = None
_sem: asyncio.Semaphore | None = None

_CONNECT_TIMEOUT = 15.0
_HEADER_DEADLINE = 15.0          # whole request-line + headers must arrive in this
_MAX_CONCURRENCY = 128           # ceiling on simultaneous tunnels/forwards


def _blocked(ip_str: str) -> bool:
    """True unless *ip_str* is a globally-routable public address."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    # is_global is False for private / loopback / link-local / CGNAT (100.64/8)
    # / reserved / benchmarking / documentation / IPv6 ULA & link-local, etc.
    if not ip.is_global:
        return True
    # Belt-and-suspenders (some ranges are is_global True on old stdlib).
    return ip.is_multicast or ip.is_unspecified


def _split_hostport(authority: str, default_port: int) -> tuple[str | None, int]:
    """Parse ``host:port`` or bracketed ``[ipv6]:port`` from a CONNECT authority."""
    authority = authority.strip()
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            return None, default_port
        host = authority[1:end]
        rest = authority[end + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default_port
    host, _sep, ps = authority.partition(":")
    return (host or None), (int(ps) if ps.isdigit() else default_port)


async def _validated_addr(host: str, port: int):
    """Resolve host:port; return one (family, sockaddr) to connect to, or None
    if it doesn't resolve or ANY resolved address is non-public."""
    if not host:
        return None
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    chosen = None
    for family, _t, _p, _c, sockaddr in infos:
        if _blocked(sockaddr[0]):
            return None                      # conservative: block the whole host
        if chosen is None:
            chosen = (family, sockaddr)
    return chosen


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except Exception:                        # noqa: BLE001 — either side closing
        pass
    finally:
        try:
            dst.close()
        except Exception:                    # noqa: BLE001
            pass


async def _reply(writer: asyncio.StreamWriter, line: bytes) -> None:
    try:
        writer.write(line)
        await writer.drain()
    except Exception:                        # noqa: BLE001
        pass


async def _read_head(reader: asyncio.StreamReader) -> tuple[bytes, list[bytes]]:
    request = await reader.readline()
    headers: list[bytes] = []
    while True:
        h = await reader.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        headers.append(h)
        if len(headers) > 100:               # absurd header count → bail
            break
    return request, headers


async def _do_connect(authority: str, reader, writer) -> None:
    host, port = _split_hostport(authority, 443)
    addr = await _validated_addr(host, port)
    if addr is None:
        await _reply(writer, b"HTTP/1.1 403 Forbidden\r\n\r\n")
        log.warning("blocked relay CONNECT to non-public host %s:%s", host, port)
        writer.close()
        return
    _family, sockaddr = addr
    try:
        up_r, up_w = await asyncio.wait_for(
            asyncio.open_connection(sockaddr[0], sockaddr[1]),
            timeout=_CONNECT_TIMEOUT)
    except Exception:                        # noqa: BLE001
        await _reply(writer, b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        writer.close()
        return
    await _reply(writer, b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))


async def _do_get(parts: list[bytes], headers: list[bytes], reader, writer) -> None:
    url = parts[1].decode("latin-1")
    u = urlsplit(url)
    if u.scheme != "http" or not u.hostname:
        await _reply(writer, b"HTTP/1.1 400 Bad Request\r\n\r\n")
        writer.close()
        return
    host, port = u.hostname, (u.port or 80)
    addr = await _validated_addr(host, port)
    if addr is None:
        await _reply(writer, b"HTTP/1.1 403 Forbidden\r\n\r\n")
        log.warning("blocked relay GET to non-public host %s:%s", host, port)
        writer.close()
        return
    _family, sockaddr = addr
    try:
        up_r, up_w = await asyncio.wait_for(
            asyncio.open_connection(sockaddr[0], sockaddr[1]),
            timeout=_CONNECT_TIMEOUT)
    except Exception:                        # noqa: BLE001
        await _reply(writer, b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        writer.close()
        return
    # Rebuild an origin-form request; drop Proxy-* headers; ensure Host.
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    ver = parts[2] if len(parts) > 2 else b"HTTP/1.1"
    out = [b"GET " + path.encode("latin-1") + b" " + ver + b"\r\n"]
    have_host = False
    for h in headers:
        low = h.lower()
        if low.startswith(b"proxy-"):
            continue
        if low.startswith(b"host:"):
            have_host = True
        out.append(h)
    if not have_host:
        out.append(f"Host: {host}\r\n".encode("latin-1"))
    out.append(b"\r\n")
    try:
        up_w.write(b"".join(out))
        await up_w.drain()
    except Exception:                        # noqa: BLE001
        up_w.close()
        writer.close()
        return
    await asyncio.gather(_pipe(up_r, writer), _pipe(reader, up_w))


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    assert _sem is not None
    async with _sem:
        try:
            try:
                request, headers = await asyncio.wait_for(
                    _read_head(reader), timeout=_HEADER_DEADLINE)
            except (asyncio.TimeoutError, Exception):   # noqa: BLE001
                writer.close()
                return
            parts = request.split()
            if len(parts) < 2:
                writer.close()
                return
            method = parts[0].upper()
            if method == b"CONNECT":
                await _do_connect(parts[1].decode("latin-1"), reader, writer)
            elif method == b"GET":
                await _do_get(parts, headers, reader, writer)
            else:
                await _reply(writer, b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                writer.close()
        except Exception:                    # noqa: BLE001 — never crash the server
            try:
                writer.close()
            except Exception:                # noqa: BLE001
                pass


async def start() -> int | None:
    """Start the proxy on 127.0.0.1:<ephemeral>.  Idempotent; returns the port
    (or None on failure — callers degrade to direct egress)."""
    global _server, _port, _sem
    if _server is not None:
        return _port
    try:
        _sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        _server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        _port = _server.sockets[0].getsockname()[1]
        log.info("SSRF-validating relay proxy listening on 127.0.0.1:%d", _port)
    except Exception as exc:                 # noqa: BLE001
        log.warning("SSRF proxy failed to start (%s) — relay egress will use "
                    "direct connections", exc)
        _server, _port, _sem = None, None, None
    return _port


async def stop() -> None:
    global _server, _port, _sem
    if _server is not None:
        _server.close()
        try:
            await _server.wait_closed()
        except Exception:                    # noqa: BLE001
            pass
    _server, _port, _sem = None, None, None


def proxy_url() -> str | None:
    """``http://127.0.0.1:<port>`` when the proxy is up, else None."""
    return f"http://127.0.0.1:{_port}" if _port else None
