# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
stations.py — internet-radio Stations API (Beta).

Listing endpoints serve the curated scene pack, the Radio Browser world
tree (continent → country → top-10 / 11-50 / remaining buckets) and the
favorites list; every listing filters out dead stations and flags
favorites.

``GET /stations/relay/{sid}`` is the playback path: the server connects
to the station upstream (so plain-http streams play on an https UI, the
oscilloscope's AnalyserNode sees same-origin audio, and ICY metadata is
readable at all), requests ``Icy-MetaData: 1``, strips the interleaved
title blocks out of the byte stream and pushes now-playing titles to all
clients over the existing library WebSocket as ``radio_meta`` events.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from soniqboom.core import radiodir

log = logging.getLogger("soniqboom.stations")

router = APIRouter(prefix="/stations", tags=["stations"])

_TITLE_RE = re.compile(rb"StreamTitle='(.*?)';")


async def _assert_public_url(url: str) -> None:
    """SSRF guard for the relay: only http(s) to a publicly-routable host.

    Station stream URLs ultimately come from a community directory whose
    entries any stranger can edit, so the relay must never be steerable at
    the SoniqBoom host's own network (cloud metadata, LAN admin panels,
    file:// etc.).  We require an http/https scheme and reject any hostname
    that resolves to a private / loopback / link-local / reserved address.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise HTTPException(400, "Only http/https station streams are allowed")
    host = parts.hostname
    if not host:
        raise HTTPException(400, "Station stream URL has no host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parts.port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise HTTPException(502, f"Station host not resolvable: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise HTTPException(403, "Station stream resolves to a non-public address")


def _decorate(stations: list[dict]) -> list[dict]:
    """Flag favorites on a listing.  Stations are never permanently hidden —
    an unavailable station is treated as a temporary outage."""
    fav = {f.get("sid") for f in radiodir.get_favorites()}
    out = []
    for s in stations:
        s = dict(s)
        s["favorite"] = s.get("sid") in fav
        out.append(s)
    return out


# ── Listings ──────────────────────────────────────────────────────────────────

@router.get("/scene")
async def scene():
    return _decorate(radiodir.SCENE_PACK)


@router.get("/favorites")
async def favorites():
    return _decorate(radiodir.get_favorites())


@router.get("/search")
async def search(q: str = Query(""), limit: int = Query(30, ge=1, le=100)):
    """Search stations by name (scene pack + favorites + Radio Browser)."""
    return _decorate(await radiodir.search_stations(q, limit))


@router.get("/world")
async def world():
    """Continents with their countries (name, ISO code, station count)."""
    try:
        countries = await radiodir.get_countries()
    except Exception as exc:                # noqa: BLE001 — RB down, no cache
        raise HTTPException(503, f"Radio directory unavailable: {exc}") from exc
    continents: dict[str, list] = {}
    for c in sorted(countries, key=lambda x: x["name"]):
        continents.setdefault(c["continent"], []).append(c)
    order = ["Africa", "Asia", "Europe", "North America", "South America",
             "Oceania", "Other"]
    return [
        {"continent": name, "countries": continents[name]}
        for name in order if name in continents
    ]


@router.get("/country/{code}")
async def country(code: str, bucket: str = Query("top10", pattern="^(top10|top50|rest)$")):
    """Stations of a country: top10 = ranks 1–10, top50 = 11–50, rest = 51+."""
    try:
        groups = await radiodir.get_country_stations(code)
    except Exception as exc:                # noqa: BLE001
        raise HTTPException(503, f"Radio directory unavailable: {exc}") from exc
    groups = _decorate(groups)
    if bucket == "top10":
        return groups[:10]
    if bucket == "top50":
        return groups[10:50]
    return groups[50:]


# ── Favorites / dead list ─────────────────────────────────────────────────────

class StationBody(BaseModel):
    sid: str
    name: str = ""
    homepage: str = ""
    favicon: str = ""
    country: str = ""
    tags: str = ""
    votes: int = 0
    streams: list[dict] = []


@router.post("/favorites")
async def add_favorite(body: StationBody):
    # Resolve the station from a TRUSTED source (scene pack, country cache,
    # or a live Radio Browser lookup) rather than trusting the client's
    # posted ``streams``.  Without this, a user could store an arbitrary URL
    # under any sid and later have the relay fetch it (SSRF).  Unknown sids
    # are rejected.
    st = await radiodir.resolve_station(body.sid)
    if not st:
        raise HTTPException(404, "Unknown station — cannot favorite")
    radiodir.add_favorite(st)
    return {"ok": True, "favorites": _decorate(radiodir.get_favorites())}


@router.delete("/favorites/{sid:path}")
async def del_favorite(sid: str):
    radiodir.remove_favorite(sid)
    return {"ok": True, "favorites": _decorate(radiodir.get_favorites())}


# ── Relay ─────────────────────────────────────────────────────────────────────

async def _broadcast_meta(payload: dict) -> None:
    # Reuse the library WebSocket every client is already connected to.
    from soniqboom.api.library import _broadcast
    try:
        await _broadcast(payload)
    except Exception:                       # noqa: BLE001 — UI nicety only
        pass


@router.get("/relay/{sid:path}")
async def relay(sid: str, v: int = Query(0, ge=0)):
    """Stream a station through the server, de-interleaving ICY metadata."""
    st = await radiodir.resolve_station(sid)
    if not st:
        raise HTTPException(404, "Unknown station")
    streams = st.get("streams") or []
    if not streams:
        raise HTTPException(404, "Station has no streams")
    stream = streams[min(v, len(streams) - 1)]
    await _assert_public_url(stream["url"])

    # Radio Browser etiquette: report the play click (dedup'd server-side
    # per IP per day) so community popularity rankings stay meaningful.
    if stream.get("uuid"):
        asyncio.get_running_loop().create_task(radiodir.report_click(stream["uuid"]))

    client = httpx.AsyncClient(
        # ``read=30`` doubles as upstream-stall detection: a live Icecast
        # mount delivers continuously, so 30 s of silence means dead.
        timeout=httpx.Timeout(10.0, read=30.0),
        headers={"User-Agent": radiodir.USER_AGENT, "Icy-MetaData": "1"},
        follow_redirects=True,
    )
    req = client.build_request("GET", stream["url"])
    try:
        upstream = await client.send(req, stream=True)
    except Exception as exc:                # noqa: BLE001 — connect failure
        await client.aclose()
        raise HTTPException(502, f"Station unreachable: {exc}") from exc
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(502, f"Station answered HTTP {upstream.status_code}")

    media_type = upstream.headers.get("content-type", "audio/mpeg").split(";")[0]
    try:
        metaint = int(upstream.headers.get("icy-metaint", "0"))
    except ValueError:
        metaint = 0
    station_name = upstream.headers.get("icy-name") or st.get("name") or ""

    async def gen():
        last_title = None

        async def emit_title(raw: bytes):
            nonlocal last_title
            m = _TITLE_RE.search(raw)
            if not m:
                return
            try:
                title = m.group(1).decode("utf-8")
            except UnicodeDecodeError:
                title = m.group(1).decode("latin-1", "replace")
            title = title.strip()
            if title and title != last_title:
                last_title = title
                await _broadcast_meta({
                    "event": "radio_meta",
                    "sid": st["sid"],
                    "station": station_name,
                    "title": title,
                })

        try:
            if metaint <= 0:
                async for chunk in upstream.aiter_bytes(8192):
                    yield chunk
            else:
                # ICY framing: ``metaint`` audio bytes, then 1 length byte
                # (×16 = metadata block size, 0 = no update), repeating.
                # Misaligning this by even one byte corrupts the audio.
                buf = b""
                audio_left = metaint
                async for chunk in upstream.aiter_bytes(8192):
                    buf += chunk
                    while True:
                        if audio_left > 0:
                            take = buf[:audio_left]
                            if not take:
                                break
                            yield take
                            buf = buf[len(take):]
                            audio_left -= len(take)
                            if audio_left > 0:
                                break              # need more upstream bytes
                        else:
                            if not buf:
                                break
                            meta_len = buf[0] * 16
                            if len(buf) < 1 + meta_len:
                                break              # metadata block split across chunks
                            if meta_len:
                                await emit_title(buf[1:1 + meta_len])
                            buf = buf[1 + meta_len:]
                            audio_left = metaint
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(gen(), media_type=media_type, headers={
        "Cache-Control": "no-store",
        "X-Station-Name": station_name.encode("ascii", "ignore").decode() or "station",
        "X-Station-Metaint": str(metaint),
    })
