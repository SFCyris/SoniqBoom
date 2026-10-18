# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline mode: no request to a host on the internet.

``SONIQBOOM_OFFLINE=1`` (or ``"offline": true`` in SoniqBoom.conf) turns it
on.  Every client that reaches a fixed internet service — the radio
directory, lyrics, artist info and photos, station logos, radio covers,
Demozoo, the song database, the Modland index and catalogue, the online
collections (HVSC, Aminet, scene.org), game-title lists, scrobbling, the
SoundFont and ffmpeg downloads, station streams — asks ``blocked(url)``
before it connects and, while offline, treats the host as unreachable
(nothing is remembered as "not found").  Hosts on this machine or the local
network (``localhost``, loopback / private / link-local addresses, ``.local``
names) are never blocked.  Behind those checks, ``install_guard`` (started
with the server when offline) refuses any name lookup that leads to an
internet address — a network share on an internet host included.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

_TRUE = {"1", "true", "yes", "on"}


class Offline(ConnectionError):
    """A request to an internet host while offline mode is on."""


def offline() -> bool:
    """Is offline mode on (the env var, read live, or the setting)?"""
    env = os.environ.get("SONIQBOOM_OFFLINE")
    if env is not None and env.strip() != "":
        return env.strip().lower() in _TRUE
    try:
        from soniqboom.config import settings
        return bool(getattr(settings, "offline", False))
    except Exception:                                   # noqa: BLE001
        return False


def _host_of(target: str) -> str:
    s = (target or "").strip()
    if "://" in s:
        try:
            return (urlsplit(s).hostname or "").lower()
        except ValueError:
            return ""
    host = s.rsplit("@", 1)[-1]
    if host.startswith("["):
        return host[1:].split("]", 1)[0].lower()
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.lower()


def is_local_host(host: str) -> bool:
    """``localhost`` / ``*.localhost`` / ``*.local``, or a loopback, private,
    link-local or unspecified address — never a name that needs DNS."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost") or h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified


def blocked(target: "str | None" = None) -> bool:
    """While offline: is ``target`` (a URL or a host) on the internet?  No
    target: any internet request (a client whose hosts are all public)."""
    if not offline():
        return False
    if target is None:
        return True
    return not is_local_host(_host_of(target))


def check(target: "str | None" = None) -> None:
    """``Offline`` when ``blocked(target)``."""
    if blocked(target):
        raise Offline(f"offline mode: not contacting {_host_of(target or '') or 'the internet'}")


def _is_local_addr(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified


_real_getaddrinfo = None
_strict = False


def _refuse(h: str):
    import socket
    return socket.gaierror(socket.EAI_NONAME, f"offline mode: not contacting {h}")


def _guarded_getaddrinfo(host, *args, **kwargs):
    """``socket.getaddrinfo`` that, while offline, refuses a name whose
    addresses are not all local (``socket.gaierror``) — so no connection to an
    internet host can follow, whatever client asked.  A local network name
    (a NAS) is looked up and allowed when it resolves to local addresses;
    under ``strict`` (the test suite) only local names and addresses are
    looked up at all."""
    if host is None or not (_strict or offline()):
        return _real_getaddrinfo(host, *args, **kwargs)
    h = host.decode("idna") if isinstance(host, (bytes, bytearray)) else str(host)
    if is_local_host(h):
        return _real_getaddrinfo(host, *args, **kwargs)
    try:
        ipaddress.ip_address(h.split("%", 1)[0])
        literal = True
    except ValueError:
        literal = False
    if literal or _strict:
        raise _refuse(h)                     # a public address / any name: never looked up
    infos = _real_getaddrinfo(host, *args, **kwargs)
    if any(not _is_local_addr(i[4][0]) for i in infos):
        raise _refuse(h)
    return infos


def install_guard(*, strict: bool = False) -> None:
    """Install the process-wide safety net behind the clients' own checks:
    while offline, a name lookup that would lead to an internet host fails
    (``_guarded_getaddrinfo``).  ``strict`` (the test suite): whether or not
    offline mode is on.  Idempotent.  (An event loop that resolves names
    itself — uvloop — is covered by the clients' checks.)"""
    global _real_getaddrinfo, _strict
    import socket
    _strict = _strict or strict
    if _real_getaddrinfo is not None:
        return
    _real_getaddrinfo = socket.getaddrinfo
    socket.getaddrinfo = _guarded_getaddrinfo
