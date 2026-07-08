# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
stations.py — internet-radio Stations API.

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
import time
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from soniqboom.core import radiodir
from soniqboom.core.station_hub import StationHub, registry as _hub_registry

log = logging.getLogger("soniqboom.stations")

# Direct/HLS relay reconnect bound: after the FIRST audio has flowed, a station
# blip (upstream EOF/error) reconnects under the same hub up to this many
# CONSECUTIVE times with exponential backoff, keeping every subscriber attached.
# Exhausting it ends the hub (subscribers get EOF → the frontend re-requests).
_RELAY_MAX_RECONNECTS = 5
# A reconnect cycle that has been delivering audio for at least this long counts
# as "healthy" and resets the consecutive-failure counter.  Without this, a
# station that flaps (yields a chunk, dies, repeats) would reset on every cycle
# and reconnect forever; with it, only a genuinely-recovered stream resets, so a
# persistently-flapping station is abandoned after _RELAY_MAX_RECONNECTS.
_RELAY_HEALTHY_SECS = 30.0

router = APIRouter(prefix="/stations", tags=["stations"])

# Strong refs to fire-and-forget background tasks (e.g. play-click reports).
# asyncio only weak-references tasks, so an unreferenced one can be GC'd —
# and silently cancelled — before it finishes.
_bg_tasks: set[asyncio.Task] = set()

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
        # ``not is_global`` covers private / loopback / link-local (incl. cloud
        # metadata 169.254/16) / CGNAT 100.64/10 (Alibaba metadata) / reserved
        # / IPv6 ULA & link-local in one check; keep the explicit flags too.
        if (not ip.is_global or ip.is_multicast or ip.is_unspecified):
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
    # Also relay to multi-room members (they're on the multiroom WS, not the
    # library WS) so a station playing to a room shows live now-playing + cover.
    try:
        from soniqboom.api.multiroom import notify_station_meta
        await notify_station_meta(payload)
    except Exception:                       # noqa: BLE001 — UI nicety only
        pass


async def _lookup_and_push_art(sid: str, raw_title: str) -> None:
    """Resolve a cover for the now-playing ``StreamTitle`` and push a
    ``radio_art`` event.  Carries ``title`` so a client can ignore a result
    that lands after the song already changed."""
    try:
        from soniqboom.core.nowplaying_art import parse_stream_title, lookup
        artist, song = parse_stream_title(raw_title)
        if not artist or not song:
            return
        res = await lookup(artist, song)
        if not res:
            return                          # no confident cover → keep station logo
        await _broadcast_meta({
            "event": "radio_art",
            "sid": sid,
            "title": raw_title,
            "artist": artist,
            "song": song,
            "cover_url": res.get("cover_url"),
            "album": res.get("album") or "",
            "year": res.get("year"),
            "label": res.get("label") or "",
            "source": res.get("source") or "",
        })
    except Exception:
        log.debug("radio art lookup failed for %r", raw_title, exc_info=True)


@router.get("/nowplaying-art/{slug}")
async def nowplaying_art(slug: str):
    """Serve a now-playing cover that was fetched from Discogs/MusicBrainz and
    cached locally (so the browser never hotlinks the third party)."""
    from fastapi.responses import Response
    from soniqboom.core.nowplaying_art import read_cover
    data = read_cover(slug)
    if not data:
        raise HTTPException(404, "No cached cover")
    mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "public, max-age=86400"})


# ── HLS relay (transcode) ─────────────────────────────────────────────────────
#
# Many stations (all of NHK World's AAC feeds, most modern broadcasters)
# publish HLS ``.m3u8`` playlists rather than a plain Icecast/SHOUTcast
# mount.  A browser <audio> element can't play HLS except in Safari, so the
# byte-forward relay below — which would hand the playlist manifest straight
# to <audio> — produces MediaError SRC_NOT_SUPPORTED everywhere else.
#
# For HLS we instead let ffmpeg (which reads HLS natively) pull the segments
# and re-mux them into ONE continuous elementary stream the browser can play:
#   * AAC/AAC+ source → ``-c:a copy -f adts`` — repackage only, bit-exact,
#     no re-encode, minimal CPU (the common case; NHK is mp4a.40.2 AAC).
#   * anything else   → ``-c:a libmp3lame`` MP3 — universally decodable.

_HLS_CODECS_COPY = {"AAC", "AAC+", "AACP", "HE-AAC", "HE-AACV2", "MP4A"}

# #7 — non-AAC HLS fallback encoder, capability-detected once.  Prefer an AAC
# encoder (macOS AudioToolbox ``aac_at``, else ffmpeg-native ``aac``) so we
# DON'T depend on libmp3lame — native aac is part of ffmpeg core and always
# present, whereas a stripped system ffmpeg can lack libmp3lame.  Output ADTS.
_encode_choice: tuple | None = None
_encode_lock = asyncio.Lock()


