# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DLNA Media Server — SSDP announcer + responder.

Makes SoniqBoom discoverable on the LAN as a UPnP MediaServer:1 device,
so DLNA controllers (BubbleUPnP on Android, Hi-Fi Cast on iOS, Plex
client, VLC's "Network Streams", many DLNA-capable speakers and TVs)
see SoniqBoom in their library list and can browse + play tracks.

Two responsibilities:

  1. **SSDP responder** — joins multicast group 239.255.255.250:1900,
     answers M-SEARCH messages with HTTP/1.1 200-shaped responses
     pointing at our device-description URL.

  2. **SSDP announcer** — periodically sends NOTIFY ssdp:alive
     packets to the same multicast group so controllers that joined
     after our last response still see us.  At shutdown we send a
     paired ssdp:byebye so controllers can remove us from their
     cached device lists.

What this module does NOT do:

  • HTTP serving — those endpoints live in ``api/dlna_upnp.py`` and
    are mounted on the main FastAPI app (port 8080).
  • ContentDirectory:1 Browse logic — also in ``api/dlna_upnp.py``.

References:
  UPnP Device Architecture 1.1   (SSDP framing)
  DLNA Networked Device Guidelines 7
  RFC 4795                       (LLMNR — informational)
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
import uuid as _uuid
from typing import Any

log = logging.getLogger(__name__)


# ── SSDP constants ─────────────────────────────────────────────────────────

_SSDP_MCAST_GROUP = "239.255.255.250"
_SSDP_MCAST_PORT  = 1900
_SSDP_ALIVE_INTERVAL_S = 300  # spec says ≤ half the max-age (default 1800)
_DEFAULT_MAX_AGE = 1800

# UPnP device + service URNs.  These specific strings are what every
# DLNA controller's M-SEARCH filter looks for — change them only when
# upgrading the spec version we declare.
_NT_DEVICE_ROOT        = "upnp:rootdevice"
_NT_DEVICE_MEDIASERVER = "urn:schemas-upnp-org:device:MediaServer:1"
_NT_SERVICE_CONTENTDIR = "urn:schemas-upnp-org:service:ContentDirectory:1"
_NT_SERVICE_CONNMGR    = "urn:schemas-upnp-org:service:ConnectionManager:1"

# A renderer/controller can ask for one of these in an M-SEARCH ST line.
# We respond to any of them with our matching NT.
_RESPONDS_TO_ST = {
    "ssdp:all",
    _NT_DEVICE_ROOT,
    _NT_DEVICE_MEDIASERVER,
    _NT_SERVICE_CONTENTDIR,
    _NT_SERVICE_CONNMGR,
}


# ── Stable UDN ─────────────────────────────────────────────────────────────
# Each MediaServer must advertise a stable UUID so controllers can remember
# us across restarts.  Derive from the server-local credential key so the
# UDN is deterministic per host, distinct across hosts, and never leaves
# this machine.

def _derive_udn() -> str:
    """Return a UPnP-compatible ``uuid:...`` Unique Device Name.

    Deterministic per host — survives restarts — but never collides with
    another SoniqBoom instance on a different machine (because the
    credential-store key is machine-identity-derived).
    """
    try:
        from soniqboom.core.credentials import _derive_key
        import hashlib
        seed = _derive_key()
        digest = hashlib.sha1(seed.encode() if isinstance(seed, str) else seed).digest()
        # RFC 4122 UUID v5-ish — derive from seed
        u = _uuid.UUID(bytes=digest[:16], version=5)
        return f"uuid:{u}"
    except Exception:
        # Fallback for early-boot scenarios.  Stable within a process
        # but not across restarts in this branch; the announcer logs a
        # warning so the operator notices.
        log.warning("dlna_server: UDN falling back to per-process random — "
                    "controllers will rediscover SoniqBoom after each restart")
        return f"uuid:{_uuid.uuid4()}"


# ── Server class ──────────────────────────────────────────────────────────

class DLNAServer:
    """SSDP responder + announcer.

    Lifecycle:
        srv = DLNAServer(base_url="http://10.0.0.5:8080",
                         friendly_name="SoniqBoom (laptop)")
        await srv.start()
        ...
        await srv.stop()

    Both ``start`` and ``stop`` are idempotent.  ``start`` returns
    promptly — discovery + announcement loops run as background tasks.
    """

    def __init__(
        self,
        *,
        base_url: str,
        friendly_name: str,
        udn: str | None = None,
        max_age: int = _DEFAULT_MAX_AGE,
    ) -> None:
        self.base_url      = base_url.rstrip("/")
        self.friendly_name = friendly_name
        self.udn           = udn or _derive_udn()
        self.max_age       = int(max_age)

        # Device description + service URLs — match the HTTP routes in
        # api/dlna_upnp.py.  Keep these aligned with that file.
        self.device_desc_url   = f"{self.base_url}/dlna/device.xml"
        self.cds_desc_url      = f"{self.base_url}/dlna/cds.xml"
        self.cds_control_url   = f"{self.base_url}/dlna/cds/control"
        self.cds_event_url     = f"{self.base_url}/dlna/cds/event"
        self.connmgr_desc_url  = f"{self.base_url}/dlna/cm.xml"
        self.connmgr_ctrl_url  = f"{self.base_url}/dlna/cm/control"
        self.connmgr_event_url = f"{self.base_url}/dlna/cm/event"

        # Server identification string — DLNA controllers display this
        # alongside the friendly name in their picker lists.
        try:
            from soniqboom import __version__
        except Exception:
            __version__ = "0.0.0"
        import platform
        self.server_string = (
            f"{platform.system()}/{platform.release()} "
            f"UPnP/1.0 SoniqBoom/{__version__}"
        )

        self._listen_sock: socket.socket | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._announce_task: asyncio.Task | None = None
        # Strong-reference set for the per-M-SEARCH response tasks.
        # asyncio.create_task without a held reference is GC-eligible the
        # moment the coroutine yields; on a stressed event loop the
        # response would silently disappear before the unicast reply was
        # sent.  We add() each scheduled task and remove via done callback.
        self._pending_replies: set[asyncio.Task] = set()
        self._stopped = asyncio.Event()
        self._started = False

    # ── Public lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the SSDP socket and kick off the listen + announce loops.

        Idempotent — calling twice is a no-op.  Any binding error gets
        re-raised so the caller (e.g. the FastAPI startup hook) can
        either retry or surface it.

        Uses ``loop.create_datagram_endpoint`` instead of ``sock_recvfrom``
        because uvloop (which uvicorn uses) doesn't implement the
        ``sock_recvfrom`` shortcut — only the protocol API.
        """
        if self._started:
            return
        sock = self._bind_socket()
        self._listen_sock = sock
        self._stopped.clear()
        self._started = True
        log.info(
            "dlna_server: announcing on %s as '%s' (UDN=%s)",
            _SSDP_MCAST_GROUP, self.friendly_name, self.udn,
        )
        # Attach a DatagramProtocol so uvloop pumps incoming bytes via
        # ``datagram_received`` instead of us awaiting ``sock_recvfrom``
        # (which uvloop NotImplementedErrors).
        loop = asyncio.get_event_loop()
        proto_factory = lambda: _SSDPProtocol(self)  # noqa: E731
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                proto_factory, sock=sock,
            )
        except Exception:
            log.exception("dlna_server: create_datagram_endpoint failed")
            self._listen_sock = None
            self._started = False
            raise
        self._announce_task = asyncio.create_task(self._announce_loop())
        # Send the immediate "we just came up" alive burst — controllers
        # use this to populate their list without waiting for the next
        # M-SEARCH from us.
        await self._send_alive_burst()

    async def stop(self) -> None:
        """Cancel loops, send ssdp:byebye, close the socket.

        Idempotent.  Bounded — the byebye burst gets up to 1 s, the
        announcer cancellation 0.5 s."""
        if not self._started:
            return
        self._stopped.set()
        try:
            await asyncio.wait_for(self._send_byebye_burst(), timeout=1.0)
        except asyncio.TimeoutError:
            log.warning("dlna_server: byebye burst timed out — continuing shutdown")
        if self._announce_task and not self._announce_task.done():
            self._announce_task.cancel()
            try:
                await asyncio.wait_for(self._announce_task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        # Cancel any still-pending M-SEARCH response tasks.  Bounded by
        # the M-SEARCH burst rate × the MX delay (max 3 s) — typically a
        # handful at shutdown time.  Each was strong-referenced via
        # ``self._pending_replies`` (see _handle_message).
        for t in list(self._pending_replies):
            if not t.done():
                t.cancel()
        if self._pending_replies:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._pending_replies, return_exceptions=True),
                    timeout=0.5,
                )
            except asyncio.TimeoutError:
                pass
        self._pending_replies.clear()
        # Close the datagram transport (which closes the underlying sock).
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        self._listen_sock = None
        self._transport = None
        self._announce_task = None
        self._started = False
        log.info("dlna_server: stopped")

    # ── Socket setup ─────────────────────────────────────────────────

    def _bind_socket(self) -> socket.socket:
        """Create + configure the SSDP UDP socket.

        Binding to port 1900 requires SO_REUSEADDR (and on BSD/macOS
        SO_REUSEPORT) because multiple DLNA implementations on the same
        host want to share the port.  IP_ADD_MEMBERSHIP joins the SSDP
        multicast group so we actually receive M-SEARCH.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)  # macOS / BSD
        except (AttributeError, OSError):
            pass
        sock.bind(("0.0.0.0", _SSDP_MCAST_PORT))
        # Multicast group join — uses INADDR_ANY so the kernel decides
        # which interface(s) to join on.  TTL=4 covers home networks
        # without leaking past the first router.
        mreq = struct.pack("4s4s",
                           socket.inet_aton(_SSDP_MCAST_GROUP),
                           socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.setblocking(False)
        return sock

    # ── Message dispatch ─────────────────────────────────────────────
    # Driven by ``_SSDPProtocol.datagram_received`` (below) rather than
    # an awaiting ``sock_recvfrom`` loop — uvloop only supports the
    # protocol API.

    def _handle_message(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse an SSDP datagram and dispatch.

        We only care about M-SEARCH; NOTIFY messages from other devices
        on the LAN are ignored (we're not a discovery client).
        """
        try:
            text = data.decode("utf-8", "replace")
        except Exception:
            return
        if not text.startswith("M-SEARCH"):
            return
        # Parse headers — case-insensitive per RFC 2616.
        headers: dict[str, str] = {}
        for line in text.splitlines()[1:]:
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            headers[k.strip().upper()] = v.strip()
        st = headers.get("ST", "")
        if st not in _RESPONDS_TO_ST:
            return
        # Honour MX (max wait) per spec — randomise the response delay
        # in [0, MX) so a burst of M-SEARCHes doesn't synchronise our
        # responses and flood the LAN.  Clamp 0..3 s.
        try:
            mx = max(0, min(3, int(headers.get("MX", "1"))))
        except ValueError:
            mx = 1
        import random
        delay = random.uniform(0, mx)
        # Determine which NT(s) to advertise back — when the searcher
        # asked for "ssdp:all" we send one response per resource type.
        if st == "ssdp:all":
            sts = (_NT_DEVICE_ROOT, _NT_DEVICE_MEDIASERVER,
                   _NT_SERVICE_CONTENTDIR, _NT_SERVICE_CONNMGR)
        else:
            sts = (st,)

        async def _respond():
            await asyncio.sleep(delay)
            for one_st in sts:
                self._send_msearch_reply(one_st, addr)
        # Strong-reference until the task completes — see _pending_replies
        # docstring.  done-callback discards the reference so the set
        # doesn't grow unbounded under M-SEARCH burst.
        task = asyncio.create_task(_respond())
        self._pending_replies.add(task)
        task.add_done_callback(self._pending_replies.discard)

    def _send_msearch_reply(self, st: str, addr: tuple[str, int]) -> None:
        """Send a unicast HTTP/1.1 200 reply to an M-SEARCH source."""
        usn = self._usn_for(st)
        msg = (
            "HTTP/1.1 200 OK\r\n"
            f"CACHE-CONTROL: max-age={self.max_age}\r\n"
            f"DATE: {self._http_date()}\r\n"
            f"EXT:\r\n"
            f"LOCATION: {self.device_desc_url}\r\n"
            f"SERVER: {self.server_string}\r\n"
            f"ST: {st}\r\n"
            f"USN: {usn}\r\n"
            f"BOOTID.UPNP.ORG: {int(time.time())}\r\n"
            f"CONFIGID.UPNP.ORG: 1\r\n"
            "\r\n"
        ).encode("utf-8")
        # IMPORTANT: send through the DatagramTransport, not the bare
        # ``self._listen_sock`` — once create_datagram_endpoint() adopts
        # a socket, the transport owns the fd and uvloop's selector is
        # in charge of writability.  A bare ``sock.sendto`` while the
        # transport is also using the fd produced intermittent EAGAIN /
        # truncated responses on busy LANs (see QA-1 P0 #2).
        try:
            tr = self._transport
            if tr is not None:
                tr.sendto(msg, addr)
            elif self._listen_sock is not None:
                # Transport not yet adopted (e.g. start() raised mid-way).
                # Fall back to raw socket so the response is still attempted.
                self._listen_sock.sendto(msg, addr)
        except OSError as exc:
            log.warning("dlna_server: reply to %s failed: %s", addr, exc)

    # ── Announce loop (NOTIFY alive) ─────────────────────────────────

    async def _announce_loop(self) -> None:
        """Send NOTIFY ssdp:alive bursts every _SSDP_ALIVE_INTERVAL_S
        until cancelled.  Spec requires NOTIFY at least every
        max-age/2 — we go a little under that to absorb packet loss.
        """
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=_SSDP_ALIVE_INTERVAL_S,
                    )
                    return  # stop signalled
                except asyncio.TimeoutError:
                    await self._send_alive_burst()
        except asyncio.CancelledError:
            return

    async def _send_alive_burst(self) -> None:
        """Send one NOTIFY ssdp:alive per advertised resource."""
        for nt in (_NT_DEVICE_ROOT, _NT_DEVICE_MEDIASERVER,
                   _NT_SERVICE_CONTENTDIR, _NT_SERVICE_CONNMGR):
            self._send_notify(nt, "ssdp:alive")
            await asyncio.sleep(0.02)  # tiny gap so the burst doesn't reorder

    async def _send_byebye_burst(self) -> None:
        """Politely tell controllers we're leaving so they evict us
        from their caches.  Best-effort — controllers also expire us
        via max-age if we missed the byebye."""
        for nt in (_NT_DEVICE_ROOT, _NT_DEVICE_MEDIASERVER,
                   _NT_SERVICE_CONTENTDIR, _NT_SERVICE_CONNMGR):
            self._send_notify(nt, "ssdp:byebye")
            await asyncio.sleep(0.02)

    def _send_notify(self, nt: str, nts: str) -> None:
        usn = self._usn_for(nt)
        msg = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {_SSDP_MCAST_GROUP}:{_SSDP_MCAST_PORT}\r\n"
            f"CACHE-CONTROL: max-age={self.max_age}\r\n"
            f"LOCATION: {self.device_desc_url}\r\n"
            f"NT: {nt}\r\n"
            f"NTS: {nts}\r\n"
            f"SERVER: {self.server_string}\r\n"
            f"USN: {usn}\r\n"
            f"BOOTID.UPNP.ORG: {int(time.time())}\r\n"
            f"CONFIGID.UPNP.ORG: 1\r\n"
            "\r\n"
        ).encode("utf-8")
        # Same transport-vs-socket reasoning as _send_msearch_reply —
        # the transport owns the fd once create_datagram_endpoint adopts it.
        try:
            tr = self._transport
            if tr is not None:
                tr.sendto(msg, (_SSDP_MCAST_GROUP, _SSDP_MCAST_PORT))
            elif self._listen_sock is not None:
                self._listen_sock.sendto(msg, (_SSDP_MCAST_GROUP, _SSDP_MCAST_PORT))
        except OSError as exc:
            log.warning("dlna_server: NOTIFY %s failed: %s", nts, exc)

    # ── Helpers ──────────────────────────────────────────────────────

    def _usn_for(self, nt: str) -> str:
        """USN spec: same as NT but the device-UDN form gets prepended."""
        if nt == self.udn or nt == _NT_DEVICE_ROOT:
            return f"{self.udn}::{nt}" if nt != self.udn else self.udn
        return f"{self.udn}::{nt}"

    @staticmethod
    def _http_date() -> str:
        from email.utils import formatdate
        return formatdate(timeval=None, localtime=False, usegmt=True)


# ── Datagram protocol ──────────────────────────────────────────────────────
# uvloop doesn't implement ``sock_recvfrom``; the protocol API works on
# both stdlib asyncio and uvloop, so we use it everywhere.

class _SSDPProtocol(asyncio.DatagramProtocol):
    """Forwards every received UDP datagram to ``DLNAServer._handle_message``.

    No state of its own — the server keeps the only mutable reference.
    """

    def __init__(self, server: "DLNAServer") -> None:
        self._server = server

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            # IPv6 datagrams arrive with 4-tuple ``addr``; normalise to
            # (host, port) so the SSDP-handling code is address-family-
            # agnostic.
            host, port = addr[0], addr[1]
            self._server._handle_message(data, (host, port))
        except Exception:
            log.exception("dlna_server: datagram_received raised; continuing")

    def error_received(self, exc: Exception) -> None:
        log.warning("dlna_server: socket error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        # Normal on shutdown; rare otherwise.
        if exc:
            log.warning("dlna_server: connection_lost: %s", exc)


# ── Process-global instance (one per FastAPI app) ─────────────────────────

_INSTANCE: DLNAServer | None = None


def get_instance() -> DLNAServer | None:
    return _INSTANCE


def set_instance(srv: DLNAServer | None) -> None:
    global _INSTANCE
    _INSTANCE = srv
