# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cast / AirPlay / DLNA output discovery (E-6).

Discovers playback targets on the local network and exposes a uniform
``CastTarget`` record so the UI can present a single "Output" picker.

Three discovery backends, each optional:

* ``pychromecast`` — Google Cast (Chromecast, Nest Hub, Cast-enabled TVs)
* ``pyatv``        — AirPlay (Apple TV, HomePod, AirPlay-capable speakers)
* ``async-upnp-client`` — DLNA / UPnP media renderers

If a backend isn't installed, that protocol is silently skipped — the
picker shows whatever's available.  The streaming side just hands the
target a URL pointing at ``/api/stream/{id}`` (with a short-lived
HMAC token in the future to avoid leaking session creds to the LAN).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, asdict, field

log = logging.getLogger(__name__)


@dataclass
class CastTarget:
    id:       str       # backend-specific stable id
    name:     str
    protocol: str       # 'cast' | 'airplay' | 'dlna'
    host:     str
    port:     int | None = None
    model:    str | None = None
    # DLNA-specific: the device description URL discovered via the SSDP
    # LOCATION header.  Without this, callers had to synthesize one and
    # invariably guessed wrong (port 1900 is the SSDP multicast port,
    # NOT the renderer's HTTP port; the actual path is renderer-specific).
    description_url: str | None = None

    def to_public(self) -> dict:
        return asdict(self)


# ── Per-protocol backends — each ``discover_*`` returns [CastTarget] ───────

async def _discover_chromecast(timeout: float) -> list[CastTarget]:
    try:
        import pychromecast
    except ImportError:
        return []
    loop = asyncio.get_running_loop()
    def _scan():
        chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
        try:
            return [
                CastTarget(
                    id=str(cc.uuid),
                    name=cc.name or cc.model_name,
                    protocol="cast",
                    host=str(cc.cast_info.host),
                    port=int(cc.cast_info.port or 8009),
                    model=cc.cast_info.model_name,
                )
                for cc in chromecasts
            ]
        finally:
            try: browser.stop_discovery()
            except Exception: pass
    try:
        return await loop.run_in_executor(None, _scan)
    except Exception:
        log.exception("pychromecast scan failed")
        return []


async def _discover_airplay(timeout: float) -> list[CastTarget]:
    try:
        import pyatv
    except ImportError:
        return []
    try:
        scan = await pyatv.scan(asyncio.get_running_loop(), timeout=timeout)
    except Exception:
        log.exception("pyatv scan failed")
        return []
    out: list[CastTarget] = []
    for atv in scan:
        out.append(CastTarget(
            id=str(atv.identifier),
            name=atv.name or "AirPlay device",
            protocol="airplay",
            host=str(atv.address),
            port=getattr(atv, "port", None),
            model=getattr(atv, "model_str", None),
        ))
    return out


async def _discover_dlna(timeout: float) -> list[CastTarget]:
    try:
        from async_upnp_client.search import async_search
        from async_upnp_client.aiohttp import AiohttpRequester
    except ImportError:
        return []
    targets: list[CastTarget] = []
    # The async-upnp-client API discovers via SSDP M-SEARCH; we filter to
    # MediaRenderer services since those are the audio sinks we want.
    try:
        async def _on_response(info):
            st = info.get("ST", "")
            if "MediaRenderer" not in st:
                return
            loc = info.get("LOCATION", "")
            usn = info.get("USN", "")
            name = info.get("SERVER") or usn or loc
            from urllib.parse import urlparse
            u = urlparse(loc)
            targets.append(CastTarget(
                id=usn or loc,
                name=name.split(",")[0][:60],
                protocol="dlna",
                host=u.hostname or "",
                port=u.port,
                description_url=loc or None,
            ))
        await async_search(timeout=int(timeout), async_callback=_on_response)
    except Exception:
        log.exception("UPnP scan failed")
    return targets


# ── Public discovery API ────────────────────────────────────────────────────

_DISCOVERY_CACHE: dict[str, object] = {"value": [], "ts": 0.0}
_DISCOVERY_TTL_S = 30.0  # SSDP responses are stable for ~minutes; 30 s
                         # keeps the picker responsive without re-blasting
                         # the LAN with M-SEARCH every time the user pauses.
_discovery_lock = asyncio.Lock()


async def discover(timeout: float = 4.0, *, force_refresh: bool = False) -> list[CastTarget]:
    """Run all three discovery backends in parallel and return the
    de-duplicated list of CastTarget.

    The result is cached for ``_DISCOVERY_TTL_S`` seconds — this is the
    difference between "/api/cast/control responds in 50 ms" and "every
    control call waits the full SSDP timeout (~4 s)".  Pass
    ``force_refresh=True`` to bust the cache (used by the picker's
    explicit refresh button).

    ``timeout`` bounds each backend so a slow protocol doesn't block the
    others.
    """
    now = time.time()
    if not force_refresh:
        cached = _DISCOVERY_CACHE.get("value") or []
        cached_ts = float(_DISCOVERY_CACHE.get("ts") or 0)
        if cached and (now - cached_ts) < _DISCOVERY_TTL_S:
            return list(cached)

    # Only one concurrent discovery run — N callers piling up during a
    # cold start would otherwise each launch their own SSDP+mDNS storm.
    async with _discovery_lock:
        # Re-check after acquiring the lock — another coroutine might
        # have populated the cache while we were waiting.
        cached = _DISCOVERY_CACHE.get("value") or []
        cached_ts = float(_DISCOVERY_CACHE.get("ts") or 0)
        if not force_refresh and cached and (time.time() - cached_ts) < _DISCOVERY_TTL_S:
            return list(cached)

        results = await asyncio.gather(
            _discover_chromecast(timeout),
            _discover_airplay(timeout),
            _discover_dlna(timeout),
            return_exceptions=True,
        )
        out: list[CastTarget] = []
        seen: set[str] = set()
        for batch in results:
            if isinstance(batch, BaseException):
                log.warning("discovery backend failed: %s", batch)
                continue
            for tgt in batch:
                key = f"{tgt.protocol}:{tgt.id}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(tgt)
        _DISCOVERY_CACHE["value"] = out
        _DISCOVERY_CACHE["ts"]    = time.time()
        return out


def invalidate_discovery_cache() -> None:
    """Drop the cached discovery results — used when an admin toggles
    a backend on/off, or when the UI explicitly asks for a refresh."""
    _DISCOVERY_CACHE["value"] = []
    _DISCOVERY_CACHE["ts"]    = 0.0


def backend_status() -> dict[str, bool]:
    """Return which discovery libraries are installed.  Surfaced to the UI
    so the picker can show "Install pychromecast to see Cast devices"
    hints instead of just an empty list."""
    def _has(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except ImportError:
            return False
    return {
        "cast":    _has("pychromecast"),
        "airplay": _has("pyatv"),
        "dlna":    _has("async_upnp_client"),
    }