async def _hls_encode_fallback() -> tuple:
    """Return ``(ffmpeg_out_args, media_type)`` for re-encoding a non-AAC HLS
    stream, or ``(None, None)`` if no usable audio encoder exists."""
    global _encode_choice
    if _encode_choice is not None:
        return _encode_choice
    async with _encode_lock:
        if _encode_choice is not None:
            return _encode_choice
        from soniqboom.config import settings
        ff = settings.ffmpeg_path or "ffmpeg"
        encs: set[str] = set()
        try:
            proc = await asyncio.create_subprocess_exec(
                ff, "-hide_banner", "-encoders",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            for ln in out.decode("utf-8", "replace").splitlines():
                p = ln.split()
                if len(p) >= 2 and p[0][:1] == "A":     # audio-encoder row
                    encs.add(p[1])
        except Exception:                               # noqa: BLE001
            pass
        if "aac_at" in encs:
            _encode_choice = (["-c:a", "aac_at", "-b:a", "128k", "-f", "adts"], "audio/aac")
        elif "aac" in encs:
            _encode_choice = (["-c:a", "aac", "-b:a", "128k", "-f", "adts"], "audio/aac")
        elif "libmp3lame" in encs:
            _encode_choice = (["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"], "audio/mpeg")
        else:
            _encode_choice = (None, None)
        log.info("HLS non-AAC fallback encoder: %s",
                 _encode_choice[0][1] if _encode_choice[0] else "NONE AVAILABLE")
    return _encode_choice


# #6 — negative cache of HLS masters that just failed to transcode, so the
# frontend's per-candidate step-down retries don't re-spawn ffmpeg for a
# known-dead stream in a tight loop.  Keyed by URL → monotonic expiry.
_hls_fail: dict[str, float] = {}
_HLS_FAIL_TTL = 10.0    # short: a dead master stops the respawn storm without
                        # locking out a stream that recovers seconds later


def _hls_recently_failed(url: str) -> bool:
    exp = _hls_fail.get(url)
    if exp is None:
        return False
    if exp <= time.monotonic():
        _hls_fail.pop(url, None)
        return False
    return True


def _hls_mark_failed(url: str) -> None:
    now = time.monotonic()
    # Opportunistic prune so the dict can't grow unbounded.
    if len(_hls_fail) > 256:
        for k in [k for k, v in _hls_fail.items() if v <= now]:
            _hls_fail.pop(k, None)
    _hls_fail[url] = now + _HLS_FAIL_TTL

# Content-types that identify an HLS playlist (used as a runtime fallback when
# neither the Radio Browser hls flag nor a ``.m3u8`` suffix flagged it).
_HLS_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "audio/mpegurl", "audio/x-mpegurl", "application/mpegurl",
}

# Non-HLS playlist WRAPPERS — a small text file that just points at the real
# stream URL(s).  Byte-forwarding the wrapper to <audio> fails the same way
# raw HLS did, so we resolve it to the inner stream first.
_PLAYLIST_CONTENT_TYPES = _HLS_CONTENT_TYPES | {
    "audio/x-scpls", "application/pls+xml",          # .pls
    "application/xspf+xml",                          # .xspf
    "video/x-ms-asf", "application/x-mpegurl",       # .asx / .m3u
    "audio/scpls", "text/uri-list",
}
_PLAYLIST_SUFFIXES = (".pls", ".m3u", ".xspf", ".asx", ".wpl")


def _url_suffix_is_playlist(url: str) -> bool:
    try:
        return urlsplit(url).path.lower().endswith(_PLAYLIST_SUFFIXES)
    except Exception:
        return False


def _parse_playlist(body: bytes, base_url: str) -> str | None:
    """Extract the first playable stream URL from a .pls / .m3u / .xspf /
    .asx wrapper.  Returns an absolute URL, or None if none is found."""
    from urllib.parse import urljoin
    text = body.decode("utf-8", "replace")
    # .pls: ``File1=<url>`` (case-insensitive, numbered).
    for line in text.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("file") and "=" in s:
            val = s.split("=", 1)[1].strip()
            if val.startswith(("http://", "https://")):
                return val
    # .m3u / plain URI list: first non-comment line that is (or resolves to) a
    # URL.  Guard the relative-URL branch against binary noise (if we mis-read
    # an audio body) — only join short, printable, whitespace-free path lines.
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith(("http://", "https://")):
            return s
        if ("://" not in s and base_url and len(s) < 2048
                and s.isprintable() and " " not in s and "\t" not in s):
            joined = urljoin(base_url, s)
            if joined.startswith(("http://", "https://")):
                return joined
    # .asx / .xspf (XML): <ref href="..."/> or <location>...</location>.
    m = (re.search(r'href\s*=\s*["\']([^"\']+)', text, re.I)
         or re.search(r'<location>\s*([^<\s]+)', text, re.I))
    if m:
        val = m.group(1).strip()
        if val.startswith(("http://", "https://")):
            return val
    return None


def _is_hls(stream: dict) -> bool:
    """True when *stream* is an HLS playlist (Radio Browser hls flag, or a
    ``.m3u8`` path)."""
    if stream.get("hls"):
        return True
    try:
        return urlsplit(stream["url"]).path.lower().endswith(".m3u8")
    except Exception:
        return False


# ── HLS in-band now-playing metadata (ID3 timed metadata) ─────────────────────
#
# Many HLS radio streams (RTL, France Info, Triton-powered US stations, …)
# carry an ID3 timed-metadata track alongside the audio, with TIT2 (title) /
# TPE1 (artist) frames that update per song.  We run a second, best-effort
# ffmpeg that maps ONLY that data track (``-map 0:d:0``) and emit the parsed
# titles as the same ``radio_meta`` WebSocket events the Icecast path uses.
# Streams without a data track (e.g. NHK) make ffmpeg exit within a few
# seconds having downloaded nothing, so this costs them nothing.

def _synchsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _id3_text(data: bytes) -> str:
    if not data:
        return ""
    enc, raw = data[0], data[1:]
    try:
        if enc == 0:
            return raw.split(b"\x00")[0].decode("latin-1").strip()
        if enc == 3:
            return raw.split(b"\x00")[0].decode("utf-8").strip()
        if enc == 2:
            return raw.split(b"\x00\x00")[0].decode("utf-16-be").strip()
        if enc == 1:
            # UTF-16: use the BOM when present.  Otherwise the endianness is
            # ambiguous (spec violation) — guess it from which byte lane holds
            # the zero bytes: for an ASCII/Latin title, UTF-16BE puts 0x00 in
            # the EVEN positions ("S" = 00 53), UTF-16LE in the ODD ones
            # (53 00).  This avoids the classic BOM-less-BE → CJK mojibake that
            # a plain ``decode('utf-16')`` (defaults to LE) produces.
            body = raw.split(b"\x00\x00")[0]
            if len(body) % 2:
                body = body[:-1]
            if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
                return body.decode("utf-16", "replace").strip()
            evens = sum(1 for k in range(0, len(body), 2) if body[k] == 0)
            odds = sum(1 for k in range(1, len(body), 2) if body[k] == 0)
            codec = "utf-16-be" if evens >= odds else "utf-16-le"
            return body.decode(codec, "replace").strip()
        return raw.split(b"\x00")[0].decode("utf-8", "replace").strip()
    except Exception:                       # noqa: BLE001 — bad encoding byte
        return ""


def _parse_id3_tag(tag: bytes) -> dict:
    """Parse TIT2/TPE1/TALB text frames out of one full ID3v2 tag."""
    out: dict = {}
    ver = tag[3] if len(tag) > 3 else 0
    i, end = 10, len(tag)
    while i + 10 <= end:
        fid = tag[i:i + 4]
        if not (65 <= fid[0] <= 90):        # not an A–Z frame id → padding
            break
        fsize = (_synchsafe(tag[i + 4:i + 8]) if ver >= 4
                 else int.from_bytes(tag[i + 4:i + 8], "big"))
        if fsize <= 0 or i + 10 + fsize > end:
            break
        if fid in (b"TIT2", b"TPE1", b"TALB"):
            out[fid.decode()] = _id3_text(tag[i + 10:i + 10 + fsize])
        i += 10 + fsize
    return out


_ID3_MAX_TAG = 256 * 1024      # a now-playing tag is a few hundred bytes; cap


def _extract_id3(buf: bytes) -> tuple[list[dict], bytes]:
    """Pull every COMPLETE ID3v2 tag out of *buf*; return (tags, leftover)."""
    tags: list[dict] = []
    while True:
        i = buf.find(b"ID3")
        if i < 0:
            return tags, buf[-2:]           # keep a tail for a split "ID3"
        if i + 10 > len(buf):
            return tags, buf[i:]            # header split across reads
        total = 10 + _synchsafe(buf[i + 6:i + 10])
        if total > _ID3_MAX_TAG:
            # Absurd declared size → treat this "ID3" as a false sync and skip
            # past it, so a hostile/garbled stream can't balloon the buffer.
            buf = buf[i + 3:]
            continue
        if i + total > len(buf):
            return tags, buf[i:]            # body not fully arrived yet
        tags.append(_parse_id3_tag(buf[i:i + total]))
        buf = buf[i + total:]


async def _hls_metadata_pump(url: str, st: dict) -> None:
    """Best-effort: extract HLS in-band ID3 now-playing and broadcast it."""
    from soniqboom.config import settings
    from soniqboom.core import ssrf_proxy
    ff = settings.ffmpeg_path or "ffmpeg"
    _proxy = ssrf_proxy.proxy_url()
    # ``http`` is allowed ONLY when the validating proxy is up (it forwards +
    # validates cleartext http, so http HLS masters like the BBC's play safely);
    # without the proxy we stay https-only so a hostile playlist can't reach an
    # http metadata/LAN endpoint directly.
    _wl = ("http,https,tls,tcp,crypto,httpproxy" if _proxy
           else "https,tls,tcp,crypto")
    cmd = [
        ff, "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", _wl,
    ]
    if _proxy:
        cmd += ["-http_proxy", _proxy]
    cmd += [
        "-user_agent", radiodir.USER_AGENT, "-rw_timeout", "20000000",
        "-i", url, "-map", "0:d:0", "-c", "copy", "-f", "data", "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except Exception:                       # noqa: BLE001 — ffmpeg missing etc.
        return
    from soniqboom.config import settings as _settings
    buf = b""
    last = None
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            tags, buf = _extract_id3(buf)
            for t in tags:
                title = (t.get("TIT2") or "").strip()
                artist = (t.get("TPE1") or "").strip()
                if not title and not artist:
                    continue
                combined = (f"{artist} - {title}" if artist and title
                            else (title or artist))
                if combined == last:
                    continue
                last = combined
                await _broadcast_meta({
                    "event": "radio_meta", "sid": st["sid"],
                    "station": st.get("name") or "", "title": combined,
                    "artist": artist, "song": title,
                })
                if _settings.radio_art_lookup:
                    _at = asyncio.get_running_loop().create_task(
                        _lookup_and_push_art(st["sid"], combined))
                    _bg_tasks.add(_at)
                    _at.add_done_callback(_bg_tasks.discard)
    except asyncio.CancelledError:
        pass
    except Exception:                       # noqa: BLE001 — never crash playback
        pass
    finally:
        with anyio.CancelScope(shield=True):
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await proc.wait()
            except Exception:               # noqa: BLE001
                pass


async def _hls_producer(stream: dict, st: dict, set_info):
    """One ffmpeg transcode attempt of an HLS station → clean ADTS/AAC audio.

    Async generator (yields self-framing audio chunks) feeding a StationHub —
    NOT a per-listener StreamingResponse anymore; the hub fans one ffmpeg's
    output to every subscriber.  Raises on a dead master so the hub's start()
    surfaces a clean 502.  ``set_info(media_type, headers)`` is called once the
    transcode is confirmed alive, before the first yield.

    The upstream master URL has already passed ``_assert_public_url``.
    ffmpeg then fetches the variant/segment URLs the master references; the
    ``-protocol_whitelist`` keeps it to network protocols so a hostile
    playlist can't steer ffmpeg to ``file://`` / local resources.
    """
    from soniqboom.config import settings
    from soniqboom.core import ssrf_proxy
    ff = settings.ffmpeg_path or "ffmpeg"

    # #6 — a master that just failed transcoding: fail fast (no ffmpeg spawn)
    # so the frontend's candidate step-down can't storm us with respawns.
    if _hls_recently_failed(stream["url"]):
        raise HTTPException(502, "HLS stream recently failed — retry shortly")

    codec = (stream.get("codec") or "").upper()
    if codec in _HLS_CODECS_COPY:
        out_args = ["-c:a", "copy", "-f", "adts"]     # bit-exact, no re-encode
        media_type = "audio/aac"
    else:
        # #7 — non-AAC codec: re-encode with the best AVAILABLE encoder
        # (native/AudioToolbox AAC preferred → no libmp3lame dependency).
        out_args, media_type = await _hls_encode_fallback()
        if out_args is None:
            raise HTTPException(
                501, "This station's codec needs re-encoding, but ffmpeg has "
                     "no usable audio encoder installed.")

    # #1 — route ALL of ffmpeg's fetches (master, variant, segments) through
    # the local SSRF-validating proxy: it re-checks each host at connect time
    # and connects to the validated IP (defeats hostile-playlist segment URLs
    # and DNS rebinding, which -protocol_whitelist alone can't).  Falls back to
    # the https-only whitelist if the proxy isn't up.
    _proxy = ssrf_proxy.proxy_url()
    # ``http`` is allowed ONLY when the validating proxy is up (it forwards +
    # validates cleartext http, so http HLS masters like the BBC's play safely);
    # without the proxy we stay https-only so a hostile playlist can't reach an
    # http metadata/LAN endpoint directly.
    _wl = ("http,https,tls,tcp,crypto,httpproxy" if _proxy
           else "https,tls,tcp,crypto")
    cmd = [
        ff, "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", _wl,
    ]
    if _proxy:
        cmd += ["-http_proxy", _proxy]
    cmd += [
        "-user_agent", radiodir.USER_AGENT,
        "-rw_timeout", "20000000",       # 20 s I/O stall → exit (dead upstream)
        "-i", stream["url"],
        "-vn",                            # audio only (drop any video/artwork track)
        *out_args,
        "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _kill() -> None:
        with anyio.CancelScope(shield=True):
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await proc.wait()
            except Exception:            # noqa: BLE001 — best-effort reap
                pass

    # Peek the first audio bytes so a failed transcode (dead stream, missing
    # encoder, non-audio URL) surfaces as a clean 502 instead of a 200 with an
    # empty body — which <audio> would report as SRC_NOT_SUPPORTED, right back
    # to the confusing error we're fixing.  The whole peek is guarded by
    # ``except BaseException`` so a client-disconnect ``CancelledError`` (which
    # is NOT an ``Exception``) can never skip reaping ffmpeg.  15 s covers an
    # HLS first-segment fetch without amplifying the frontend's per-candidate
    # step-down retries.
    try:
        try:
            first = await asyncio.wait_for(proc.stdout.read(8192), timeout=15.0)
        except asyncio.TimeoutError as exc:
            raise HTTPException(504, "HLS stream timed out waiting for audio") from exc
        if not first:
            err = b""
            try:
                err = await asyncio.wait_for(proc.stderr.read(2048), timeout=2.0)
            except BaseException:            # noqa: BLE001 — best-effort, keep going
                pass
            reason = err.decode("utf-8", "replace").strip().splitlines()
            _hls_mark_failed(stream["url"])   # #6 — negative-cache this dead master
            raise HTTPException(
                502, f"HLS transcode failed: {reason[-1] if reason else 'no audio produced'}")
    except asyncio.CancelledError:
        # Client hung up during the peek — not a stream failure; DON'T
        # negative-cache it.  Just reap ffmpeg and propagate.
        await _kill()
        raise
    except HTTPException:
        # A 504 timeout / 502 failure — mark the master dead (the 502 branch
        # above already did; this covers the 504 timeout path) and reap.
        _hls_mark_failed(stream["url"])
        await _kill()
        raise
    except BaseException:
        await _kill()
        raise

    # Peek produced audio → the master is alive; clear any stale failure entry
    # so a station that briefly hiccuped isn't kept blocked for the TTL.
    _hls_fail.pop(stream["url"], None)

    # Success — drain stderr in the background so a chatty ffmpeg can't fill
    # the pipe buffer and stall (we don't need its output past startup).
    async def _drain_stderr() -> None:
        try:
            while await proc.stderr.read(4096):
                pass
        except Exception:                # noqa: BLE001
            pass
    _de = asyncio.get_running_loop().create_task(_drain_stderr())
    _bg_tasks.add(_de)
    _de.add_done_callback(_bg_tasks.discard)

    # Best-effort now-playing: a second ffmpeg extracts the in-band ID3 timed
    # metadata (if any) and broadcasts radio_meta events.  ONE pump per hub
    # (not per listener) — cancelled in the finally below when the hub tears the
    # producer down.
    meta_task = asyncio.get_running_loop().create_task(
        _hls_metadata_pump(stream["url"], st))
    _bg_tasks.add(meta_task)
    meta_task.add_done_callback(_bg_tasks.discard)

    set_info(media_type, {
        "Cache-Control": "no-store",
        "X-Station-Name": (st.get("name") or "station").encode("ascii", "ignore").decode() or "station",
        "X-Station-Transcode": "hls",
    })
    try:
        yield first
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            yield chunk
    finally:
        meta_task.cancel()
        await _kill()


async def _direct_producer(stream: dict, st: dict, set_info, _depth: int = 0):
    """Byte-forward a direct Icecast/SHOUTcast mount, de-interleaving ICY
    metadata into ``radio_meta`` events.

    Also resolves the indirections a browser <audio> can't follow itself:
      * HLS behind a non-``.m3u8`` URL (detected by ``#EXT-X-`` in the body)
        → hand off to the ffmpeg transcode path.
      * a ``.pls`` / ``.m3u`` / ``.xspf`` / ``.asx`` playlist WRAPPER → parse
        out the inner stream URL, re-validate it (SSRF), and recurse.
    Recursion (playlist + redirect hops combined) is bounded to ``_depth`` 5
    so a playlist/redirect loop can't spin.

    Async generator (yields clean audio) feeding a StationHub — the hub fans
    one upstream connection to every subscriber.  ``set_info(media_type,
    headers)`` is called once the real audio stream is resolved, before the
    first yield.
    """
    if _is_hls(stream):
        async for _c in _hls_producer(stream, st, set_info):
            yield _c
        return

    from soniqboom.core import ssrf_proxy
    _client_kw = dict(
        # ``read=30`` doubles as upstream-stall detection: a live Icecast
        # mount delivers continuously, so 30 s of silence means dead.
        timeout=httpx.Timeout(10.0, read=30.0),
        headers={"User-Agent": radiodir.USER_AGENT, "Icy-MetaData": "1"},
        # SSRF: do NOT auto-follow redirects.  httpx would connect to the
        # redirect TARGET without re-validation, so a hostile station could
        # 302 to http://169.254.169.254/ (cloud metadata) or a LAN host.  We
        # handle 3xx manually below and re-run _assert_public_url on the target.
        follow_redirects=False,
    )
    # Route BOTH http and https egress through the SSRF-validating proxy — it
    # re-checks the host IP at connect time (defeats DNS rebinding) for the
    # direct byte-forward path too.  http is forwarded (validated), https is
    # tunnelled via CONNECT.  Falls back to a direct client if the proxy's down.
    _proxy = ssrf_proxy.proxy_url()
    if _proxy:
        _t = httpx.AsyncHTTPTransport(proxy=_proxy)
        _client_kw["mounts"] = {"https://": _t, "http://": _t}
    client = httpx.AsyncClient(**_client_kw)
    req = client.build_request("GET", stream["url"])
    try:
        upstream = await client.send(req, stream=True)
    except Exception as exc:                # noqa: BLE001 — connect failure
        await client.aclose()
        raise HTTPException(502, f"Station unreachable: {exc}") from exc
    if upstream.status_code in (301, 302, 303, 307, 308):
        loc = upstream.headers.get("location", "")
        await upstream.aclose()
        await client.aclose()
        if not loc or _depth >= 5:
            raise HTTPException(502, "Station redirect could not be followed")
        from urllib.parse import urljoin
        target = urljoin(stream["url"], loc)
        await _assert_public_url(target)     # validate the redirect destination
        async for _c in _direct_producer({**stream, "url": target}, st, set_info, _depth + 1):
            yield _c
        return
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(502, f"Station answered HTTP {upstream.status_code}")

    media_type = upstream.headers.get("content-type", "audio/mpeg").split(";")[0]
    ct = media_type.lower().strip()

    # Playlist wrapper, or HLS that only reveals itself now?  Decide from the
    # RESPONSE content-type, not just the URL suffix (a ``.pls`` URL that 302s
    # to a direct ``.mp3`` arrives as ``audio/mpeg`` and must be byte-forwarded,
    # NOT parsed).  ``text/html`` is always sniffed — it is never a direct audio
    # stream, so this catches an HLS/playlist behind a suffixless URL served
    # with a generic type — while octet-stream/binary are only sniffed on a
    # playlist-suffixed URL (they CAN be a real stream).  Never ``audio/*``.
    _generic = ct in ("text/plain", "application/octet-stream",
                      "application/binary", "")
    looks_playlist = (
        ct in _PLAYLIST_CONTENT_TYPES
        or ct == "text/html"
        or (_generic and _url_suffix_is_playlist(stream["url"]))
    )
    if looks_playlist:
        if _depth >= 5:
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
                await client.aclose()
            raise HTTPException(502, "Station playlist nested too deeply")
        head = b""
        try:
            async for _c in upstream.aiter_bytes(4096):
                head += _c
                if len(head) >= 65536:
                    break
        except Exception:                   # noqa: BLE001
            pass
        with anyio.CancelScope(shield=True):
            await upstream.aclose()
            await client.aclose()
        if b"#EXT-X-" in head[:4096]:
            async for _c in _hls_producer(stream, st, set_info):   # ffmpeg reads stream["url"]
                yield _c
            return
        inner = _parse_playlist(head, stream["url"])
        if not inner:
            raise HTTPException(502, "Station playlist contained no stream URL")
        await _assert_public_url(inner)
        async for _c in _direct_producer({**stream, "url": inner}, st, set_info, _depth + 1):
            yield _c
        return

    try:
        metaint = int(upstream.headers.get("icy-metaint", "0"))
    except ValueError:
        metaint = 0
    station_name = upstream.headers.get("icy-name") or st.get("name") or ""

    set_info(media_type, {
        "Cache-Control": "no-store",
        "X-Station-Name": station_name.encode("ascii", "ignore").decode() or "station",
        # The served stream is CLEAN — ICY metadata is de-interleaved server-side
        # and pushed over the WS — so the client never parses it: advertise 0.
        "X-Station-Metaint": "0",
    })

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
            # Parse "Artist - Song" so the player bar can show them split
            # (falls back to the raw title when there's no clean split).
            from soniqboom.core.nowplaying_art import parse_stream_title
            _artist, _song = parse_stream_title(title)
            await _broadcast_meta({
                "event": "radio_meta",
                "sid": st["sid"],
                "station": station_name,
                "title": title,
                "artist": _artist or "",
                "song": _song or "",
            })
            # Background now-playing cover lookup (library → Discogs →
            # MusicBrainz); pushes a ``radio_art`` event when it finds one.
            # Never blocks the audio stream.  Gated by the privacy setting.
            from soniqboom.config import settings as _settings
            if _settings.radio_art_lookup:
                _at = asyncio.get_running_loop().create_task(
                    _lookup_and_push_art(st["sid"], title),
                )
                _bg_tasks.add(_at)
                _at.add_done_callback(_bg_tasks.discard)

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
        # The hub cancels this producer on teardown; the cancellation is injected
        # at the suspended ``yield``, so this finally runs inside an already-
        # cancelled scope — an unshielded ``await upstream.aclose()`` would
        # re-raise CancelledError immediately and ``client.aclose()`` would never
        # run, leaking the socket + httpx client until GC.  Shield the teardown
        # so both actually close on abort (same idiom as cast_pipe).
        with anyio.CancelScope(shield=True):
            await upstream.aclose()
            await client.aclose()


async def _producer_with_reconnect(stream: dict, st: dict, set_info):
    """Wrap _direct_producer (which self-dispatches to HLS) with server-side
    reconnect.  BEFORE any audio has flowed a failure propagates (so a dead
    station surfaces as a clean 502 to the hub's first subscriber); AFTER audio
    has flowed, an upstream EOF/error reconnects with exponential backoff up to
    _RELAY_MAX_RECONNECTS, keeping every subscriber attached across the blip."""
    yielded_any = False
    attempt = 0
    while True:
        cycle_start = time.monotonic()
        try:
            async for chunk in _direct_producer(stream, st, set_info):
                yielded_any = True
                # Reset the consecutive-failure counter only after SUSTAINED
                # delivery — a flapping station that yields one chunk then dies
                # must NOT reset every cycle (that reconnects forever, spawning a
                # fresh client/ffmpeg each time).  A rare blip after a long
                # healthy run still resets.  (Concurrency audit 2026-07-04.)
                if attempt and time.monotonic() - cycle_start >= _RELAY_HEALTHY_SECS:
                    attempt = 0
                yield chunk
            # Clean async-for exit == upstream closed the connection (EOF).
        except asyncio.CancelledError:
            raise                            # hub teardown — propagate
        except Exception as exc:             # noqa: BLE001
            if not yielded_any:
                raise                        # first-play failure → 502 to listener
            log.info("station %s relay blip: %s — reconnecting", st.get("sid"), exc)
        if not yielded_any:
            return                           # EOF before any audio, no error → stop
        attempt += 1
        if attempt > _RELAY_MAX_RECONNECTS:
            log.info("station %s: giving up after %d consecutive reconnects",
                     st.get("sid"), attempt)
            return
        await asyncio.sleep(min(2 ** attempt * 0.5, 8.0))


@router.get("/relay/{sid:path}")
async def relay(sid: str, v: int = Query(0, ge=0)):
    """Stream a station through the server, SHARED via a StationHub.

    The first listener of a given (sid, v) opens ONE upstream connection (or one
    ffmpeg for HLS); every subsequent listener — browser or cast/room target —
    attaches to the same hub, so N listeners cost one upstream pull + one
    transcode.  HLS ``.m3u8`` is transcoded via ffmpeg (browsers can't play HLS
    through <audio>); plain Icecast/SHOUTcast mounts are byte-forwarded with ICY
    metadata de-interleaved once and pushed over the WS.
    """
    st = await radiodir.resolve_station(sid)
    if not st:
        raise HTTPException(404, "Unknown station")
    streams = st.get("streams") or []
    if not streams:
        raise HTTPException(404, "Station has no streams")
    stream = streams[min(v, len(streams) - 1)]
    await _assert_public_url(stream["url"])     # fast SSRF pre-check per request

    key = f"{sid}#{v}"

    async def _build() -> StationHub:
        # Radio Browser etiquette: report the play click ONCE per hub (one
        # upstream session), not once per listener — dedup'd server-side per IP
        # per day anyway.  uuid validated inside report_click (anti-traversal).
        if stream.get("uuid"):
            _t = asyncio.get_running_loop().create_task(
                radiodir.report_click(stream["uuid"]))
            _bg_tasks.add(_t)
            _t.add_done_callback(_bg_tasks.discard)
        hub = StationHub(
            key,
            lambda set_info: _producer_with_reconnect(stream, st, set_info),
            media_type="audio/mpeg",         # provisional; producer set_info finalises
            burst=True,                       # MP3/ADTS self-frame → safe burst
            on_teardown=_hub_registry._deregister,
        )
        await hub.start()                     # connects; raises 502/504 if dead
        return hub

    hub = await _hub_registry.get_or_create(key, _build)
    return StreamingResponse(hub.stream(), media_type=hub.media_type,
                             headers=hub.headers)


@router.get("/hubs")
async def hubs():
    """Introspection: live station hubs and their subscriber counts.

    Proves the fan-out (one hub with N subscribers, not N hubs) and lets the
    multi-room layer see which stations are already warm.  Auth-gated like the
    rest of the stations API."""
    return _hub_registry.stats()


# ── Hover-intent warm ─────────────────────────────────────────────────────────
# The relay only connects on click, so the first audio is preceded by a cold
# DNS lookup (+ a Radio Browser resolve for rb: stations).  Warming pre-runs the
# CHEAP, side-effect-free half of the relay path on hover so the click starts
# from a warm resolver.  Deliberately does NOT: fire report_click (a hover is
# not a play — the earlier warm attempt polluted Radio Browser popularity), nor
# spawn a relay (uncapped ffmpeg was the other revert reason).  Deduped per sid
# with a short TTL and hard-capped so hovering down a long list can't stampede.
_WARM_TTL = 30.0            # seconds a warmed sid stays "fresh" (skip re-warm)
_WARM_MAX = 8              # max concurrent in-flight warms (list-scroll backstop)
_WARM_MAX_TRACKED = 512    # hard cap on the freshness map (bounds memory)
_warm_fresh: dict[str, float] = {}   # sid → monotonic expiry
_warm_inflight: set[str] = set()


@router.get("/warm/{sid:path}")
async def warm(sid: str, v: int = Query(0, ge=0)):
    """Pre-resolve a station + its stream host DNS so a later /relay click
    starts faster.  Always answers 204 (even for unknown/non-public sids) — a
    hover must never surface an error or leak whether a sid is valid.

    Cheap half of the relay path ONLY: no report_click, no relay spawn.  See the
    section comment above for why.
    """
    now = time.monotonic()
    exp = _warm_fresh.get(sid)
    if exp and exp > now:
        return Response(status_code=204)            # already warm within TTL
    # Cap + per-sid dedup.  The check-and-add is atomic under asyncio (no await
    # between here and the add), so no lock is needed.
    if sid in _warm_inflight or len(_warm_inflight) >= _WARM_MAX:
        return Response(status_code=204)
    _warm_inflight.add(sid)
    try:
        st = await radiodir.resolve_station(sid)     # rb: → warms the RB http pool
        streams = (st or {}).get("streams") or []
        if not streams:
            return Response(status_code=204)
        stream = streams[min(v, len(streams) - 1)]
        url = stream.get("url")
        if url:
            try:
                await _assert_public_url(url)        # getaddrinfo → OS DNS cache
            except HTTPException:
                return Response(status_code=204)     # non-public / unresolvable
            _warm_fresh[sid] = now + _WARM_TTL
        # Bound the freshness map: prune expired first, then hard-cap by dropping
        # the soonest-to-expire entries.  Without the hard cap, a client hovering
        # many distinct still-fresh sids within one TTL window could grow it to
        # the count of currently-fresh stations (a few MB) before expiry reclaims.
        if len(_warm_fresh) > _WARM_MAX_TRACKED:
            for k in [k for k, e in _warm_fresh.items() if e <= now]:
                _warm_fresh.pop(k, None)
            if len(_warm_fresh) > _WARM_MAX_TRACKED:
                for k in sorted(_warm_fresh, key=_warm_fresh.get)[
                        :len(_warm_fresh) - _WARM_MAX_TRACKED]:
                    _warm_fresh.pop(k, None)
        return Response(status_code=204)
    except Exception:                                # noqa: BLE001 — warm is best-effort
        return Response(status_code=204)
    finally:
        _warm_inflight.discard(sid)
