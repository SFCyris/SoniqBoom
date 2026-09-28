# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenSubsonic-compatible REST API (``/rest/*``).

The bare minimum needed by the major Subsonic clients — DSub,
Substreamer, play:Sub, Symfonium, Tempo, Sublime Music, Airsonic-refix,
Feishin — so SoniqBoom inherits their mobile + desktop ecosystem
without writing a native app.

Spec reference: https://www.subsonic.org/pages/api.jsp
OpenSubsonic extensions: https://opensubsonic.netlify.app/docs/

**Auth.** ``u + p`` (password plain or ``enc:hex``), ``u + s + t`` (salt +
md5 token), OpenSubsonic ``apiKey`` (per-user keys), HTTP Basic, or the web
session cookie.  Token mode needs a plaintext secret: the user's Subsonic app
password (``subsonic_password`` — generated under My Account, or seeded with
the login password for accounts that never set one).  ``p=`` accepts the login
password or the app password; both are O(1) after the first scrypt check,
which runs off the event loop.

**IDs.**  Tracks have first-class IDs; artists and albums are derived
views, so we synthesise stable IDs of the form ``ar:<sha1-of-key>``,
``al:<sha1-of-key>`` and folder albums ``fa:<dir hash>[~<owner key>]``
(core/subsonic_index.py; a letter-bucket group's owner share is
``fa:<ancestor hash>~g<owner key>``).  These are stable across restarts because
they hash the canonical lowercased name.  One tune of a multi-tune file (SID,
SNDH, NSF …) is ``<track id>~<n>`` — ``n`` the same wire index the web player
sends as ``?subsong=`` (``subsonic_index.wire_tune``: wire 0 is the file's
default tune, so the bare track id plays it; ``~0`` is accepted and means the
bare id).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import hashlib
import hmac
import logging
import math
import operator
import os
import re
import time
from collections import Counter
from itertools import islice
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute

from soniqboom import __version__
from soniqboom.core import folder_album as _fa
from soniqboom.core import subsonic_index as _sx
from soniqboom.core import store as _store_mod
from soniqboom.core.store import _SPLIT_RE, get_store
from soniqboom.core.subsonic_state import get_state as _get_state
from soniqboom.core.users import get_user_store
from soniqboom.models.user import User

log = logging.getLogger(__name__)


# ── OpenSubsonic formPost: every route also accepts POST form bodies ─────────
# Clients that see the ``formPost`` extension send the SAME parameters as an
# ``application/x-www-form-urlencoded`` POST body instead of the query string
# (long ``savePlayQueue`` id lists, passwords kept out of access logs).  Every
# handler reads its params with ``Query(...)``, so rather than duplicate each
# signature the handler runs on a COPY of the request whose ``query_string``
# has the form body merged in — one place, every endpoint.  The original ASGI
# scope is never modified: the server's access log formats the request line
# from it, and must not record the POSTed credentials.
_FORM_POST_MAX_BYTES = 2 * 1024 * 1024


class _SubsonicRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            req = request
            if request.method == "POST":
                ctype = (request.headers.get("content-type") or "").lower()
                if ctype.startswith("application/x-www-form-urlencoded"):
                    body = bytearray()
                    async for chunk in request.stream():
                        body += chunk
                        if len(body) > _FORM_POST_MAX_BYTES:
                            return _err(10, "Request body too large.",
                                        fmt=request.query_params.get("f", "xml"))
                    if body:
                        qs = request.scope.get("query_string") or b""
                        scope = dict(request.scope)      # shallow: state/route shared
                        scope["query_string"] = (qs + b"&" + bytes(body)) if qs else bytes(body)
                        req = Request(scope, request.receive)
                    req._body = bytes(body)          # later .body() reads the cache
            try:
                resp = await original(req)
            except RequestValidationError as exc:
                # Same envelope main.py's app-level handler produces, but with
                # the merged (form-body) ``f`` so a POSTing JSON client gets
                # JSON, and so a bare router (tests) behaves identically.
                try:
                    loc = (exc.errors()[0].get("loc") or ())
                    msg = f"Required parameter '{loc[-1] if loc else '?'}' is missing or invalid."
                except Exception:        # noqa: BLE001
                    msg = "A required parameter is missing or invalid."
                resp = _err(10, msg, fmt=req.query_params.get("f", "xml"))
            return await _gzip_offloop(req, _maybe_jsonp(req, resp))

        return handler


# Listing bodies at least this big are gzipped here, in a worker thread,
# instead of by the GZip middleware on the event loop (level 6: a 500-song
# JSON page was ~6 ms of loop time).  Below it the thread hop isn't worth it
# and the middleware compresses inline as before.
_GZIP_OFFLOAD_MIN_BYTES = 64 * 1024
_GZIP_OFFLOAD_TYPES = ("application/json", "text/xml", "application/xml",
                       "application/javascript")


async def _gzip_offloop(request: Request, resp: Response) -> Response:
    """Gzip a big in-memory listing body off the event loop when the client
    accepts gzip — the same level (6) and headers the middleware would
    produce, which passes a body that already carries ``Content-Encoding``
    through untouched.  Media (streamed / file) responses and pre-encoded
    bodies (the memoised artist index) are returned as they are."""
    if "gzip" not in (request.headers.get("accept-encoding") or ""):
        return resp
    body = getattr(resp, "body", None)
    if not isinstance(body, (bytes, bytearray)) or len(body) < _GZIP_OFFLOAD_MIN_BYTES:
        return resp
    if resp.headers.get("content-encoding"):
        return resp
    if not (resp.headers.get("content-type") or "").startswith(_GZIP_OFFLOAD_TYPES):
        return resp
    import gzip as _gzip
    gz = await asyncio.to_thread(_gzip.compress, bytes(body), 6)
    headers = {k: v for k, v in resp.headers.items()
               if k.lower() not in ("content-length", "content-encoding", "vary")}
    vary = resp.headers.get("vary")
    headers["Vary"] = f"{vary}, Accept-Encoding" if vary and "accept-encoding" not in vary.lower() \
        else (vary or "Accept-Encoding")
    headers["Content-Encoding"] = "gzip"
    return Response(content=gz, status_code=resp.status_code, headers=headers,
                    background=resp.background)


router = APIRouter(prefix="/rest", tags=["subsonic"], route_class=_SubsonicRoute)


def _route(path: str):
    """Register ``path`` for GET and POST (OpenSubsonic ``formPost``)."""
    return router.api_route(path, methods=["GET", "POST"])


def _media_route(path: str):
    """Like ``_route`` plus HEAD — for the media endpoints players and cast /
    UPnP renderers probe with HEAD before playing (stream, download, cover
    art, avatar, radio).  Each handler answers HEAD without rendering,
    transcoding or opening an upstream connection."""
    return router.api_route(path, methods=["GET", "HEAD", "POST"])


# Subsonic API version we claim to implement.  1.16.1 is the latest
# stable version most clients require for getArtists / getAlbumList2.
_SUBSONIC_API_VERSION = "1.16.1"
_SERVER_NAME = "SoniqBoom"
_FOLDER_ID = 0                 # we expose one virtual music folder (spec type: int)
_FOLDER_NAME = "Library"


# ── Envelope helpers ─────────────────────────────────────────────────────────

# The OpenSubsonic extensions we genuinely implement, served by the dedicated
# getOpenSubsonicExtensions endpoint.  ONLY list ones we implement — clients
# gate features on this list, so a phantom entry makes them call routes that
# fail.
#   songLyrics → getLyrics + getLyricsBySongId (structured/synced)
#   formPost   → every route is registered for GET *and* POST, and
#                _SubsonicRoute merges an x-www-form-urlencoded body into the
#                query params before the handler parses them
#   indexBasedQueue → getPlayQueueByIndex + savePlayQueueByIndex
#   apiKeyAuthentication → ``apiKey=`` (keys managed under Settings → My
#                Account, /api/me/api-keys) + tokenInfo
#   topSongsByArtistId → getTopSongs accepts ``id`` (an artist id)
#   transcoding → getTranscodeDecision (POST ClientInfo JSON) +
#                getTranscodeStream(mediaId, mediaType, transcodeParams, offset)
#   transcodeOffset → stream's ``timeOffset`` (seconds) starts a transcoded
#                stream (``format`` / ``maxBitRate`` / a rendered format) that
#                far in; a file served as is ignores it (seek with Range)
#   playbackReport → reportPlayback (state / position / rate per player) and
#                getNowPlaying's state / positionMs / playbackRate
#   sonicSimilarity → getSonicSimilarTracks + findSonicPath (the same
#                audio-envelope engine as the core getSimilarSongs / 2)
_OPENSUBSONIC_EXTENSIONS = [
    {"name": "songLyrics", "versions": [1]},
    {"name": "formPost", "versions": [1]},
    {"name": "indexBasedQueue", "versions": [1]},
    {"name": "apiKeyAuthentication", "versions": [1]},
    {"name": "topSongsByArtistId", "versions": [1]},
    {"name": "transcoding", "versions": [1]},
    {"name": "transcodeOffset", "versions": [1]},
    {"name": "playbackReport", "versions": [1]},
    {"name": "sonicSimilarity", "versions": [1]},
]


def _envelope(payload: dict[str, Any] | None = None, *, status: str = "ok") -> dict:
    """Build the canonical ``subsonic-response`` envelope.  ``payload`` is
    merged into the response root (e.g. ``{"musicFolders": {...}}``)."""
    body: dict[str, Any] = {
        "status": status,
        "version": _SUBSONIC_API_VERSION,
        "type": _SERVER_NAME,
        "serverVersion": __version__,
        "openSubsonic": True,
    }
    if payload:
        body.update(payload)
    return {"subsonic-response": body}


# ── XML serialization ───────────────────────────────────────────────────────
#
# The Subsonic spec defaults to XML when ``f`` is omitted from the
# request — that's what Amperfy, DSub, and most legacy clients rely on.
# We previously always emitted JSON, which clients couldn't parse, so
# they reported the server as broken even though our auth was correct.
# The serializer follows the spec's convention:
#   - dict     → child element with attributes / nested children
#   - list     → repeated child elements under the parent's key
#   - scalar   → attribute on the enclosing element
# Top-level emits an ``xmlns="http://subsonic.org/restapi"`` per spec.
import xml.etree.ElementTree as _ET


def _xml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _xml_walk(parent: _ET.Element, tag: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        elem = _ET.SubElement(parent, tag)
        # Two passes: attributes first (scalar leaves), then children.
        # The Subsonic XML schema treats lists/dicts as children and
        # everything else as attributes on the enclosing element.  The
        # reserved "_text" key becomes the element's text content (needed by
        # <lyrics>…text…</lyrics> and any element whose body is character data,
        # not an attribute).
        for k, v in value.items():
            if k == "_text":
                continue
            if isinstance(v, (dict, list)):
                continue
            if v is None:
                continue
            elem.set(k, _xml_scalar(v))
        if value.get("_text") is not None:
            elem.text = _xml_scalar(value["_text"])
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                _xml_walk(elem, k, v)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                _xml_walk(parent, tag, item)
            else:
                # A repeated *scalar* becomes a repeated child element with text
                # content — Subsonic/OpenSubsonic XML never uses multi-valued
                # attributes.  e.g. openSubsonicExtensions' ``versions:[1]`` →
                # ``<versions>1</versions>`` (spec-correct, and discoverable by
                # strict XML clients).  The previous ``parent.set(tag, …)`` path
                # flattened the list to a single attribute — silently keeping
                # only the LAST value of a multi-element list.
                child = _ET.SubElement(parent, tag)
                child.text = _xml_scalar(item)
    else:
        parent.set(tag, _xml_scalar(value))


# Characters XML 1.0 forbids outright (escaping doesn't help): C0 controls
# other than tab / LF / CR, and U+FFFE / U+FFFF.  ElementTree writes them
# verbatim, so ONE stray control byte in a tag, a client name or a station
# name made the whole document unparseable for every XML client.  Stripped
# from the UTF-8 output in one C-speed pass (a UTF-8 multi-byte sequence
# never contains bytes < 0x80, so the byte-level removal is safe).
_XML_ILLEGAL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]|\xef\xbf[\xbe\xbf]")
# The C0 controls go with one ``bytes.translate`` (~4x faster than the regex
# over a multi-MB body); the regex only runs when a U+FFFE / U+FFFF remains.
_XML_C0_DELETE = bytes(range(0x00, 0x09)) + b"\x0b\x0c" + bytes(range(0x0e, 0x20))


def _xml_strip_illegal(xml: bytes) -> bytes:
    xml = xml.translate(None, _XML_C0_DELETE)
    if b"\xef\xbf\xbe" in xml or b"\xef\xbf\xbf" in xml:
        xml = _XML_ILLEGAL.sub(b"", xml)
    return xml


def _envelope_to_xml(envelope: dict) -> bytes:
    body = envelope.get("subsonic-response", {})
    root = _ET.Element("subsonic-response",
                       attrib={"xmlns": "http://subsonic.org/restapi"})
    for k, v in body.items():
        if isinstance(v, (dict, list)):
            _xml_walk(root, k, v)
        elif v is not None:
            root.set(k, _xml_scalar(v))
    try:
        xml = _ET.tostring(root, encoding="utf-8")
    except UnicodeEncodeError:
        # A lone surrogate (undecodable tag bytes) can't be UTF-8 encoded at
        # all — degrade those characters instead of failing the response.
        xml = _ET.tostring(root, encoding="unicode").encode("utf-8", "replace")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + _xml_strip_illegal(xml)


def _json_normalize(obj: Any) -> Any:
    """Rename the reserved ``_text`` key to ``value`` for JSON output.

    The XML serializer turns ``_text`` into element character data
    (``<lyrics>…</lyrics>``); OpenSubsonic's JSON encoding carries that same
    content in a ``value`` field.  One in-memory transform keeps both encodings
    spec-correct from a single payload shape."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out["value" if k == "_text" else k] = _json_normalize(v)
        return out
    if isinstance(obj, list):
        return [_json_normalize(v) for v in obj]
    return obj


# Payload roots that carry the reserved ``_text`` key.  Only these need the
# recursive ``_json_normalize`` copy; everything else (a 5,000-song directory,
# a 50k-artist index) is serialised as-is instead of being deep-copied first.
_TEXT_ROOTS = ("lyrics", "lyricsList", "genres")


_XML_MEDIA = "text/xml"


def _ok(payload: dict[str, Any] | None = None, *, fmt: str = "xml") -> Response:
    data = _envelope(payload)
    f = (fmt or "xml").lower()
    if f in ("json", "jsonp"):
        # ``f=jsonp`` bodies are plain JSON here; the route wrapper
        # (``_maybe_jsonp``) adds the ``callback(...)`` around them.
        if payload and any(k in payload for k in _TEXT_ROOTS):
            data = _json_normalize(data)
        return JSONResponse(data)
    # Spec default — XML, as ``text/xml`` (Starlette appends
    # ``; charset=utf-8``): the spec says an error from a binary endpoint
    # (stream, download, getCoverArt, getAvatar) has a content type starting
    # with "text/xml", and clients such as DSub tell an error document from
    # the song bytes by exactly that — ``application/xml`` got saved as audio.
    return Response(content=_envelope_to_xml(data), media_type=_XML_MEDIA)


# XML listings at least this long are serialised in a worker thread: a
# 9.7k-song folder was ~280 ms of ElementTree on the event loop, stalling every
# other request.  ElementTree serialises in Python, so the loop gets the GIL
# back every switch interval.  Below the threshold the hop (~0.1 ms) isn't
# worth it.
_OFFLOAD_MIN_ITEMS = 500
# JSON is one C call that holds the GIL throughout — in a thread it would stall
# the loop just the same — so a big JSON listing is encoded item by item
# (``_json_spliced``) in a worker thread instead: a 17k-song album was ~96 ms
# of ``json.dumps`` on the loop.  Smaller bodies (a few ms) stay inline.
_JSON_OFFLOAD_MIN_ITEMS = 2000


async def _ok_async(payload: dict[str, Any] | None = None, *, fmt: str = "xml",
                    n_hint: int = 0, splice: tuple[str, str] | None = None) -> Response:
    """``_ok`` for a potentially large listing: an XML body with ``n_hint``
    (its entry count) ≥ ``_OFFLOAD_MIN_ITEMS`` is serialised off the event
    loop; so is a JSON body with ≥ ``_JSON_OFFLOAD_MIN_ITEMS`` entries when
    ``splice`` (``(root key, list key)`` — where the entries sit) is given.
    The payload must be a fresh plain-dict tree built on the loop and
    referenced nowhere else (song / album dicts — never live store objects),
    so the worker only reads memory nothing mutates.  Same bytes as ``_ok``."""
    if (fmt or "xml").lower() in ("json", "jsonp"):
        if (splice is not None and n_hint >= _JSON_OFFLOAD_MIN_ITEMS and payload
                and isinstance((payload.get(splice[0]) or {}).get(splice[1]), list)):
            body = await asyncio.to_thread(_json_spliced, payload, *splice)
            return Response(content=body, media_type="application/json")
        return _ok(payload, fmt=fmt)
    if n_hint < _OFFLOAD_MIN_ITEMS:
        return _ok(payload, fmt=fmt)
    return await asyncio.to_thread(_ok, payload, fmt=fmt)


def _err(code: int, message: str, *, fmt: str = "xml") -> Response:
    body = _envelope({"error": {"code": code, "message": message}}, status="failed")
    # Subsonic clients expect HTTP 200 even for protocol errors — the
    # error code lives in the envelope.
    f = (fmt or "xml").lower()
    if f in ("json", "jsonp"):
        return JSONResponse(body, status_code=200)
    return Response(content=_envelope_to_xml(body), media_type=_XML_MEDIA,
                    status_code=200)


# ``f=jsonp&callback=<name>``: the JSON body wrapped as ``name(<json>);``.
# The name is validated (a JS identifier path, bounded) so it can't inject
# script; a jsonp request WITHOUT a callback gets plain JSON (never a
# regression for a client that sends f=jsonp alone).
_JSONP_CALLBACK_RE = re.compile(r"[A-Za-z_$][\w$.]{0,63}", re.ASCII)


def _maybe_jsonp(request: Request, resp: Response) -> Response:
    """Apply the JSONP wrapper to a finished response when ``f=jsonp`` —
    one choke point (the route class) instead of every ``_ok`` / ``_err``."""
    qp = request.query_params
    if (qp.get("f") or "").lower() != "jsonp":
        return resp
    body = getattr(resp, "body", None)
    ctype = (resp.headers.get("content-type") or "")
    if (not isinstance(body, (bytes, bytearray)) or not ctype.startswith("application/json")
            or resp.headers.get("content-encoding")):
        return resp                         # media / streamed / pre-compressed
    cb = qp.get("callback")
    if not cb:
        return resp
    if not _JSONP_CALLBACK_RE.fullmatch(cb):
        return _err(10, "Required parameter 'callback' is missing or invalid.", fmt="json")
    headers = {k: v for k, v in resp.headers.items()
               if k.lower() not in ("content-length", "content-type")}
    return Response(content=cb.encode("ascii") + b"(" + bytes(body) + b");",
                    status_code=resp.status_code, headers=headers,
                    media_type="application/javascript")


# ── ID helpers ───────────────────────────────────────────────────────────────

# The id scheme lives in core/subsonic_index.py (the derived views build ids
# too).  Names are stripped + lower-cased before hashing, so a padded tag
# ("Foo ") gets the same id as the aggregate name ("Foo") it resolves through.
_artist_id = _sx.artist_id
_album_id = _sx.album_id


# ── Derived library views (artist union + album catalogue) ──────────────────
# Decoding ``ar:<hash>`` / ``al:<hash>`` back to a name can't reverse the hash,
# and grouping a flat track table into artists / albums is an O(N) pass — so
# core/subsonic_index.py derives ONE snapshot (artist union + album catalogue,
# built together so they never drift apart) and it is memoised here per store
# + catalogue sequence + the folder-albums flag.
#
# The sequence is the store's ``_catalog_seq`` when it has one, else
# ``_mutation_seq``.  ``_catalog_seq`` moves only for writes that change a
# field the catalogue reads — not for cover-art or play-count writes, whose
# values the snapshot's live sample dicts show anyway.  It also does not move
# for the render / probe duration backfill, which bumps only
# ``store._duration_seq``: the precomputed album totals (``AlbumEntry.duration``
# — getAlbumList2, getArtist) can lag until the next catalogue change, while
# song durations come from the live track dicts and getAlbum recomputes its
# total from them (``_album_and_tracks``).  Keying on ``_duration_seq`` too
# would re-cool the catalogue after every first play.
#
# Invalidation is *debounced*: during a watcher-triggered rescan the seq bumps
# every WRITE_CHUNK=25 tracks, and naive invalidation would rebuild hundreds
# of times during a scan.  Instead we record the last-seen seq and rebuild at
# most every ``_CACHE_DEBOUNCE_SEC``.  A flag flip rebuilds immediately (it
# changes WHAT the catalogue contains, not just freshness), so toggling
# ``subsonic_folder_albums`` takes effect on the next request.  The cache also
# records WHICH store it was built from: two stores (a test fixture, a rebuilt
# store) can share a seq value.
#
# Stale-while-revalidate: once a library's build costs more than
# ``_SWR_MIN_BUILD_SEC`` (a large library — ~0.2-0.4 s at 263k tracks), a
# stale snapshot is served while ONE background task rebuilds it in chunks
# that yield to the event loop (``build_catalogue_async``).  Small libraries
# rebuild inline — it is cheaper than the bookkeeping, and immediately fresh.
# The very first build (and a folder-flag flip), which has no snapshot to
# serve, is also chunked when it happens inside a request handler: the
# request waits for it (``_StaleMiss`` → ``_wrap``), the event loop doesn't.

_CACHE_DEBOUNCE_SEC = 5.0
_SWR_MIN_BUILD_SEC = 0.05

_ALBUM_LIST_CACHE: dict = {"store": None, "seq": None, "built_at": 0.0,
                           "folder_on": None, "cat": None, "gen": 0,
                           "build_sec": 0.0, "task": None}


def _folder_albums_on(store) -> bool:
    # Default ON (``folder_album.SUBSONIC_FOLDER_ALBUMS_DEFAULT``, the same
    # constant admin Settings shows): album-less retro tracks become one
    # album per folder, so tag-based (ID3) clients see every artist's music.
    # An explicit False (admin Settings) still wins.
    getter = getattr(store, "get_config", None)
    if getter is None:
        return _fa.SUBSONIC_FOLDER_ALBUMS_DEFAULT
    return _sx.truthy(getter(_fa.SUBSONIC_FOLDER_ALBUMS_KEY, _fa.SUBSONIC_FOLDER_ALBUMS_DEFAULT))


def _catalog_seq(store) -> int:
    seq = getattr(store, "_catalog_seq", None)
    return store._mutation_seq if seq is None else seq


# A lookup that MISSES in a debounced (stale) snapshot may be an id a fresh
# store read just handed out (a new song's albumId / artistId).  Such a miss
# forces one rebuild — paced by what a build costs (``_miss_rebuild_interval``)
# so a client probing many unknown ids during a scan can't turn every request
# into a full pass.
_MISS_REBUILD_MIN_SEC = 1.0


def _miss_rebuild_interval() -> float:
    """Minimum snapshot age before an id miss may trigger a rebuild: at least
    ``_MISS_REBUILD_MIN_SEC`` and 4x the last build's cost (so miss-driven
    rebuilds use at most ~20 % of the loop), never beyond the debounce."""
    b = float(_ALBUM_LIST_CACHE.get("build_sec") or 0.0)
    return min(max(_MISS_REBUILD_MIN_SEC, 4.0 * b),
               max(_CACHE_DEBOUNCE_SEC, _MISS_REBUILD_MIN_SEC))


def _install_catalogue(store, seq, folder_on: bool, cat: _sx.Catalogue) -> _sx.Catalogue:
    c = _ALBUM_LIST_CACHE
    c.update(store=store, seq=seq, folder_on=folder_on, built_at=time.monotonic(),
             cat=cat, gen=int(c.get("gen") or 0) + 1, build_sec=cat.build_sec)
    return cat


def _live_task(c: dict) -> asyncio.Task | None:
    """The in-flight background rebuild, if it belongs to the running loop."""
    task = c.get("task")
    if task is None or task.done():
        return None
    try:
        if task.get_loop() is not asyncio.get_running_loop():
            return None                       # left over from a closed loop (tests)
    except RuntimeError:
        return None
    return task


def _schedule_rebuild(store, folder_on: bool) -> asyncio.Task | None:
    """Start (or join) the single background rebuild.  None when no event
    loop is running (the caller then builds inline)."""
    c = _ALBUM_LIST_CACHE
    task = _live_task(c)
    if task is not None:
        return task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    gen = c.get("gen")

    async def _run() -> None:
        seq = _catalog_seq(store)             # read BEFORE the snapshot
        cat = await _sx.build_catalogue_async(store, folder_on=folder_on)
        # Install only if nothing landed meanwhile (any install — an inline
        # build on a flag flip / store swap — bumps ``gen``).
        if c.get("gen") == gen:
            _install_catalogue(store, seq, folder_on, cat)

    def _done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.warning("Subsonic catalogue rebuild failed", exc_info=t.exception())

    task = loop.create_task(_run())
    task.add_done_callback(_done)
    c["task"] = task
    return task


_STARTUP_BG: set = set()


def prewarm_catalogue(store) -> asyncio.Task | None:
    """The startup prewarm (main.py, a few seconds after startup): start —
    or join — the single background catalogue build a request would start,
    then, once that snapshot is installed, build the whole library's artist
    index off the loop (getArtists / getIndexes — what a client syncs first),
    so the first request after a restart is served warm.  Nothing for an
    empty library or when a snapshot already exists.  Returns the build
    task.  On the event loop (store access)."""
    if store.track_count() == 0 or _ALBUM_LIST_CACHE.get("cat") is not None:
        return None
    task = _schedule_rebuild(store, _folder_albums_on(store))
    if task is None:
        return None

    def _index_done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.debug("Subsonic artist-index prewarm failed", exc_info=t.exception())

    def _then(t: asyncio.Task) -> None:
        if t.cancelled() or t.exception() is not None:
            return                                # _schedule_rebuild logs failures
        c = _ALBUM_LIST_CACHE
        cat = c.get("cat")
        if cat is None or c.get("store") is not store:
            return
        w = asyncio.get_running_loop().create_task(
            _artist_index_async(store, cat, cat.union, None))
        _STARTUP_BG.add(w)
        w.add_done_callback(_STARTUP_BG.discard)
        w.add_done_callback(_index_done)
    task.add_done_callback(_then)
    return task


def _catalogue(store, *, refresh_stale: bool = False) -> _sx.Catalogue:
    c = _ALBUM_LIST_CACHE
    seq = _catalog_seq(store)
    folder_on = _folder_albums_on(store)
    cat = c.get("cat")
    if cat is None or c.get("store") is not store or c.get("folder_on") != folder_on:
        if _STALE_RETRY_OK.get():
            raise _StaleMiss()                    # built off-loop by _wrap, then retried
        return _install_catalogue(store, seq, folder_on,
                                  _sx.build_catalogue(store, folder_on=folder_on))
    if c.get("seq") == seq:
        return cat
    age = time.monotonic() - (c.get("built_at") or 0.0)   # can't step backwards
    if refresh_stale:
        if age < _miss_rebuild_interval():
            return cat
    elif age < _CACHE_DEBOUNCE_SEC:
        return cat
    elif (c.get("build_sec") or 0.0) >= _SWR_MIN_BUILD_SEC \
            and _schedule_rebuild(store, folder_on) is not None:
        return cat                                          # stale while revalidating
    return _install_catalogue(store, seq, folder_on,
                              _sx.build_catalogue(store, folder_on=folder_on))


class _StaleMiss(Exception):
    """An id missed a stale snapshot of a large library inside a handler
    that ``_wrap`` can re-run: the wrapper awaits a (chunked, background)
    rebuild and retries once, instead of blocking the loop on an inline one."""


# True while the current handler run may raise ``_StaleMiss`` (``_wrap``'s
# first attempt); helpers used anywhere else fall back to an inline rebuild.
_STALE_RETRY_OK: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "subsonic_stale_retry_ok", default=False)


def _lookup(store, fn):
    """``fn(catalogue)``; on a miss (``None``) against a stale snapshot, retry
    once on a rebuilt one (paced, see ``_miss_rebuild_interval``)."""
    cat = _catalogue(store)
    hit = fn(cat)
    if hit is not None:
        return hit
    c = _ALBUM_LIST_CACHE
    if c.get("seq") == _catalog_seq(store):
        return None                                   # fresh snapshot: a real miss
    if ((c.get("build_sec") or 0.0) >= _SWR_MIN_BUILD_SEC and _STALE_RETRY_OK.get()
            and (_live_task(c) is not None
                 or time.monotonic() - (c.get("built_at") or 0.0) >= _miss_rebuild_interval())):
        raise _StaleMiss()
    fresh = _catalogue(store, refresh_stale=True)
    return fn(fresh) if fresh is not cat else None


async def _refresh_for_miss(store) -> None:
    """Bring the catalogue up to date for a ``_StaleMiss`` retry: join the
    in-flight background rebuild, or start one (chunked — the loop stays
    responsive while this request waits)."""
    c = _ALBUM_LIST_CACHE
    folder_on = _folder_albums_on(store)
    if (c.get("cat") is not None and c.get("store") is store
            and c.get("folder_on") == folder_on and c.get("seq") == _catalog_seq(store)):
        return                                       # already fresh
    task = _schedule_rebuild(store, folder_on)
    if task is None:
        _catalogue(store, refresh_stale=True)       # no loop: inline (not reached
        return                                      # from a request handler)
    try:
        await asyncio.shield(task)
    except Exception:            # noqa: BLE001 — logged by the task; the retry
        pass                     # then answers from whatever snapshot exists


def _artist_union(store) -> _sx.ArtistUnion:
    return _catalogue(store).union


def _decode_artist(raw: str, store) -> tuple[str, str] | None:
    """``ar:<hash>`` → ``(lower-cased key, display name)``, or ``None``.
    O(1): the union covers every album artist, track artist and the reserved
    ``[Unknown Artist]`` (key ``""``)."""
    if not raw.startswith("ar:"):
        return None

    def _find(cat):
        low = cat.union.by_id.get(raw)
        return (low, cat.union.display[low]) if low is not None else None
    return _lookup(store, _find)


def _decode_album_id(raw: str, store) -> tuple[str | None, str | None]:
    """Reverse ``al:<hash>`` → (owner, album), O(1) via the catalogue.  The
    owner is "" for an album whose tracks carry no artist tag at all (filed
    under ``[Unknown Artist]``) — callers test the ALBUM half for not-found.
    (Every album id this server ever handed out — getAlbumList, getArtist,
    search, song ``albumId`` — is an ``(owner, album)`` id, so the catalogue
    covers them all; the old O(artists × albums) reverse map is gone.)"""
    if not raw.startswith("al:"):
        return (None, None)
    e = _lookup(store, lambda cat: cat.by_id.get(raw))
    if e is not None and e.kind == "album":
        return (e.artist if e.artist_l else "", e.name)
    return (None, None)


# ── Mappers (SoniqBoom → Subsonic schema) ────────────────────────────────────

def _safe_int(v: Any, default: int = 0, *, round_: bool = False) -> int:
    """Coerce to int, mapping None / non-numeric / NaN / inf to ``default``.

    A corrupt file can decode to a non-finite ``duration`` / ``bitrate`` /
    ``channels`` etc. (mutagen ``audio.info.length`` = nan).  Without this,
    ``int(round(nan))`` raises and — since @_wrap only catches _SubsonicError —
    500s the WHOLE Subsonic listing on one bad file (the same failure mode the
    replayGain guard fixes).

    ``round_=True`` rounds to nearest instead of truncating — used for
    ``duration`` so a 199.6 s track still reports 200 s (the pre-guard code did
    ``int(round(x))``; plain truncation would regress it to 199)."""
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return int(round(f)) if round_ else int(f)


class _SongCtx:
    """Per-request context for song mapping: the folder-albums flag, the
    (global) rating and play-count maps, the caller's starred songs and
    bookmarks, and the scan roots client paths are relative to — all O(1)
    lookups, so decorating a 5,000-song listing adds no per-item store work."""

    __slots__ = ("folder_on", "ratings", "starred", "store", "folder_names",
                 "plays", "marks", "fixed_album", "cat", "roots", "artist_ids")

    def __init__(self, store=None, user=None, *,
                 album: tuple[str, str] | None = None) -> None:
        self.store = store
        self.folder_names: dict[str, str] = {}      # dir_hash → folder album name
        self.artist_ids: dict[str, str] = {}        # name → ar: id (one sha1 per name)
        # ``album`` = (id, name): the songs are being listed INSIDE that
        # folder album (fa: getAlbum / getMusicDirectory) — album-less songs
        # point at it even when the setting is off, so parent / albumId /
        # album match the directory they're in.
        self.fixed_album = album
        self.folder_on = _folder_albums_on(store) if store is not None else False
        # The catalogue snapshot, fetched on first need (an album-less song
        # with folder albums on) — a listing of tagged songs never pays for it.
        self.cat: _sx.Catalogue | None = None
        self.roots = _sx.scan_roots(store) if store is not None else ()
        # Minimal stub stores (tests) may lack the private rating / play maps.
        # Live references (never copied); read on the event loop only.
        self.ratings = getattr(store, "_ratings", None) or {}
        self.plays = getattr(store, "_play_stats", None) or {}
        uid = getattr(user, "id", None)
        state = _get_state()
        self.starred = state.starred(uid, "song") if uid else {}
        self.marks = state.bookmarks(uid) if uid else {}

    def folder_entry(self, t: dict) -> _sx.AlbumEntry | None:
        """The folder album an album-less track belongs to — the whole
        folder's or its owner's share of a mixed one (O(1))."""
        cat = self.cat
        if cat is None:
            cat = self.cat = _catalogue(self.store)
        return cat.track_entry(t) if cat.folder_on else None


def _song_ctx(store, user, *, album: tuple[str, str] | None = None) -> _SongCtx:
    return _SongCtx(store, user, album=album)


# ── Source format → suffix / contentType / what /rest/stream delivers ───────
# ``suffix`` / ``contentType`` describe the SOURCE file (its real extension,
# a real MIME type — never one built from the display label, which produced
# "audio/x-fasttracker 2").  ``transcodedSuffix`` / ``transcodedContentType``
# describe what ``/rest/stream`` actually sends when the client asks for no
# ``format``: the file itself for browser-native formats and AAC/ALAC-in-MP4,
# a WAV for everything rendered (SID / tracker / Amiga / chip / MIDI) or
# transcoded by ffmpeg (AIFF, DSD, Musepack, WavPack, APE, WMA …).  Clients
# that request ``format=`` already know the codec they asked for.
_EXT_MIME = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg", "oga": "audio/ogg",
    "opus": "audio/ogg", "wav": "audio/wav", "m4a": "audio/mp4", "mp4": "audio/mp4",
    "m4b": "audio/mp4", "m4r": "audio/mp4", "aac": "audio/mp4", "3gp": "audio/mp4",
    "aif": "audio/aiff", "aiff": "audio/aiff", "aifc": "audio/aiff",
    "dsf": "audio/x-dsd", "dff": "audio/x-dsd", "wsd": "audio/x-dsd",
    "mpc": "audio/x-musepack", "wv": "audio/x-wavpack", "ape": "audio/x-ape",
    "wma": "audio/x-ms-wma", "mid": "audio/midi", "midi": "audio/midi", "kar": "audio/midi",
    "sid": "audio/prs.sid", "psid": "audio/prs.sid",
    "mod": "audio/x-mod", "xm": "audio/x-mod", "s3m": "audio/x-mod", "it": "audio/x-mod",
}
# Delivered unchanged by /rest/stream (no format=): api/stream.NATIVE plus the
# MP4 family when it holds AAC.  ALAC-in-MP4 is served as-is only to Safari
# (or a client that declared ALAC support) — a Subsonic app gets a WAV, so a
# track the scanner labelled "ALAC" advertises the WAV.
_DIRECT_EXTS = frozenset({"mp3", "flac", "wav", "ogg", "opus",
                          "m4a", "mp4", "m4b", "m4r", "aac", "3gp"})
_MP4_EXTS = frozenset({"m4a", "mp4", "m4b", "m4r", "aac", "3gp"})
_SUFFIX_JUNK = re.compile(r"[^a-z0-9]")
_DELIVERY_MEMO: dict[str, tuple[str, str, str | None, str | None]] = {}
_UADE_PREFIX_MEMO: dict[str, bool] = {}


def _uade_prefix(first: str) -> bool:
    """Is ``first`` an Amiga prefix-form token (``mdat.song``, ``fc13.x``)?
    Memoised (bounded); only asked for names whose extension is unknown."""
    hit = _UADE_PREFIX_MEMO.get(first)
    if hit is None:
        try:
            from soniqboom.core import uade_formats as _uf
            hit = _uf.classify(first + ".x") is not None
        except Exception:                    # noqa: BLE001 — no uade config
            hit = False
        if len(_UADE_PREFIX_MEMO) > 4096:
            _UADE_PREFIX_MEMO.clear()
        _UADE_PREFIX_MEMO[first] = hit
    return hit


def _delivery(t: dict) -> tuple[str, str, str | None, str | None]:
    """``(suffix, contentType, transcodedSuffix, transcodedContentType)`` for
    a track — the last two ``None`` when the bytes go out unchanged."""
    member = (t.get("path") or "").split("::")[-1].replace("\\", "/").rsplit("/", 1)[-1]
    first, dot, _rest = member.lower().partition(".")
    ext = _SUFFIX_JUNK.sub("", member.lower().rsplit(".", 1)[-1]) if dot else ""
    if dot and ext not in _EXT_MIME:
        pre = _SUFFIX_JUNK.sub("", first)
        if pre and _uade_prefix(pre):
            ext = pre                          # Amiga prefix form: mdat.<song>
    if not ext:
        ext = _SUFFIX_JUNK.sub("", (t.get("format") or "").split("/", 1)[0].lower())[:8] or "bin"
    hit = _DELIVERY_MEMO.get(ext)
    if hit is None:
        mime = _EXT_MIME.get(ext, "application/octet-stream")
        hit = (ext, mime, None, None) if ext in _DIRECT_EXTS else (ext, mime, "wav", "audio/wav")
        if len(_DELIVERY_MEMO) > 4096:
            _DELIVERY_MEMO.clear()
        _DELIVERY_MEMO[ext] = hit
    if ext in _MP4_EXTS and (t.get("format") or "").upper().startswith("ALAC"):
        return (ext, hit[1], "wav", "audio/wav")
    return hit


_PATH_SEG_JUNK = re.compile(r"[\\/\x00-\x1f]+")
_PATH_SEG_MEMO: dict[str, str] = {}


def _path_seg(s: str, default: str) -> str:
    """One synthetic path component (no separators / control characters, not
    "." or "..") — memoised: a listing repeats the same few artist / album
    names."""
    hit = _PATH_SEG_MEMO.get(s)
    if hit is None:
        hit = _PATH_SEG_JUNK.sub("_", (s or "").strip()).strip(". ")
        if len(_PATH_SEG_MEMO) >= 50_000:
            _PATH_SEG_MEMO.clear()
        _PATH_SEG_MEMO[s] = hit
    return hit or default


def _client_path(t: dict, ctx: _SongCtx | None, owner: str, album: str | None) -> str:
    """The song's ``path`` as clients see it (DSub builds its cache tree from
    it): relative to the scan root the file lives in, archive members as
    ``archive.zip/member`` — never the server's absolute path or a share's
    address.  A file outside every scan root (or with no context) gets a
    synthetic ``artist/album/file`` path.  O(#scan roots), no store work."""
    raw = t.get("path") or ""
    if raw and ctx is not None and ctx.roots:
        r = _sx.root_of(raw, ctx.roots)
        if r:
            rel = _sx.below_root(raw, r)
            if rel:
                return rel
    # The file name has no separators by construction (split on them).
    base = raw.split("::")[-1].replace("\\", "/").rsplit("/", 1)[-1].strip(". ") \
        or str(t.get("id") or "track")
    return f"{_path_seg(owner, _sx.UNKNOWN_ARTIST)}/{_path_seg(album or '', 'Unknown')}/{base}"


def _track_to_song(t: dict, ctx: _SongCtx | None = None) -> dict:
    """Map a SoniqBoom track dict to a Subsonic ``Child`` (song) object.

    ``artist`` / ``artistId`` are the TRACK artist (a compilation track shows
    and links its own artist); ``albumArtist`` / ``parent`` follow the owner
    (album artist, else track artist) the album grouping uses.

    Album-less tracks (retro archives) never get a dangling album id: with
    folder albums on — and always for a track with no artist at all, which
    ``[Unknown Artist]`` lists inside its folder — they point at their folder
    album (``fa:<dir_hash>`` or, in a mixed folder, their owner's share of
    it; album = the folder album's name); otherwise their parent is their
    artist, the directory ``getMusicDirectory`` lists them under, and
    ``albumId`` is omitted.  A placeholder artist tag (``<?>``) is shown as
    tagged but its ids point at ``[Unknown Artist]``.  ``path`` is relative to
    the file's scan root (never a server path or share address)."""
    aa = _sx.owner_of(t)
    al = (t.get("album") or "").strip()
    genre_list = t.get("genre") or []
    genre = genre_list[0] if genre_list else ""
    suffix, content_type, t_suffix, t_mime = _delivery(t)
    ar_tag = t.get("artist")
    ar_tag = ar_tag.strip() if isinstance(ar_tag, str) else ""
    ar = _sx.norm_owner(ar_tag) or aa
    ids = ctx.artist_ids if ctx is not None else None
    owner_id = ids.get(aa) if ids is not None else None
    if owner_id is None:
        owner_id = _artist_id(aa)
        if ids is not None:
            ids[aa] = owner_id
    # Owner "" → the reserved [Unknown Artist] (id of ""), which getIndexes
    # lists whenever such tracks exist — so these ids always resolve.
    if ar == aa:
        artist_id = owner_id
    else:
        artist_id = ids.get(ar) if ids is not None else None
        if artist_id is None:
            artist_id = _artist_id(ar)
            if ids is not None:
                ids[ar] = artist_id
    album_id: str | None = None
    dh = t.get("dir_hash")
    fixed = ctx.fixed_album if ctx is not None else None
    if al:
        album_id = _album_id(aa, al)
    elif dh and (fixed is not None or (ctx is not None and ctx.folder_on) or not aa):
        if fixed is not None:
            album_id, al = fixed
        elif ctx is not None and ctx.folder_on:
            e = ctx.folder_entry(t)
            if e is not None:
                album_id, al = e.id, e.name
        if album_id is None:
            album_id = _sx.folder_album_id(dh)
            names = ctx.folder_names if ctx is not None else None
            al = names.get(dh) if names is not None else None
            if al is None:
                al = _sx.folder_album_name(ctx.store if ctx is not None else None, dh)
                if names is not None:
                    names[dh] = al
    aa_disp = aa or _sx.UNKNOWN_ARTIST
    dur = t.get("duration")
    if t.get("start_subsong"):
        # A file whose default tune isn't tune 1: that tune's own length.
        dur = _sx.default_duration(t) or dur
    out: dict = {
        "id":         t["id"],
        "parent":     album_id or owner_id,
        "isDir":      False,
        "title":      t.get("title") or "",
        "album":      al,
        "artist":     ar_tag or aa_disp,
        "albumArtist": aa,
        "track":      _safe_int(t.get("track_number")),
        "year":       _normalise_year(t.get("year")),
        "genre":      genre,
        "coverArt":   t["id"],
        "size":       _safe_int(t.get("file_size")),
        "contentType": content_type,
        "suffix":     suffix,
        "duration":   _safe_int(dur, round_=True),
        "bitRate":    _safe_int((t.get("bitrate") or 0) / 1000),
        "path":       _client_path(t, ctx, aa_disp, al),
        "isVideo":    False,
        "type":       "music",
        "artistId":   artist_id,
        "discNumber": _safe_int(t.get("disc_number")),
    }
    if not out["year"]:
        del out["year"]             # optional: an unknown year is omitted, not 0
    # OpenSubsonic artist fields (the plain ``albumArtist`` above is kept for
    # existing clients).
    out["displayArtist"] = out["artist"]
    out["displayAlbumArtist"] = aa_disp
    out["artists"] = [{"id": artist_id, "name": ar or aa_disp}]
    out["albumArtists"] = [{"id": owner_id, "name": aa_disp}]
    if album_id is not None:
        out["albumId"] = album_id
    created = _iso(t.get("added_at"))
    if created:                     # "" is not a valid xs:dateTime — omit
        out["created"] = created
    if t_suffix:
        out["transcodedContentType"] = t_mime
        out["transcodedSuffix"] = t_suffix
    if ctx is not None:
        tid = t["id"]
        r = ctx.ratings.get(tid)
        if r:
            out["userRating"] = r
        st = ctx.starred.get(tid)
        if st:
            out["starred"] = _iso(st)
        ps = ctx.plays.get(tid)
        out["playCount"] = _safe_int(ps.get("count")) if isinstance(ps, dict) else 0
        if isinstance(ps, dict) and ps.get("last_played"):
            played = _iso(ps["last_played"])
            if played:
                out["played"] = played
        bm = ctx.marks.get(tid)
        if bm:
            out["bookmarkPosition"] = int(_ts_num(bm.get("position")))
    # ── OpenSubsonic optional fields ────────────────────────────────────────
    # Capability-aware clients (Symfonium, Amperfy, Feishin) read these to show
    # bit depth / sample rate, apply ReplayGain themselves, and render
    # multi-valued genres.  Audio-property fields are emitted only when the
    # track carries them (a missing value must never advertise a wrong one);
    # tag fields the spec lists are always present, empty when untagged.
    ch = _safe_int(t.get("channels"))
    if ch:
        out["channelCount"] = ch
    sr = _safe_int(t.get("sample_rate"))
    if sr:
        out["samplingRate"] = sr
    bd = _safe_int(t.get("bit_depth"))
    if bd:
        out["bitDepth"] = bd
    out["mediaType"] = "song"
    out["bpm"] = _safe_int(t.get("bpm"), round_=True)
    out["comment"] = t.get("comment") or ""
    out["displayComposer"] = t.get("composer") or ""
    isrc = t.get("isrc")
    out["isrc"] = [isrc] if isrc and isinstance(isrc, str) else []
    # OpenSubsonic `genres` is a repeated element with a `name` attribute.
    out["genres"] = [{"name": g} for g in genre_list if g]
    # ReplayGain — the exact fields Amperfy/Symfonium look for to level volume
    # server-side-tagged. dB gains + linear peaks; omit any that's absent.
    # A malformed tag can parse to nan/inf (float("nan") succeeds); Starlette's
    # JSONResponse serialises with allow_nan=False and would 500 the WHOLE
    # listing on one bad file, so drop any non-finite value here.
    def _fin(v, nd):
        if v is None:
            return None
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None
        return round(fv, nd) if math.isfinite(fv) else None
    rg: dict = {}
    for _k, _src, _nd in (("trackGain", "replaygain_track_gain", 2),
                          ("albumGain", "replaygain_album_gain", 2),
                          ("trackPeak", "replaygain_track_peak", 6),
                          ("albumPeak", "replaygain_album_peak", 6)):
        _v = _fin(t.get(_src), _nd)
        if _v is not None:
            rg[_k] = _v
    out["replayGain"] = rg
    return out


# ── Multi-tune files: one Child per tune ─────────────────────────────────────
# A SID / SNDH / NSF / tracker file with N subsongs is ONE track; a tune is
# reached by its wire index ``w`` — the web player's ``?subsong=w``, Subsonic's
# ``<track id>~<w>`` (0 ≤ w < N) — with the mapping the web player and the
# renderers share (``_sx.wire_tune``): wire 0 is the file's DEFAULT tune (its
# start song), so the bare id plays what the file plays by default and
# existing client caches keep working; the other wires are the other tunes.
# Listings add every other tune in tune order, capped per file like the web
# picker's rows (a 2,800-tune file can't flood a response).  ``~0`` is
# accepted and normalised to the bare id.  Searches, random / genre / starred
# / top lists are never expanded.  Album headers count the same entries
# (``_sx.tune_count`` / ``_sx.tune_duration_sum``).

_SUB_CAP = _sx.SUB_CAP


def _split_id(store, raw: str) -> tuple[str, int]:
    """A song id → ``(track id, wire)``: ``<track id>~<w>`` with ``0 ≤ w <``
    the track's tune count → ``(track id, w)``; anything else → ``(raw, 0)``
    (a bare id: wire 0, the default tune; or an id that resolves to
    nothing).  The wire is what the renderers take."""
    if not raw or "~" not in raw:
        return raw, 0
    base, _, n = raw.rpartition("~")
    if not (n.isdigit() and len(n) <= 6):
        return raw, 0
    k = int(n)
    t = store.get_track(base)
    if not t or not (0 <= k < _safe_int(t.get("subsongs"))):
        return raw, 0
    return base, k


def _canon_song_id(store, raw: str) -> str:
    """A song id with ``<track id>~0`` normalised to the bare id (the same
    tune), so a tune is never stored / listed twice; anything else as
    sent."""
    if not raw or "~" not in raw:
        return raw
    tid, n = _split_id(store, raw)
    return tid if tid != raw and n == 0 else raw


def _tune_child(song: dict, t: dict, n: int, ctx: _SongCtx | None) -> dict:
    """Wire ``n`` of the file ``song`` maps: ``song`` itself for wire 0 (the
    default tune); else the same album / art / tags with its own id, a
    "(Tune k/N)" title (the tune's number, as the web picker shows it) and
    the tune's own length when HVSC lists it — 0 (unknown) otherwise, never
    the file's length repeated."""
    if not n:
        return song
    cnt = _safe_int(t.get("subsongs"))
    c = dict(song)
    c["id"] = f"{t['id']}~{n}"
    c["title"] = f"{song.get('title') or ''} (Tune {_sx.wire_tune(t, n) + 1}/{cnt})".lstrip()
    c["duration"] = _safe_int(_sx.wire_length(t, n), round_=True)
    c.pop("bookmarkPosition", None)
    bm = ctx.marks.get(c["id"]) if ctx is not None else None
    if bm:
        c["bookmarkPosition"] = int(_ts_num(bm.get("position")))
    return c


def _with_tunes(t: dict, song: dict, ctx: _SongCtx | None) -> list[dict]:
    """``[song]`` (the default tune) plus a Child per other tune of a
    multi-tune file, in tune order (at most ``_SUB_CAP`` entries per file in
    all — ``_sx.listed_tunes``)."""
    ns = _sx.listed_tunes(t)
    if not ns:
        return [song]
    return [song] + [_tune_child(song, t, n, ctx) for n in ns]


# A big listing is mapped in time slices, the catalogue build's pattern: the
# clock is read every ``_MAP_CHECK`` tracks (a multi-tune file expands to many
# Children, and a song with tunes costs ~35 µs), and once ``_sx._SLICE_SEC``
# (6 ms) of work has run the event loop gets a turn — a sub-ms timer
# (``_sx._YIELD_SEC``), not ``sleep(0)``, so requests that arrived meanwhile
# run first.  Fixed 1000-track steps were up to 41 ms on a 17k-song album.
_MAP_CHECK = 64


async def _songs_with_tunes(tracks: list[dict], ctx: _SongCtx) -> list[dict]:
    """Every track's Child plus its extra tunes (``_with_tunes``), yielding
    to the event loop after every ~6 ms slice (``_MAP_CHECK``).  ``tracks``
    are dicts the caller already holds, so a concurrent store write can't
    change what is listed mid-way."""
    out: list[dict] = []
    clock = time.perf_counter
    t0 = clock()
    for i, tr in enumerate(tracks):
        if i and not i % _MAP_CHECK and clock() - t0 >= _sx._SLICE_SEC:
            await asyncio.sleep(_sx._YIELD_SEC)
            t0 = clock()
        out.extend(_with_tunes(tr, _track_to_song(tr, ctx), ctx))
    return out


def _normalise_year(y: Any) -> int:
    """Collapse YYYYMMDD-style ints to the year and coerce string years
    to int — Subsonic clients expect a plain integer."""
    if isinstance(y, str) and y.isdigit():
        y = int(y)
    if isinstance(y, int) and y > 9999:
        y = y // 10000
    return int(y) if isinstance(y, int) else 0


def _iso(ts: float | None) -> str:
    """Epoch seconds → an xs:dateTime in UTC WITH the zone designator
    (``2026-09-23T06:05:57Z``).  Without the ``Z`` clients parse it as local
    time — hours off, which broke play-queue "newer than" comparisons."""
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        # A bad timestamp — non-finite (NaN/inf, e.g. a corrupt or tampered
        # persistence snapshot reintroducing one via default json.loads, which
        # accepts NaN/Infinity), non-numeric, or out of range for the platform
        # time_t — must not make time.gmtime raise and, via @_wrap, blank the
        # WHOLE listing.  Same "one bad record can't kill the response"
        # rationale as _safe_int; degrade to an empty string.
        return ""


def _iso_req(*vals: Any) -> str:
    """For schema-REQUIRED dateTime attributes (playlist created / changed,
    play-queue changed, bookmark created / changed): the first usable value,
    else the epoch — never an empty string, which isn't an xs:dateTime."""
    for v in vals:
        out = _iso(v)
        if out:
            return out
    return "1970-01-01T00:00:00Z"


# ── Album / artist payload helpers ──────────────────────────────────────────

def _ts_num(v: Any) -> float:
    """A stored timestamp as a sortable number (a damaged value sorts last)."""
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else 0.0


def _album_id3(e: _sx.AlbumEntry, starred: dict | None = None,
               ratings: dict | None = None) -> dict:
    """``AlbumID3`` for a catalogue entry (getAlbumList2, getArtist, search3,
    getStarred2).  Empty optional fields are omitted — never ``coverArt: ""``.
    ``starred`` / ``ratings``: the caller's per-user maps (id → value)."""
    sample = e.sample or {}
    out: dict = {
        "id":        e.id,
        "name":      e.name,
        "artist":    e.artist,
        "songCount": e.song_count,
        "duration":  e.duration,
    }
    if e.year:                    # optional: an unknown year is omitted, not 0
        out["year"] = e.year
    created = _iso(e.added)
    if created:                   # "" is not a valid xs:dateTime — omit instead
        out["created"] = created
    if e.artist_id:
        out["artistId"] = e.artist_id
    cover = sample.get("id")
    if cover:
        out["coverArt"] = cover
    if e.genre:
        out["genre"] = e.genre
    st = (starred or {}).get(e.id)
    if st:
        out["starred"] = _iso(st)
    r = (ratings or {}).get(e.id)
    if r:
        out["userRating"] = r
    return out


def _album_child(e: _sx.AlbumEntry, starred: dict | None = None,
                 ratings: dict | None = None) -> dict:
    """The same album as a directory ``Child`` (getMusicDirectory children,
    getAlbumList v1, getStarred v1): adds ``isDir`` / ``title`` / ``parent``."""
    out = _album_id3(e, starred, ratings)
    out["isDir"] = True
    out["title"] = e.name
    out["album"] = e.name
    if e.artist_id:
        out["parent"] = e.artist_id
    return out


def _album_tracks(store, aa: str, al: str) -> list[dict]:
    """Tracks of the album ``(owner, album)`` — owner = album artist, else
    track artist, the same grouping the catalogue uses — in disc/track order.

    O(|album-name bucket|) via the ``_tag_album`` index (NOT ``filter_tracks``,
    whose sorted-index walk never reaches its limit for a small album and so
    scans the whole library)."""
    tag_album = getattr(store, "_tag_album", None)
    if tag_album is None:
        # Minimal stub stores (unit tests) expose only filter_tracks.
        tracks = (store.filter_tracks(album_artist=aa, album=al)
                  or store.filter_tracks(artist=aa, album=al))
    else:
        aa_l = (aa or "").strip().lower()
        tracks = []
        for tid in tag_album.get((al or "").strip().lower(), ()):
            t = store.get_track(tid)
            if t and _sx.owner_of(t).lower() == aa_l:
                tracks.append(t)
    tracks = list(tracks)
    tracks.sort(key=lambda x: (_safe_int(x.get("disc_number")),
                               _safe_int(x.get("track_number")),
                               (x.get("title") or "").lower()))
    return tracks


def _artist_track_ids(store, name_l: str) -> set[str]:
    """Every track whose album artist OR track artist is ``name_l`` —
    O(artist size) set union over the tag indexes."""
    a = store._tag_album_artist.get(name_l) or set()
    b = store._tag_artist.get(name_l) or set()
    return a | b if (a and b) else set(a or b)


def _loose_tracks(store, name_l: str, cat: _sx.Catalogue | None,
                  listed: set[str] | frozenset = frozenset()) -> list[dict]:
    """Album-less tracks filed under artist ``name_l`` (owner match).  With
    folder albums on, tracks whose folder album is already a child directory
    here (``listed``: the artist's own folder album or own share of a mixed
    folder, or — for an artist with no albums of their own — one they appear
    on) are excluded; the rest stay as songs, so every track remains
    reachable under its own artist."""
    out: list[dict] = []
    folder_on = bool(cat and cat.folder_on)
    # The owner-less artist has no tag-index bucket ("" is never indexed);
    # the catalogue pass collected its album-less track ids instead.
    cands = (cat.unknown_loose if cat is not None else ()) if name_l == "" \
        else _artist_track_ids(store, name_l)
    for tid in cands:
        t = store.get_track(tid)
        if not t or (t.get("album") or "").strip():
            continue
        if _sx.owner_of(t).lower() != name_l:
            continue
        if folder_on and listed:
            e = cat.track_entry(t)
            if e is not None and e.id in listed:
                continue
        out.append(t)
    out.sort(key=lambda x: ((x.get("title") or "").lower(), x.get("id") or ""))
    return out


def _unknown_folder_entries(store, cat: _sx.Catalogue) -> tuple[list, list[dict]]:
    """The ``[Unknown Artist]`` directory's album-less tracks, grouped by
    folder: ``(folder-album entries, tracks with no folder)``.

    Owner-less tracks have no artist to hang off, and a real retro library has
    tens of thousands of them (37k in the owner's 263k library) — as loose
    songs that would be one unusable multi-megabyte directory.  Their folder
    is the only structure they have, so they're listed as ``fa:`` folder
    directories (which resolve whether or not folder albums are on).
    Memoised per snapshot; O(album-less tracks in those folders) once."""
    hit = cat.memo.get("unknown_dirs")
    if hit is not None:
        return hit
    by_dir: dict[str, dict] = {}         # dir_hash → newest owner-less track
    counts: dict[str, int] = {}
    no_dir: list[dict] = []
    for tid in cat.unknown_loose:
        t = store.get_track(tid)
        if not t:
            continue
        dh = t.get("dir_hash")
        if dh:
            cur = by_dir.get(dh)
            if cur is None or _sx.sample_key(t) > _sx.sample_key(cur):
                by_dir[dh] = t
            counts[dh] = counts.get(dh, 0) + _sx.tune_count(t)
        else:
            no_dir.append(t)
    entries = []
    seen: set = set()
    for dh, newest in by_dir.items():
        e = cat.by_id.get(_sx.FOLDER_PREFIX + dh)
        if e is None and cat.folder_on:
            # A mixed folder: its owner-less share is its own album — or the
            # letter-bucket group's, listed once for all its buckets.
            g = cat.dir_group.get(dh)
            e = cat.by_id.get(_sx.group_album_id(g, "") if g else _sx.folder_album_id(dh, ""))
            if e is not None:
                if e.id in seen:
                    continue
                seen.add(e.id)
        if e is None:
            # Folder albums off: a light directory stub — no per-folder track
            # scan (that was ~350 ms for 16k folders).  getMusicDirectory /
            # getAlbum on its fa: id compute the exact contents on demand.
            e = _sx.AlbumEntry()
            e.id = _sx.FOLDER_PREFIX + dh
            e.kind = "folder"
            e.dir_hash = dh
            e.name = _sx.folder_album_name(store, dh)
            e.artist_l = ""
            e.artist = _sx.UNKNOWN_ARTIST
            e.artist_id = cat.union.artist_id("")
            e.song_count = counts[dh]
            e.duration = 0
            e.sample = newest
            e.year = _normalise_year(newest.get("year"))
            e.genre = ((newest.get("genre") or [""])[0]) or ""
            e.genres_l = frozenset()
            e.added = newest.get("added_at") or 0
        entries.append(e)
    entries.sort(key=lambda e: (e.name.lower(), e.id))
    no_dir.sort(key=lambda x: ((x.get("title") or "").lower(), x.get("id") or ""))
    cat.memo["unknown_dirs"] = hit = (entries, no_dir)
    return hit


def _resolve_album_entry(store, raw: str,
                         cat: _sx.Catalogue | None = None) -> _sx.AlbumEntry | None:
    """Any album id this server hands out → a catalogue-shaped entry.  Folder
    album ids resolve even while the setting is off (a client may have cached
    one), straight from the dir-hash index without the full pass."""
    if cat is None and raw.startswith("al:"):
        # Retry a miss on a rebuilt snapshot (a brand-new album's id).
        e = _lookup(store, lambda c: c.by_id.get(raw))
        cat = _catalogue(store)
    else:
        cat = cat or _catalogue(store)
        e = cat.by_id.get(raw)
    if e is not None:
        return e
    # fa: ids resolve straight from the dir-hash index — never stale.
    ref = _sx.parse_folder_album_ref(raw)
    if ref:
        dh, okey = ref
        return _sx.folder_entry_from_tracks(
            store, cat.union, dh, _sx.folder_album_tracks(store, dh, okey), okey)
    gref = _sx.parse_group_ref(raw)
    if gref:
        anc, okey = gref
        return _sx.folder_entry_from_tracks(
            store, cat.union, anc, _sx.group_album_tracks(store, cat, anc, okey), okey,
            group=True)
    return None


# ── Auth ─────────────────────────────────────────────────────────────────────
# ``p=`` (plain / enc:hex) is checked in O(1) first: against the user's
# ``subsonic_password`` (the Subsonic app password, or the login password it
# was seeded with), then against the login password this process already
# verified with scrypt (``UserStore.check_cached_password``).  A client that
# sends ``p=`` on every request therefore no longer pays a ~60 ms scrypt
# (serialised on the event loop) per call.  Only a miss — a wrong password, or
# the first request after a restart — runs the scrypt check with the lockout
# accounting; inside a ``@_wrap`` handler it runs in a worker thread
# (``_NeedScrypt``), never on the loop.  A locked-out account skips the fast
# paths, so they can't be used to test guesses.

class _NeedScrypt(Exception):
    """``p=`` needs the scrypt check: ``_wrap`` runs it off the event loop
    and re-runs the handler with the verified user (``_PREAUTH``)."""

    def __init__(self, username: str, plain: str) -> None:
        self.username = username
        self.plain = plain


# True while the current handler run may raise ``_NeedScrypt`` (``_wrap``'s
# attempts); anywhere else the scrypt check runs inline, as before.
_SCRYPT_OFFLOAD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "subsonic_scrypt_offload", default=False)
# (username lower-cased, plaintext, user) verified off-loop for this request.
_PREAUTH: contextvars.ContextVar[tuple | None] = contextvars.ContextVar(
    "subsonic_preauth", default=None)

# Concurrent off-loop scrypt checks (each needs ~32 MB): a burst of
# wrong-password requests queues here instead of fanning out to every worker.
_SCRYPT_SLOTS = 2
_SCRYPT_GATE: dict = {"loop": None, "sem": None}


def _scrypt_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if _SCRYPT_GATE["loop"] is not loop:
        _SCRYPT_GATE.update(loop=loop, sem=asyncio.Semaphore(_SCRYPT_SLOTS))
    return _SCRYPT_GATE["sem"]


def _p_authenticate(store):
    """The scrypt check for a ``p=`` password: the store's
    ``authenticate_subsonic_password`` (failures counted in the ``p``
    lockout scope, so a stale device can't lock the web login), or plain
    ``authenticate`` for a store without it."""
    return getattr(store, "authenticate_subsonic_password", None) or store.authenticate


async def _scrypt_authenticate(username: str, plain: str) -> User | None:
    store = get_user_store()
    async with _scrypt_semaphore():
        return await asyncio.to_thread(_p_authenticate(store), username, plain)


def _pw_equal(a: str, b: str) -> bool:
    # bytes: hmac.compare_digest rejects non-ASCII *str* operands.
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _resolve_user(
    request: Request,
    sb_session: str | None,
    u: str | None,
    p: str | None,
    s: str | None,
    t: str | None,
) -> User | None:
    """Resolve the caller's user — via session cookie, Subsonic password
    param, HTTP Basic header, or Subsonic token (``s+t``).

    ``p=`` / Basic accept the login password or the Subsonic app password.
    Token mode (``t = md5(secret + s)``) checks ``subsonic_password``: the
    app password the user generated, or — for an account that never set one —
    the login password it was seeded with.  Token-mode and ``p=`` failures
    are each counted in their own lockout scope ("token" / "p"), so guessing
    is bounded without letting a client stuck on a stale token or a retired
    app password lock the web login; the ``p=`` fast paths are refused while
    either the main or the ``p`` scope is locked."""
    store = get_user_store()

    # 1. Session cookie wins — useful for in-browser Subsonic clients.
    if sb_session:
        user = store.lookup_session(sb_session)
        if user:
            return user

    # 2. HTTP Basic — some clients send credentials this way.
    auth = request.headers.get("authorization", "")
    if not u and auth.lower().startswith("basic "):
        import base64
        try:
            raw = base64.b64decode(auth[6:]).decode("utf-8")
            u, _, p = raw.partition(":")
        except Exception:
            pass

    is_locked = getattr(store, "is_locked", None)

    # 3. Subsonic password mode (?p=password or ?p=enc:hex).
    if u and p is not None:
        plain = _decode_password(p)
        pre = _PREAUTH.get()
        if pre is not None and pre[0] == u.lower() and _pw_equal(pre[1], plain):
            return pre[2]                       # verified off-loop by _wrap
        cand = store.get_by_username(u) if hasattr(store, "get_by_username") else None
        if cand is not None and not (is_locked and (is_locked(u) or is_locked(u, "p"))):
            if cand.subsonic_password and _pw_equal(plain, cand.subsonic_password):
                return cand
            cached = getattr(store, "check_cached_password", None)
            if cached is not None and cached(u, plain) is not None:
                return cand
        if _SCRYPT_OFFLOAD.get():
            raise _NeedScrypt(u, plain)
        return _p_authenticate(store)(u, plain)

    # 4. Subsonic token mode (?s=salt&t=md5(password+salt)).
    if u and s is not None and t is not None:
        if is_locked and is_locked(u, "token"):
            return None
        cand = store.get_by_username(u) if hasattr(store, "get_by_username") else None
        if cand and cand.subsonic_password:
            expected = hashlib.md5(
                (cand.subsonic_password + s).encode("utf-8")
            ).hexdigest()
            if _pw_equal(expected, t.lower()):
                return cand
        note = getattr(store, "note_failed_attempt", None)
        if note:
            note(u, "token")
        return None
    return None


def _decode_password(p: str) -> str:
    """Subsonic clients can send ``p=password`` or ``p=enc:<hex(password)>``."""
    if p.startswith("enc:"):
        try:
            return bytes.fromhex(p[4:]).decode("utf-8")
        except ValueError:
            return ""
    return p


def _api_key_user(request: Request, u, p, s, t) -> User | None:
    """OpenSubsonic ``apiKey``: the key's owner, ``None`` when no key was
    sent; error 43 when combined with another mechanism, 44 when invalid."""
    qp = getattr(request, "query_params", None)
    key = qp.get("apiKey") if qp is not None else None
    if key is None:
        return None
    if u or p or s or t or _has_basic_auth(request):
        raise _SubsonicError(43, "Multiple conflicting authentication mechanisms "
                                 "provided — send apiKey alone (no u / p / t / s).")
    ustore = get_user_store()
    entry = getattr(ustore, "lookup_api_key_entry", None)
    if entry is not None:
        hit = entry(key)
        user = hit[0] if hit else None
        if hit:
            # Links this request mints (radio / cover URLs) die with the key.
            with contextlib.suppress(Exception):
                request.state.sb_api_key_id = hit[1]
    else:
        lookup = getattr(ustore, "lookup_api_key", None)
        user = lookup(key) if lookup else None
    if user is None:
        raise _SubsonicError(44, "Invalid API key.")
    return user


def _refuse_password_and_token(u, p, s, t) -> None:
    """OpenSubsonic: ``p`` together with ``t`` + ``s`` is two conflicting
    mechanisms — error 43, checked before anything is verified (no scrypt,
    no lockout count).  (An HTTP Basic header next to them is left alone: a
    reverse proxy's own Basic auth is forwarded with every request.)"""
    if u and p is not None and s is not None and t is not None:
        raise _SubsonicError(43, "Multiple conflicting authentication mechanisms provided "
                                 "— send either p or t + s, not both.")


def _require_user(
    request: Request,
    sb_session: str | None,
    u: str | None, p: str | None, s: str | None, t: str | None,
) -> User:
    """Resolve + check enabled; raise the appropriate Subsonic error: 10 when
    no usable credentials were sent at all, 40 when they were wrong.

    ``f=jsonp`` never rides the session cookie: a JSONP body is readable by
    any page that loads it with ``<script>`` — including a same-site page on
    another port or subdomain, which SameSite=Lax doesn't stop — so it needs
    explicit credentials (u + p, u + t + s, an API key or HTTP Basic)."""
    qp = getattr(request, "query_params", None)
    if qp is not None and (qp.get("f") or "").lower() == "jsonp":
        sb_session = None
    user = _api_key_user(request, u, p, s, t)
    if user is not None:
        return user                          # enabled-checked by the lookup
    _refuse_password_and_token(u, p, s, t)
    user = _resolve_user(request, sb_session, u, p, s, t)
    if user is None:
        if u and s is not None and t is not None:
            # Token mode failed: say which secret it checks and where it is
            # set, instead of a bare "wrong password".
            raise _SubsonicError(40, "Wrong username or password. Apps that sign in with "
                                     "a token use your SoniqBoom password, or your Subsonic "
                                     "app password if you generated one (My Account; "
                                     "Preferences for non-admin accounts).")
        if not _has_basic_auth(request) and (not u or (p is None and (s is None or t is None))):
            raise _SubsonicError(10, "Required parameter 'u' and 'p' (or 't' and 's') "
                                     "is missing.")
        raise _SubsonicError(40, "Wrong username or password.")
    if not user.enabled:
        raise _SubsonicError(40, "Account disabled.")
    return user


def _require_user_no_cookie(
    request: Request,
    sb_session: str | None,
    u: str | None, p: str | None, s: str | None, t: str | None,
) -> User:
    """Same as ``_require_user`` but refuses cookie-only auth — used on
    mutation endpoints (createPlaylist / updatePlaylist / deletePlaylist /
    scrobble) where a cookie-only request could be triggered by an
    attacker-origin ``<img src=...>`` (CSRF).  By requiring explicit
    Subsonic credentials (u + p, u + t + s, an API key, or HTTP Basic), we
    ensure the request came from a real Subsonic client, not a drive-by
    browser tab."""
    # Same-origin requests can also be allowed (the Origin header must
    # match the request host); this lets in-browser dev still work.
    origin = request.headers.get("origin") or ""
    same_origin = origin and (
        origin == f"{request.url.scheme}://{request.url.netloc}"
    )
    if same_origin:
        return _require_user(request, sb_session, u, p, s, t)
    qp = getattr(request, "query_params", None)
    has_key = qp is not None and qp.get("apiKey") is not None
    if u or s or t or has_key or _has_basic_auth(request):
        # The explicit credentials must authenticate ON THEIR OWN: drop the
        # cookie, which ``_resolve_user`` would otherwise prefer — a
        # ``?u=anything`` link (top-level navigation carries a Lax cookie)
        # must not ride the victim's session into a mutation.
        return _require_user(request, None, u, p, s, t)
    # No explicit credentials at all: the spec's "required parameter
    # missing" (10), not "wrong password" (40).
    raise _SubsonicError(
        10,
        "This endpoint requires Subsonic credentials (u + p, u + t + s or an "
        "API key) — cookie-only auth is rejected on mutation endpoints "
        "to prevent CSRF.",
    )


def _has_basic_auth(request: Request) -> bool:
    a = request.headers.get("authorization", "")
    return a.lower().startswith("basic ")


class _SubsonicError(Exception):
    def __init__(self, code: int, message: str, *, http_status: int | None = None):
        self.code = code
        self.message = message
        # The HTTP status of the failure this maps (a stream error), for the
        # callers that answer in plain HTTP (getTranscodeStream spec form).
        self.http_status = http_status


def _stream_http_error(exc: HTTPException) -> _SubsonicError:
    """A stream-handler HTTP error → a Subsonic error with client-safe text.
    The handler's own detail can name a server path or a share's address
    ("Source offline: ftp://10.0.0.88/…", "File not found on disk: /music/…"),
    which no client may see: it is logged for the operator instead."""
    st = int(exc.status_code)
    log.info("Subsonic stream failed (HTTP %s): %s", st, exc.detail)
    if st in (404, 410):
        return _SubsonicError(70, "This song's file is missing on the server.", http_status=st)
    if st == 503:
        return _SubsonicError(0, "The music source for this song is offline.", http_status=st)
    return _SubsonicError(0, "The server could not prepare this song.", http_status=st)


# ── Dependency wrapper that converts _SubsonicError → JSON envelope ─────────

def _wrap(handler):
    """Decorator: any handler raising ``_SubsonicError`` returns a proper
    Subsonic error envelope instead of an HTTP 4xx.

    It also owns the two "do the slow part off the loop, then re-run"
    retries: ``_NeedScrypt`` (a ``p=`` that needs the scrypt check — run in a
    worker thread) and ``_StaleMiss`` (an id missing from a stale catalogue
    snapshot of a large library — await the chunked background rebuild).
    Each happens at most once per request; handlers resolve auth and ids
    before any side effect, so a re-run repeats nothing observable.

    ``functools.wraps`` is essential here — FastAPI inspects the wrapped
    function's signature to know which query params, cookies, and
    dependencies to inject.  Without it the wrapper looks like
    ``(*args, **kwargs)`` and FastAPI rejects every call with 422."""
    @functools.wraps(handler)
    async def _wrapped(*args, **kwargs):
        fmt = kwargs.get("f", "xml")
        tokens = [(_SCRYPT_OFFLOAD, _SCRYPT_OFFLOAD.set(True)),
                  (_STALE_RETRY_OK, _STALE_RETRY_OK.set(True))]
        try:
            while True:
                try:
                    return await handler(*args, **kwargs)
                except _NeedScrypt as need:
                    user = await _scrypt_authenticate(need.username, need.plain)
                    if user is None:
                        return _err(40, "Wrong username or password.", fmt=fmt)
                    tokens.append((_PREAUTH, _PREAUTH.set(
                        (need.username.lower(), need.plain, user))))
                    tokens.append((_SCRYPT_OFFLOAD, _SCRYPT_OFFLOAD.set(False)))
                except _StaleMiss:
                    await _refresh_for_miss(get_store())
                    tokens.append((_STALE_RETRY_OK, _STALE_RETRY_OK.set(False)))
                except _SubsonicError as e:
                    # Honour the caller's requested format on the error too —
                    # every handler takes ``f`` as a keyword (FastAPI injects by
                    # name), so a JSON client (Symfonium/Feishin) gets a JSON
                    # error envelope it can parse rather than XML it chokes on.
                    return _err(e.code, e.message, fmt=fmt)
                except HTTPException:
                    # Auth/redirect helpers raise these deliberately (e.g. 401
                    # with a WWW-Authenticate header); let Starlette render
                    # them unchanged.
                    raise
                except Exception:  # noqa: BLE001 — last-resort envelope, not a swallow
                    # Any other unexpected failure would otherwise surface as a
                    # raw HTTP 500 (or, for a non-finite float reaching
                    # JSONResponse, a 500 from ``allow_nan=False``) — which
                    # Subsonic clients treat as a transport error, not a
                    # protocol error, and often show as "server unreachable".
                    # Convert it to a generic Subsonic error envelope (code 0)
                    # so the client shows a real message; log the traceback so
                    # the operator can still see it.
                    log.exception("Unhandled error in Subsonic handler %s", handler.__name__)
                    return _err(0, "An internal server error occurred.", fmt=fmt)
        finally:
            for var, tok in reversed(tokens):
                var.reset(tok)
    return _wrapped


# ── Endpoints ────────────────────────────────────────────────────────────────

@_route("/ping")
@_route("/ping.view")
@_wrap
async def ping(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
    c: str = Query(default=""),
    v: str = Query(default=""),
):
    _require_user(request, sb_session, u, p, s, t)
    return _ok(fmt=f)


@_route("/getOpenSubsonicExtensions")
@_route("/getOpenSubsonicExtensions.view")
@_wrap
async def get_open_subsonic_extensions(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    # OpenSubsonic's extension-discovery endpoint — the ONLY place the list is
    # served (the spec's envelope carries just ``openSubsonic: true``).
    # Public by spec (clients probe it before they know which auth mode to
    # use); it reveals nothing but the capability list.
    return _ok({"openSubsonicExtensions": _OPENSUBSONIC_EXTENSIONS}, fmt=f)


@_route("/getLicense")
@_route("/getLicense.view")
@_wrap
async def get_license(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    return _ok({
        "license": {
            "valid": True,
            "email": "",
            "licenseExpires": "2099-12-31T00:00:00Z",
        },
    }, fmt=f)


@_route("/getMusicFolders")
@_route("/getMusicFolders.view")
@_wrap
async def get_music_folders(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    folders = _music_folders(get_store())
    rows = ([{"id": x["id"], "name": x["name"]} for x in folders]
            or [{"id": _FOLDER_ID, "name": _FOLDER_NAME}])
    return _ok({"musicFolders": {"musicFolder": rows}}, fmt=f)


# ── Music folders = scan roots ───────────────────────────────────────────────
# Each scan root is a music folder with a stable int id (from its path hash —
# Java / Android clients parse it as an int, so it stays within 31 bits and
# never 0).  ``musicFolderId`` narrows the lists / indexes / searches that take
# it; no id, 0 (the single virtual folder older builds reported, which a
# client may have cached) or an unknown id means the whole library.  Nothing
# is computed unless a client actually filters.

_MUSIC_FOLDERS: dict = {"sig": None, "rows": [], "by_id": {}}


def _folder_label(path: str) -> str:
    """A root's display name: its last path segment — never a share URL's
    scheme, credentials or host (unless the root IS the host)."""
    segs = [x for x in re.split(r"[\\/]", path) if x.strip()]
    name = segs[-1] if segs else path
    if "@" in name:
        name = name.rsplit("@", 1)[-1]           # user:pass@host → host
    return name.strip() or "Library"


def _music_folders(store) -> list[dict]:
    """``[{id, name, hash}]`` — one per scan root (unavailable ones too),
    ordered by name; memoised on the scan-dir set."""
    sd = getattr(store, "_scan_dirs", None)
    if not isinstance(sd, dict):
        return []
    sig = tuple((p, (v or {}).get("path_hash") if isinstance(v, dict) else None)
                for p, v in sd.items())
    c = _MUSIC_FOLDERS
    if c["sig"] == sig:
        return c["rows"]
    rows, used = [], set()
    for path, ph in sorted(sig, key=lambda x: x[0]):
        if not isinstance(path, str) or not path:
            continue
        ph = ph or hashlib.sha256(path.encode()).hexdigest()[:16]
        fid = int(ph[:7], 16) + 1                     # 1 … 2^28: a positive int32
        while fid in used:
            fid += 1
        used.add(fid)
        rows.append({"id": fid, "name": _folder_label(path), "hash": ph})
    rows.sort(key=lambda x: (x["name"].lower(), x["id"]))
    c.update(sig=sig, rows=rows, by_id={x["id"]: x["hash"] for x in rows})
    return rows


def _folder_hash(store, music_folder_id: Any) -> str | None:
    """The scan-root hash a ``musicFolderId`` names, or None (= no filter)."""
    if music_folder_id in (None, ""):
        return None
    try:
        fid = int(str(music_folder_id).strip())
    except (TypeError, ValueError):
        return None
    if fid <= 0:
        return None
    _music_folders(store)
    return _MUSIC_FOLDERS["by_id"].get(fid)


def _root_track_ids(store, root_hash: str) -> set:
    """The live tag-index set of a root's tracks (read-only use)."""
    idx = getattr(store, "_tag_scan_root_hash", None) or {}
    return idx.get(root_hash) or set()


def _folder_view(store, cat: _sx.Catalogue, root_hash: str) -> tuple[frozenset, frozenset]:
    """``(album ids, artist keys)`` with at least one track in the root —
    one O(root size) pass per catalogue snapshot and root, memoised."""
    key = ("folder_view", root_hash)
    hit = cat.memo.get(key)
    if hit is not None:
        return hit
    albums: set = set()
    artists: set = set()
    for tid in _root_track_ids(store, root_hash):
        t = store.get_track(tid)
        if not t:
            continue
        e = cat.track_entry(t)
        if e is not None:
            albums.add(e.id)
        o = _sx.owner_of(t).lower()
        artists.add(o)
        a = _sx.norm_owner(t.get("artist")).lower()
        if a:
            artists.add(a)
    hit = cat.memo[key] = (frozenset(albums), frozenset(artists))
    return hit


async def _prepare_folder_view(store, cat: _sx.Catalogue, root_hash: str | None) -> None:
    """Fill ``_folder_view``'s memo with the event loop getting a turn after
    every ~6 ms slice (the catalogue build's ``_sx._SLICE_SEC`` /
    ``_sx._YIELD_SEC``; a 60k-track root was ~70 ms in one go, fixed
    5000-track steps 23-39 ms).  The root's id set is copied first — a live
    set can't be iterated across awaits."""
    if not root_hash or ("folder_view", root_hash) in cat.memo:
        return
    ids = list(_root_track_ids(store, root_hash))
    albums: set = set()
    artists: set = set()
    clock = time.perf_counter
    t0 = clock()
    for i, tid in enumerate(ids):
        if not i & 255 and clock() - t0 >= _sx._SLICE_SEC:
            await asyncio.sleep(_sx._YIELD_SEC)
            t0 = clock()
        t = store.get_track(tid)
        if not t:
            continue
        e = cat.track_entry(t)
        if e is not None:
            albums.add(e.id)
        artists.add(_sx.owner_of(t).lower())
        a = _sx.norm_owner(t.get("artist")).lower()
        if a:
            artists.add(a)
    cat.memo.setdefault(("folder_view", root_hash), (frozenset(albums), frozenset(artists)))


def _playable_albums(cat: _sx.Catalogue, base: list, dead: frozenset) -> list:
    """``base`` without the albums whose source is offline (``dead`` scan
    roots, judged by the album's sample track — an album spanning roots is
    rare), for random album picks; memoised per snapshot and dead set.
    Nothing reachable → ``base`` (the endpoint still answers)."""
    if not dead:
        return base
    key = ("live_albums", dead)
    hit = cat.memo.get(key)
    if hit is None:
        hit = [e for e in base if (e.sample or {}).get("scan_root_hash") not in dead]
        cat.memo[key] = hit = hit or base
    return hit


def _in_folder(cat: _sx.Catalogue, name: Any, base: list, root_hash: str | None,
               store) -> list:
    """``base`` (a memoised catalogue list, key ``name``) narrowed to one
    music folder's albums — memoised per snapshot, root and list."""
    if not root_hash:
        return base
    key = ("in_folder", name, root_hash)
    hit = cat.memo.get(key)
    if hit is None:
        ids = _folder_view(store, cat, root_hash)[0]
        hit = cat.memo[key] = [e for e in base if e.id in ids]
    return hit


@_route("/tokenInfo")
@_route("/tokenInfo.view")
@_wrap
async def token_info(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    # OpenSubsonic apiKeyAuthentication: which account the credentials (an
    # API key, or any other mechanism) belong to.  An invalid key is 44.
    user = _require_user(request, sb_session, u, p, s, t)
    return _ok({"tokenInfo": {"username": user.username}}, fmt=f)


_IGNORED_ARTICLES = "The El La Los Las Le Les"
_ARTICLE_PREFIXES = tuple(a.lower() + " " for a in _IGNORED_ARTICLES.split())
_ARTICLE_INITIALS = frozenset(p[0] for p in _ARTICLE_PREFIXES)
# Song search's own accent fold (core/store.py — ``FOLD_EXTRA`` letters, marks
# dropped after a Latin base only), uncached: artist / album
# names are memoised per snapshot in ``cat.memo`` already, and routing whole
# names through the store's per-token lru_cache would evict its search
# tokens.  One definition, so name and song matching cannot drift apart.
_fold_text = getattr(_store_mod._fold_nonascii, "__wrapped__", _store_mod._fold_nonascii)


def _fold(s: str) -> str:
    """Lower-cased with accents folded exactly like song search ("Öörni" →
    "oorni", "Ødegaard" → "odegaard", "Beyoncé" → "beyonce"; a mark after a
    non-Latin letter is kept, so が ≠ か and й ≠ и) — one ``isascii`` check
    for the common ASCII name."""
    s = (s or "").lower()
    if s.isascii():
        return s
    return _fold_text(s)


def _index_sort_name(name: str) -> str:
    """The key an artist is bucketed and sorted by: lower-cased, a leading
    ignored article dropped ("The Beatles" → "beatles"; a band called just
    "The" keeps it) and accents folded ("Émilie" → "emilie", "Ødegaard" →
    "odegaard") — what ``ignoredArticles`` promises clients."""
    key = (name or "").strip().lower()
    if key[:1] in _ARTICLE_INITIALS:
        for pre in _ARTICLE_PREFIXES:
            if key.startswith(pre) and key[len(pre):].strip():
                key = key[len(pre):].lstrip()
                break
    return _fold(key)


def _index_letter(key: str) -> str:
    """Bucket label for an ``_index_sort_name`` key — A-Z, else '#' (digits,
    symbols, non-Latin scripts).  (``_build_artist_index`` inlines this.)"""
    ch = key[:1]
    return ch.upper() if "a" <= ch <= "z" else "#"


def _build_artist_index(cat: _sx.Catalogue, union: _sx.ArtistUnion,
                        only: frozenset | None = None) -> list[dict]:
    """The A–Z ``index`` list for getArtists / getIndexes (``only``: just
    these artist keys — one music folder's artists).

    Artists = album artists ∪ track artists (case-insensitive, album-artist
    casing wins).  ``albumCount`` is the exact number of albums ``getArtist``
    returns for the same id — own albums (incl. their folder albums), else
    the albums they appear on — read from the catalogue in O(1).

    Pure: reads only the (immutable, installed) snapshot, so it may run in a
    worker thread."""
    buckets: dict[str, list[tuple]] = {}
    id_of = union.id_of
    for low, name in union.display.items():
        if only is not None and low not in only:
            continue
        aid = id_of.get(low) or union.artist_id(low)
        key = _index_sort_name(name)
        ch = key[:1]
        letter = ch.upper() if ("a" <= ch <= "z") else "#"
        buckets.setdefault(letter, []).append(((key, low, aid), {
            "id":         aid,
            "name":       name,
            "albumCount": cat.album_count(low),
            "coverArt":   aid,
        }))
    first = operator.itemgetter(0)
    indexed = []
    for letter in sorted(buckets.keys()):
        rows = buckets[letter]
        rows.sort(key=first)
        indexed.append({"name": letter, "artist": [r for _k, r in rows]})
    return indexed


def _index_fingerprint(indexed: list[dict]) -> str:
    """A digest of every bucket / id / name / albumCount — getIndexes'
    ``lastModified`` moves only when this does.  Pure (thread-safe)."""
    h = hashlib.sha1()
    for bucket in indexed:
        # The bucket name too: a regrouping that keeps every row the same
        # (e.g. "The Beatles" moving from T to B) is still a change.
        h.update(f'#{bucket["name"]}\n'.encode("utf-8"))
        for r in bucket["artist"]:
            h.update(f'{r["id"]}\0{r["name"]}\0{r["albumCount"]}\n'.encode("utf-8"))
    return h.hexdigest()


def _artist_index(cat: _sx.Catalogue, union: _sx.ArtistUnion) -> list[dict]:
    """The whole library's artist index, memoised on the catalogue snapshot
    (the union and the catalogue are rebuilt together) — the synchronous
    form, for callers outside getArtists / getIndexes."""
    hit = cat.memo.get("artist_index")
    if hit is not None and hit[0] is union:
        return hit[1]
    indexed = _build_artist_index(cat, union)
    cat.memo["artist_index"] = (union, indexed, _index_fingerprint(indexed))
    return indexed


async def _artist_index_async(store, cat: _sx.Catalogue, union: _sx.ArtistUnion,
                              root_hash: str | None) -> tuple[list[dict], str]:
    """``(index, fingerprint)`` for the whole library or one music folder,
    memoised on the snapshot.  A miss builds both in a worker thread (the
    index sort and the sha1 were 60-180 ms on the loop at 24k artists); the
    folder's artist set is read from the store on the loop first.  The memo
    value carries the union it was built from (identity-checked)."""
    key = ("artist_index", root_hash) if root_hash else "artist_index"
    hit = cat.memo.get(key)
    if hit is not None and hit[0] is union:
        return hit[1], hit[2]
    await _prepare_folder_view(store, cat, root_hash)
    only = _folder_view(store, cat, root_hash)[1] if root_hash else None

    def _work() -> tuple[list[dict], str]:
        idx = _build_artist_index(cat, union, only)
        return idx, _index_fingerprint(idx)
    indexed, fp = await asyncio.to_thread(_work)
    cur = cat.memo.get(key)
    if cur is not None and cur[0] is union:
        return cur[1], cur[2]                    # a concurrent build landed first
    cat.memo[key] = (union, indexed, fp)
    return indexed, fp


def _index_last_modified(cat: _sx.Catalogue, fp: str,
                         root_hash: str | None = None) -> tuple[int, bool]:
    """``(lastModified ms, needs-save)`` for this snapshot's artist index
    (whole library, or one music folder's).

    Derived from the index CONTENT (``_index_fingerprint``: every id / name /
    albumCount), not from when the snapshot happened to be rebuilt: a play
    count, a rating or a tag edit that leaves the artist list unchanged keeps
    the same value, and so does a restart (the stamp is persisted with the
    Subsonic state) — a client polling with ``ifModifiedSince`` refetches only
    when the index really changed."""
    key = ("index_lm", root_hash or "")
    hit = cat.memo.get(key)
    if hit is None:
        ms, changed = _get_state().index_stamp(fp, root_hash or "")
        cat.memo[key] = (ms, changed)
        return ms, changed
    return hit[0], False


def _accepts_gzip(request: Request) -> bool:
    # Same test Starlette's GZipMiddleware applies.
    return "gzip" in (request.headers.get("accept-encoding") or "")


# Bodies at least this big get a memoised gzip copy (the GZip middleware's
# own ``minimum_size``).
_GZIP_MIN_BYTES = 1000


# Stand-in for the list spliced into a separately encoded JSON envelope.
_SPLICE = "__sb_splice_list__"


def _json_spliced(payload: dict, root_key: str, list_key: str) -> bytes:
    """``_ok(payload, fmt="json").body`` byte for byte, encoded piece by
    piece: the envelope with ``payload[root_key][list_key]`` swapped for a
    marker, and each list item on its own, then spliced.  One ``json.dumps``
    of a 2.4 MB artist index is a ~20 ms C call that holds the GIL — in a
    worker thread it still stalls the event loop for all of it; per item the
    loop gets the GIL back between calls.  Same encoder settings as
    Starlette's ``JSONResponse.render``."""
    import json as _json
    inner = payload[root_key]
    items = inner[list_key]
    head = _ok({**payload, root_key: {**inner, list_key: _SPLICE}}, fmt="json").body
    enc = functools.partial(_json.dumps, ensure_ascii=False, allow_nan=False,
                            indent=None, separators=(",", ":"))
    body = b"[" + b",".join(enc(it).encode("utf-8") for it in items) + b"]"
    return head.replace(enc(_SPLICE).encode("utf-8"), body, 1)


async def _cached_render(cat: _sx.Catalogue, slot: tuple, tag: Any, fmt: str, build,
                         *, gzip_ok: bool = False,
                         splice: tuple[str, str] | None = None) -> Response:
    """Serialise a payload once and reuse it while ``tag`` is unchanged.  A
    20-40k-artist index costs ~15 ms to JSON-encode and 75-130 ms to
    XML-encode — per request, for a body that only changes with the library
    or the caller's stars.  One memo SLOT per ``slot`` + format holds the
    latest ``[tag, body, media type, gzip body]``; a new tag replaces it, so
    the memo is bounded (shared slot + one per user) rather than one entry per
    star change.  A miss is built AND serialised in a worker thread —
    ``build`` must only read the immutable snapshot (the memoised index, or
    the per-user copy ``_with_user_artists`` makes); a slot another request
    refreshed meanwhile is left alone.

    ``gzip_ok`` (the client accepts gzip): the compressed body is memoised
    too — made once, off the event loop, on the first gzip request — and sent
    with ``Content-Encoding: gzip``, which the GZip middleware passes through.
    Re-compressing the ~2.4 MB index was ~35 ms of loop time per request.

    ``splice`` (``(root key, list key)``): a JSON body is encoded per list
    item (``_json_spliced``) so the worker never holds the GIL for the whole
    body — the same bytes."""
    f = (fmt or "xml").lower()
    is_json = f in ("json", "jsonp")
    mkey = ("render", slot, "json" if is_json else "xml")
    hit = cat.memo.get(mkey)
    if hit is None or hit[0] != tag:
        if is_json and splice is not None:
            body_ = await asyncio.to_thread(lambda: _json_spliced(build(), *splice))
            media_ = "application/json"
        else:
            resp = await asyncio.to_thread(lambda: _ok(build(), fmt=f))
            body_, media_ = resp.body, resp.media_type
        cur = cat.memo.get(mkey)
        if cur is not None and cur[0] == tag:
            hit = cur                            # a concurrent render landed first
        else:
            cat.memo[mkey] = hit = [tag, body_, media_, None]
    body = hit[1]
    if gzip_ok and f != "jsonp" and len(body) >= _GZIP_MIN_BYTES:
        gz = hit[3]
        if gz is None:
            import gzip as _gzip
            gz = await asyncio.to_thread(_gzip.compress, body, 6)
            if cat.memo.get(mkey) is hit:         # still the current body
                hit[3] = gz
        return Response(content=gz, media_type=hit[2],
                        headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    # (Identity bodies get ``Vary`` from the GZip middleware itself.)
    return Response(content=body, media_type=hit[2])


def _with_user_artists(indexed: list[dict], starred: dict, ratings: dict) -> list[dict]:
    """Overlay the caller's starred timestamps / ratings on the shared cached
    index — copying only the buckets / rows that change, never the whole
    list."""
    if not starred and not ratings:
        return indexed
    out = []
    for bucket in indexed:
        rows = bucket["artist"]
        if any(r["id"] in starred or r["id"] in ratings for r in rows):
            new_rows = []
            for r in rows:
                rid = r["id"]
                if rid in starred or rid in ratings:
                    r = dict(r)
                    if rid in starred:
                        r["starred"] = _iso(starred[rid])
                    if ratings.get(rid):
                        r["userRating"] = ratings[rid]
                new_rows.append(r)
            bucket = {"name": bucket["name"], "artist": new_rows}
        out.append(bucket)
    return out


@_route("/getArtists")
@_route("/getArtists.view")
@_route("/getIndexes")
@_route("/getIndexes.view")
@_wrap
async def get_artists(
    request: Request,
    musicFolderId: str | None = None,
    ifModifiedSince: int | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    # ``musicFolderId``: one scan root's artists (see ``_music_folders``).
    root = _folder_hash(store, musicFolderId)
    cat = _catalogue(store)
    union = cat.union
    uid = getattr(user, "id", None)
    state = _get_state()
    starred = state.starred(uid, "artist")
    rated = state.ratings(uid, "artist")
    indexed, fp = await _artist_index_async(store, cat, union, root)
    gzip_ok = _accepts_gzip(request)
    is_indexes = request.url.path.endswith(("getIndexes", "getIndexes.view"))
    if is_indexes:
        # ``lastModified``: the artist index's content stamp (stable across
        # rebuilds and restarts while the index is unchanged), or the caller's
        # last star change — never the request time, which made every poll
        # look like a change.
        stamp, changed = _index_last_modified(cat, fp, root)
        if changed:
            await state.save()
        # Only ARTIST stars show in the index, so only they move it.
        last_modified = max(stamp, state.starred_changed_ms(uid, "artist"))
        if ifModifiedSince is not None and ifModifiedSince >= last_modified:
            # Spec: unchanged since the client's copy → empty indexes.
            return _ok({"indexes": {
                "lastModified": last_modified,
                "ignoredArticles": _IGNORED_ARTICLES,
            }}, fmt=f)

        def _payload(idx=indexed):
            return {"indexes": {"lastModified": last_modified,
                                "ignoredArticles": _IGNORED_ARTICLES, "index": idx}}
    else:
        def _payload(idx=indexed):
            return {"artists": {"ignoredArticles": _IGNORED_ARTICLES, "index": idx}}
    kind = "indexes" if is_indexes else "artists"
    lm = last_modified if is_indexes else 0
    if not starred and not rated and (not is_indexes or last_modified == stamp):
        # The common case — no per-user overlay: ONE shared serialised body
        # per snapshot, music folder and format.
        return await _cached_render(cat, (kind, None, root), lm, f, _payload, gzip_ok=gzip_ok,
                                    splice=(kind, "index"))
    # A per-user body (starred / rated overlay, or a lastModified raised by
    # the user's own artist-star change): one slot per user (and folder),
    # replaced when stale — the memo stays bounded by users × folders ×
    # formats however often they star or rate.
    tag = (lm, state.starred_changed_ms(uid, "artist"), state.ratings_changed_ms(uid, "artist"))
    # Copies: the overlay is built in a worker thread, the live state dicts
    # may change on the loop meanwhile.
    st_copy, rt_copy = dict(starred), dict(rated)
    return await _cached_render(cat, (kind, uid, root), tag, f,
                                lambda: _payload(_with_user_artists(indexed, st_copy, rt_copy)),
                                gzip_ok=gzip_ok, splice=(kind, "index"))


@_route("/getArtist")
@_route("/getArtist.view")
@_wrap
async def get_artist(
    request: Request,
    id: str = Query(..., description="Artist ID (ar:hash)"),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    hit = _decode_artist(id, store)
    if hit is None:
        raise _SubsonicError(70, "Artist not found.")
    low, name = hit
    # Own albums (album-artist or track-artist-owned, plus — folder albums
    # on — the folders they own at least half of and their own shares of
    # mixed folders), else the albums the artist appears on —
    # pre-grouped and pre-sorted (year, name) in the
    # catalogue, so this is O(albums of the artist), no library scan.
    cat = _catalogue(store)
    uid = getattr(user, "id", None)
    state = _get_state()
    starred_albums = state.starred(uid, "album")
    rated_albums = state.ratings(uid, "album")
    albums = [_album_id3(e, starred_albums, rated_albums) for e in cat.albums_for_artist(low)]
    out = {
        "id":         id,
        "name":       name,
        "albumCount": len(albums),
        "coverArt":   id,
        "album":      albums,
    }
    st = state.starred(uid, "artist").get(out["id"])
    if st:
        out["starred"] = _iso(st)
    r = state.ratings(uid, "artist").get(out["id"])
    if r:
        out["userRating"] = r
    return _ok({"artist": out}, fmt=f)


@_route("/getAlbum")
@_route("/getAlbum.view")
@_wrap
async def get_album(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    album, tracks = _album_and_tracks(store, id)
    if album is None:
        raise _SubsonicError(70, "Album not found.")
    ctx = _song_ctx(store, user, album=((album["id"], album["name"])
                                        if id.startswith(_sx.FOLDER_PREFIX) else None))
    songs = await _songs_with_tunes(tracks, ctx)
    # The header's songCount / duration come from ``_album_and_tracks`` —
    # the catalogue's own rules (every listed tune counted; the raw durations
    # summed — non-finite / corrupt values skipped — then rounded once), so
    # getAlbum, getMusicDirectory and every list view agree.  songCount
    # equals ``len(songs)`` by construction.
    album["song"] = songs
    uid = getattr(user, "id", None)
    st = _get_state().starred(uid, "album").get(id)
    if st:
        album["starred"] = _iso(st)
    r = _get_state().ratings(uid, "album").get(id)
    if r:
        album["userRating"] = r
    return await _ok_async({"album": album}, fmt=f, n_hint=len(songs), splice=("album", "song"))


# Folder / group albums at least this big keep their header and member ids in
# the catalogue snapshot's memo (``_album_and_tracks``): a 17k-song group share
# cost ~35 ms of header work on every getAlbum / getMusicDirectory / next-track
# lookup.  Smaller ones are cheap to rebuild, and a client syncing every album
# must not copy the whole library's membership into the memo.
_ALBUM_MEMO_MIN = 1000


def _album_and_tracks(store, raw: str) -> tuple[dict | None, list[dict]]:
    """Resolve any album id (``al:`` or ``fa:``) to ``(AlbumID3 header without
    songs, tracks in play order)``, or ``(None, [])``.  Shared by getAlbum and
    getMusicDirectory (and the album-seeded similar songs) so all agree on
    membership.  The header is the caller's own copy (it may add keys).  A
    big folder / group album (≥ ``_ALBUM_MEMO_MIN`` tracks) is memoised per
    catalogue snapshot — as fresh as the snapshot's own entries."""
    ref = _sx.parse_folder_album_ref(raw)
    gref = None if ref else _sx.parse_group_ref(raw)
    if ref or gref:
        cat = _catalogue(store)
        mkey = ("album_tracks", raw)
        hit = cat.memo.get(mkey)
        if hit is not None:
            get = store.get_track
            tracks = [t for t in map(get, hit[1]) if t is not None]
            return (dict(hit[0]), tracks) if tracks else (None, [])
        if ref:
            dh, okey = ref
            tracks = _sx.folder_album_tracks(store, dh, okey)
        else:
            dh, okey = gref
            tracks = _sx.group_album_tracks(store, cat, dh, okey)
        e = _sx.folder_entry_from_tracks(store, cat.union, dh, tracks, okey,
                                         group=gref is not None)
        if e is None:
            return None, []
        listed = cat.by_id.get(raw)
        if listed is not None:
            e.name = listed.name      # the lists' name (a same-name qualifier included)
        album = _album_id3(e)
        if len(tracks) >= _ALBUM_MEMO_MIN:
            cat.memo[mkey] = (album, tuple(t["id"] for t in tracks))
            album = dict(album)
        return album, tracks
    aa, al = _decode_album_id(raw, store)
    if not al:
        return None, []
    tracks = _album_tracks(store, aa or "", al)
    if not tracks:
        return None, []
    # Header sample / counts: the catalogue's rules (``_sx.sample_key``, every
    # listed tune counted), so list views and getAlbum agree.
    sample = max(tracks, key=_sx.sample_key)
    album: dict = {
        "id":        raw,
        "name":      al,
        "artist":    aa or _sx.UNKNOWN_ARTIST,
        "artistId":  _artist_id(aa or ""),
        "songCount": sum(_sx.tune_count(x) for x in tracks),
        "duration":  _sx._finite_int(sum(_sx.tune_duration_sum(x) for x in tracks)),
    }
    y = _normalise_year(sample.get("year"))
    if y:                         # optional: an unknown year is omitted, not 0
        album["year"] = y
    # ``created`` is required on AlbumID3 — but "" is not an xs:dateTime:
    # the newest track's added time, else its file mtime, else omitted.
    created = _iso(sample.get("added_at")) or _iso(_ts_num(sample.get("mtime")))
    if created:
        album["created"] = created
    if sample.get("id"):
        album["coverArt"] = sample["id"]
    g = (sample.get("genre") or [""])[0]
    if g:
        album["genre"] = g
    return album, tracks


@_route("/getSong")
@_route("/getSong.view")
@_wrap
async def get_song(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    tid, sub = _split_id(store, id)
    track = store.get_track(tid)
    if not track:
        raise _SubsonicError(70, "Song not found.")
    ctx = _song_ctx(store, user)
    song = _track_to_song(track, ctx)
    return _ok({"song": _tune_child(song, track, sub, ctx)}, fmt=f)


# The list types the spec defines; anything else is an error, not a list.
_ALBUM_LIST_TYPES = frozenset({"newest", "recent", "frequent", "highest", "alphabeticalByName",
                               "alphabeticalByArtist", "random", "byYear", "byGenre",
                               "starred"})


@_route("/getAlbumList")
@_route("/getAlbumList.view")
@_route("/getAlbumList2")
@_route("/getAlbumList2.view")
@_wrap
async def get_album_list(
    request: Request,
    type: str = Query("newest", description="newest|recent|frequent|highest|alphabeticalByName|alphabeticalByArtist|random|byYear|byGenre|starred"),
    size: int = Query(10, ge=1),
    offset: int = Query(0, ge=0),
    fromYear: int | None = Query(default=None),
    toYear:   int | None = Query(default=None),
    genre:    str | None = Query(default=None),
    musicFolderId: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    if type not in _ALBUM_LIST_TYPES:
        raise _SubsonicError(0, f"Type '{type}' is not supported.")
    store = get_store()
    size = min(size, 500)                     # original Subsonic's cap: clamp, never an error
    # Every list type reads the memoised catalogue (one O(N) pass per library
    # mutation, 5 s debounce during a scan); the deterministic orders are
    # memoised on the snapshot too, so the common carousel poll is an O(size)
    # slice.  Folder albums are included when the setting is on.
    # ``musicFolderId``: only albums with a track in that scan root.
    root = _folder_hash(store, musicFolderId)
    cat = _catalogue(store)
    uid = getattr(user, "id", None)
    starred = _get_state().starred(uid, "album")
    rated = _get_state().ratings(uid, "album")
    is_v2 = request.url.path.endswith(("getAlbumList2", "getAlbumList2.view"))
    # Once-per-snapshot work (a 60k-album sort, a root's album set) is done
    # off the loop / in steps before the O(size) slice below.
    await _prepare_folder_view(store, cat, root)

    if type == "byGenre":
        if not genre:
            raise _SubsonicError(10, "Required parameter 'genre' is missing.")
        # Membership, not one sample track's tag: an album is in a genre when
        # ANY of its tracks carries it.  Name-sorted once per snapshot.
        g_l = genre.strip().lower()
        await _prepare_genre(cat, g_l)
        base = cat.genre_sorted(g_l)
        if base:                       # (an unknown genre is never memoised)
            base = _in_folder(cat, ("genre", g_l), base, root, store)
    elif type == "starred":
        base = [e for e in (_resolve_album_entry(store, i, cat)
                            for i, _ts in sorted(starred.items(), key=lambda kv: -_ts_num(kv[1])))
                if e is not None]
        if root:
            ids = _folder_view(store, cat, root)[0]
            base = [e for e in base if e.id in ids]
    elif type == "highest" and any(rated.values()):
        # The caller's own album ratings first (best first), then the albums
        # ranked by their tracks' mean rating — each album once.  Nothing is
        # added for a user without album ratings (the shared memo below).
        mine = [(r, e) for i, r in rated.items() if r
                for e in (_resolve_album_entry(store, i, cat),) if e is not None]
        mine.sort(key=lambda x: (-x[0], x[1].name.lower(), x[1].id))
        seen = {e.id for _r, e in mine}
        base = [e for _r, e in mine] + [e for e in _ranked_albums(store, cat, "highest")
                                        if e.id not in seen]
        if root:
            ids = _folder_view(store, cat, root)[0]
            base = [e for e in base if e.id in ids]
    elif type in ("frequent", "recent", "highest"):
        base = _ranked_albums(store, cat, type)
        if root:
            ids = _folder_view(store, cat, root)[0]
            base = [e for e in base if e.id in ids]
    elif type == "random":
        base = _in_folder(cat, "entries", cat.entries, root, store)
        if not root:
            base = _playable_albums(cat, base, _dead_root_hashes(store))
    else:                    # newest / alphabeticalByName / alphabeticalByArtist / byYear
        await _prepare_sorted(cat, type)
        base = _in_folder(cat, ("sorted", type), _sorted_entries(cat, type), root, store)

    has_filter = (bool(genre) and type != "byGenre") or fromYear is not None or toYear is not None
    if has_filter:
        gset = _genre_ids(cat, genre) if (genre and type != "byGenre") else None
        lo = fromYear if fromYear is not None else -9999
        hi = toYear if toYear is not None else 9999
        if lo > hi:
            lo, hi = hi, lo
        base = [e for e in base
                if (gset is None or e.id in gset) and lo <= (e.year or 0) <= hi]
        # Spec: byYear with fromYear > toYear lists newest year first.
        if type == "byYear" and fromYear is not None and toYear is not None and toYear < fromYear:
            base = base[::-1]

    if type == "random":
        import random
        n = len(base)
        picks = random.sample(range(n), min(size, n)) if n else []
        sliced = [base[i] for i in picks]
    else:
        sliced = base[offset: offset + size]
    mapper = _album_id3 if is_v2 else _album_child
    out = [mapper(e, starred, rated) for e in sliced]
    key = "albumList2" if is_v2 else "albumList"
    return await _ok_async({key: {"album": out}}, fmt=f, n_hint=len(out))


# Run length for ``_sort_runs``: each run's sort is one C call holding the
# GIL (~1.5 ms at this size); between runs a worker thread lets the event
# loop in.  One ``list.sort`` of 63k entries held it for 15-37 ms.
_SORT_RUN = 4000


def _sort_runs(items: list, key, chunk: int = _SORT_RUN) -> list:
    """``sorted(items, key=key)`` as sorted runs of ``chunk`` merged with
    ``heapq.merge`` — the same order (both are stable; ties keep run order,
    i.e. input order) but no single C call holds the GIL for the whole sort.
    For worker threads; small lists sort in one go."""
    if len(items) <= chunk:
        return sorted(items, key=key)
    import heapq
    runs = [sorted(items[i:i + chunk], key=key) for i in range(0, len(items), chunk)]
    return list(heapq.merge(*runs, key=key))


def _sort_entries(entries: list, sort_type: str) -> list[_sx.AlbumEntry]:
    """A new list of ``entries`` in a deterministic order.  Pure (reads only
    the immutable snapshot's entries), so it may run in a worker thread —
    in runs (``_sort_runs``) so the thread never holds the GIL long."""
    if sort_type == "alphabeticalByName":
        key = lambda e: (e.name.lower(), e.artist.lower(), e.id)       # noqa: E731
    elif sort_type == "alphabeticalByArtist":
        key = lambda e: (e.artist.lower(), e.name.lower(), e.id)       # noqa: E731
    elif sort_type == "byYear":
        key = lambda e: (e.year or 0, e.name.lower(), e.id)            # noqa: E731
    elif sort_type == "genre":                       # one genre's list (byGenre)
        key = lambda e: (e.name.lower(), e.id)                         # noqa: E731
    else:                                            # newest
        key = lambda e: (-(e.added or 0), e.id)                        # noqa: E731
    return _sort_runs(list(entries), key)


def _sorted_entries(cat: _sx.Catalogue, sort_type: str) -> list[_sx.AlbumEntry]:
    """Catalogue entries in a deterministic order, memoised on the snapshot.
    Returns the cached list — callers slice / comprehend, never mutate."""
    key = ("sorted", sort_type)
    hit = cat.memo.get(key)
    if hit is None:
        hit = cat.memo[key] = _sort_entries(cat.entries, sort_type)
    return hit


# A sort of this many entries runs in a worker thread (25-35 ms per order at
# 61k albums on the event loop otherwise — once per snapshot, but stalling).
_SORT_OFFLOAD_MIN = 5000


async def _prepare_sorted(cat: _sx.Catalogue, sort_type: str) -> None:
    """Fill the ``_sorted_entries`` memo off the event loop when it's big."""
    key = ("sorted", sort_type)
    if key in cat.memo or len(cat.entries) < _SORT_OFFLOAD_MIN:
        return
    items = await asyncio.to_thread(_sort_entries, cat.entries, sort_type)
    cat.memo.setdefault(key, items)


async def _prepare_genre(cat: _sx.Catalogue, genre_l: str) -> None:
    """Fill ``Catalogue.genre_sorted``'s memo off the loop for a big genre."""
    key = ("genre_sorted", genre_l)
    lst = cat.genre_albums.get(genre_l)
    if key in cat.memo or not lst or len(lst) < _SORT_OFFLOAD_MIN:
        return
    items = await asyncio.to_thread(_sort_entries, lst, "genre")
    cat.memo.setdefault(key, items)


def _genre_ids(cat: _sx.Catalogue, genre: str) -> set[str]:
    g = (genre or "").strip().lower()
    if g not in cat.genre_albums:
        return set()             # never memoise misses (client-supplied strings)
    key = ("genre_ids", g)
    hit = cat.memo.get(key)
    if hit is None:
        hit = cat.memo[key] = {e.id for e in cat.genre_albums[g]}
    return hit


def _ranked_albums(store, cat: _sx.Catalogue, kind: str) -> list[_sx.AlbumEntry]:
    """``frequent`` (total plays), ``recent`` (last played) and ``highest``
    (mean track rating — the shared fallback getAlbumList puts after the
    caller's own album ratings) album lists.  One pass over the PLAYED / RATED tracks
    only — not the library — memoised on the snapshot + the store's play /
    rating sequence, which bump on every play / rating without touching
    ``_mutation_seq``."""
    seq = getattr(store, "_rating_seq" if kind == "highest" else "_play_seq", 0)
    key = ("ranked", kind, seq)
    hit = cat.memo.get(key)
    if hit is not None:
        return hit
    for k in [k for k in cat.memo if isinstance(k, tuple) and k[:2] == ("ranked", kind)]:
        del cat.memo[k]                              # drop superseded rankings
    agg: dict[str, list] = {}
    if kind == "highest":
        for tid, r in store._ratings.items():
            t = store.get_track(tid)
            e = cat.track_entry(t) if t else None
            if e is None or not r:
                continue
            a = agg.setdefault(e.id, [e, 0, 0])
            a[1] += r
            a[2] += 1
        ranked = sorted(agg.values(), key=lambda a: (-(a[1] / a[2]), -a[2], a[0].name.lower()))
    else:
        field = "count" if kind == "frequent" else "last_played"
        for tid, st in store._play_stats.items():
            t = store.get_track(tid)
            e = cat.track_entry(t) if t else None
            if e is None:
                continue
            v = st.get(field) or 0
            a = agg.setdefault(e.id, [e, 0])
            a[1] = a[1] + v if kind == "frequent" else max(a[1], v)
        ranked = sorted(agg.values(), key=lambda a: (-a[1], a[0].name.lower()))
    out = [a[0] for a in ranked]
    cat.memo[key] = out
    return out


@_route("/search3")
@_route("/search3.view")
@_route("/search2")
@_route("/search2.view")
@_wrap
async def search3(
    request: Request,
    query: str = Query("", alias="query"),
    artistCount: int = Query(20, ge=0),
    albumCount:  int = Query(20, ge=0),
    songCount:   int = Query(20, ge=0),
    artistOffset: int = Query(0, ge=0),
    albumOffset:  int = Query(0, ge=0),
    songOffset:   int = Query(0, ge=0),
    musicFolderId: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    # Page sizes are clamped, never an error (a syncing client asking for a
    # bigger page than the cap just pages more often).
    artistCount = min(artistCount, _SEARCH_MAX)
    albumCount = min(albumCount, _SEARCH_MAX)
    songCount = min(songCount, _SEARCH_MAX)
    root = _folder_hash(store, musicFolderId)
    q = (query or "").strip()
    if q in ('""', "''", "*"):
        q = ""                     # Symfonium / DSub "match everything" sync query
    q = q.lower()
    # Field operators (``game:uridium``, ``artist:"rob hubbard" year:>1986``)
    # — the web search box's syntax, parsed by the same code: songs by the
    # predicates, artists / albums by the free-text remainder only (an
    # operator-only query matches no artists / albums, never all of them).
    adv_kw: dict | None = None
    if q and ":" in q:
        from soniqboom.api.search import _parse_advanced_query
        adv = _parse_advanced_query(q)
        if adv:
            from soniqboom.core.data import _parse_tag_query
            adv_kw = _parse_tag_query(adv)
    text = (adv_kw.get("query") or "").lower() if adv_kw is not None else q
    ctx = _song_ctx(store, user)
    if adv_kw is not None:
        kw = dict(adv_kw)
        if root:
            kw["scan_root_hash"] = root
        tracks = store.filter_tracks(**kw, limit=songCount, offset=songOffset) if songCount else []
    else:
        # Songs: the store's own offset/limit — an empty-query full-library
        # sync pages in O(page) off the sorted index instead of materialising
        # and discarding ``songOffset`` rows per page (was 435 ms at offset
        # 200k).  A query with no word characters ("!!!", "&") tokenises to
        # nothing, which the store reads as "no filter" (= every song) —
        # match no songs instead; artist / album name matching still applies.
        word_q = not q or any(ch.isalnum() for ch in q)
        if songCount and word_q:
            tracks = (store.filter_tracks(query=q, limit=songCount, offset=songOffset,
                                          scan_root_hash=root)
                      if root else store.filter_tracks(query=q, limit=songCount, offset=songOffset))
        else:
            tracks = []
    songs = [_track_to_song(t_, ctx) for t_ in tracks]
    # Artists / albums: accent-insensitive substring match ("oorni" finds
    # "Lasse Öörni") over name-sorted lists memoised per catalogue snapshot
    # with their folded names, stopping once the requested page is full.
    fq = _fold(text)
    no_match = adv_kw is not None and not fq
    # Word-wise, like song search: every word of the query must occur in the
    # name, in any order — "hubbard rob", "star wars empire", '"rob hubbard"'
    # and "hubbard*" all find their artist / album (the store's separators
    # drop quotes, ``*`` and punctuation).  A query with no word characters
    # ("!!!") keeps the literal substring match.
    words = [w for w in _SPLIT_RE.split(fq) if w] if fq else []
    if len(words) > 1:
        def _name_match(n: str, _ws=tuple(words)) -> bool:
            for w in _ws:
                if w not in n:
                    return False
            return True
    else:
        _needle = words[0] if words else fq
        def _name_match(n: str, _w=_needle) -> bool:
            return _w in n
    cat = _catalogue(store)
    union = _artist_union(store)
    uid = getattr(user, "id", None)
    artists_out = []
    if root and (artistCount or albumCount):
        await _prepare_folder_view(store, cat, root)
    fview = _folder_view(store, cat, root) if root and (artistCount or albumCount) else None
    if artistCount and not no_match:
        artists_sorted, folded = await _search_artists(cat, union)
        rows = (kv for kv, fk in zip(artists_sorted, folded)
                if (not fq or _name_match(fk)) and (fview is None or kv[0] in fview[1]))
        st_art = _get_state().starred(uid, "artist")
        rt_art = _get_state().ratings(uid, "artist")
        for low, name in islice(rows, artistOffset, artistOffset + artistCount):
            aid = union.artist_id(low)
            row = {"id": aid, "name": name,
                   "albumCount": cat.album_count(low), "coverArt": aid}
            if row["id"] in st_art:
                row["starred"] = _iso(st_art[row["id"]])
            if rt_art.get(aid):
                row["userRating"] = rt_art[aid]
            artists_out.append(row)
    # Albums: the name-sorted catalogue (incl. folder albums when enabled) —
    # O(albums) at worst, no track walk, no per-request sort or lower().
    albums_out = []
    if albumCount and not no_match:
        await _prepare_sorted(cat, "alphabeticalByName")
        by_name = _in_folder(cat, ("sorted", "alphabeticalByName"),
                             _sorted_entries(cat, "alphabeticalByName"), root, store)
        if not fq:
            matched_al = by_name[albumOffset:albumOffset + albumCount]
        else:
            names = await _folded_names(cat, by_name, root)
            matched_al = list(islice((e for e, n in zip(by_name, names) if _name_match(n)),
                                     albumOffset, albumOffset + albumCount))
        st_al = _get_state().starred(uid, "album")
        rt_al = _get_state().ratings(uid, "album")
        is_v3_albums = "search3" in request.url.path
        mapper = _album_id3 if is_v3_albums else _album_child
        albums_out = [mapper(e, st_al, rt_al) for e in matched_al]

    is_v3 = "search3" in request.url.path
    key = "searchResult3" if is_v3 else "searchResult2"
    return await _ok_async({
        key: {
            "artist": artists_out,
            "album":  albums_out,
            "song":   songs,
        },
    }, fmt=f, n_hint=len(songs) + len(albums_out) + len(artists_out))


# search2 / search3 page-size ceiling (clamped, not an error).  Above what
# syncing clients request per page; bounds one response's mapping work.
_SEARCH_MAX = 2000


def _sort_fold_artists(items: list) -> tuple[list, list[str]]:
    """``(name-sorted (key, display) pairs, their folded keys)``.  Pure — runs
    in a worker thread for a big union (the ArtistUnion is an immutable
    snapshot and ``items`` a copy)."""
    srt = _sort_runs(items, lambda kv: (kv[1].lower(), kv[0]))
    return srt, [_fold(kv[0]) for kv in srt]


async def _search_artists(cat: _sx.Catalogue, union: _sx.ArtistUnion) -> tuple[list, list[str]]:
    """search3's artist list and its folded names, memoised per snapshot
    (keyed on the union's identity); built off the loop when big."""
    hit = cat.memo.get("artists_sorted")
    if hit is not None and hit[0] is union:
        return hit[1], hit[2]
    items = list(union.display.items())               # snapshot on the loop
    if len(items) >= _SORT_OFFLOAD_MIN:
        srt, folded = await asyncio.to_thread(_sort_fold_artists, items)
    else:
        srt, folded = _sort_fold_artists(items)
    hit = cat.memo.get("artists_sorted")
    if hit is not None and hit[0] is union:           # a concurrent fill won
        return hit[1], hit[2]
    cat.memo["artists_sorted"] = (union, srt, folded)
    return srt, folded


async def _folded_names(cat: _sx.Catalogue, by_name: list, root: str | None) -> list[str]:
    """Folded names of an album list (``_fold``), memoised next to it per
    snapshot and music folder — paired by the list's identity, so a rebuilt
    ``_in_folder`` list never meets stale names.  Built off the loop when
    big (the entries are the snapshot's and their names immutable)."""
    key = ("names_folded", "alphabeticalByName", root or "")
    hit = cat.memo.get(key)
    if hit is not None and hit[0] is by_name:
        return hit[1]
    if len(by_name) >= _SORT_OFFLOAD_MIN:
        names = await asyncio.to_thread(lambda: [_fold(e.name) for e in by_name])
    else:
        names = [_fold(e.name) for e in by_name]
    cat.memo[key] = (by_name, names)
    return names


# Cached snapshot of ``list(store._tracks.keys())`` for ``getRandomSongs``.
# Rebuilding the list each call is a 170K-entry copy + full O(N) shuffle —
# on a busy library, ``random.shuffle(all_track_metas())`` was the single
# largest CPU consumer for Subsonic clients on shuffle play.  We sample
# without materialising metadata until *after* the selection.
_RANDOM_KEYS_CACHE: dict = {"seq": None, "keys": [], "roots": {}, "live": {}}


def _dead_root_hashes(store) -> frozenset:
    """Scan roots whose source is known to be unreachable right now: a root
    marked ``unavailable`` (an ejected drive, a dropped mount, a share the
    monitor lost) or a network share with no connected source — the stored-
    state test ``tracks._source_unreachable`` makes per track, no I/O.
    O(#roots) per call."""
    try:
        sds = getattr(store, "_scan_dirs", None)
        if not isinstance(sds, dict) or not sds:
            return frozenset()
        from soniqboom.api.tracks import _REMOTE_PREFIXES
        get_source = None
        dead = set()
        for path, sd in sds.items():
            if not isinstance(sd, dict) or not isinstance(path, str):
                continue
            ph = sd.get("path_hash") or hashlib.sha256(path.encode()).hexdigest()[:16]
            if sd.get("status") == "unavailable":
                dead.add(ph)
            elif path.startswith(_REMOTE_PREFIXES):
                if get_source is None:
                    from soniqboom.core.filesource import get_source
                if get_source(path) is None:     # a remote root's path IS its source key
                    dead.add(ph)
        return frozenset(dead)
    except Exception:                  # noqa: BLE001 — a probe must never fail a request
        return frozenset()


def _random_track_keys(store, root_hash: str | None = None,
                       dead: frozenset = frozenset()) -> list[str]:
    """The ids ``getRandomSongs`` samples from: the whole library, or one
    scan root's (``root_hash``), memoised per catalogue sequence.  With
    ``dead`` roots (``_dead_root_hashes``) and no folder filter, only the
    reachable roots' tracks — a random mix must not queue songs that can't
    play (every one an error in the client).  Tracks that belong to no scan
    root are left out of that list; if nothing reachable remains, the whole
    library (the endpoint still answers)."""
    seq = _catalog_seq(store)
    c = _RANDOM_KEYS_CACHE
    if c["seq"] != seq or c.get("store") is not store:
        c["keys"] = list(store._tracks.keys())
        c["roots"] = {}
        c["live"] = {}
        c["seq"] = seq
        c["store"] = store
    if not root_hash:
        if not dead:
            return c["keys"]
        hit = c["live"].get(dead)
        if hit is None:
            hit = []
            for sd in list((getattr(store, "_scan_dirs", None) or {}).values()):
                h = sd.get("path_hash") if isinstance(sd, dict) else None
                if h and h not in dead:
                    hit.extend(_root_track_ids(store, h))
            if not hit:
                hit = c["keys"]
            if len(c["live"]) >= 4:
                c["live"].clear()
            c["live"][dead] = hit
        return hit
    roots = c["roots"]
    hit = roots.get(root_hash)
    if hit is None:
        hit = roots[root_hash] = list(_root_track_ids(store, root_hash))
    return hit


def _sample_playable(store, preds: dict, size: int, dead: frozenset) -> list:
    """Up to ``size`` random ids of the tracks matching ``preds`` (the store's
    ``filter_track_ids`` predicates), skipping tracks in a ``dead`` scan root.
    While most of the library is reachable: an oversampled draw (the same
    candidate list copy ``filter_track_ids`` makes, then O(size) checks).
    Otherwise — or when the draw came up short — the reachable roots' share
    of the candidate set by set intersection (per root O(min(candidates,
    root size)); no per-candidate Python loop).  Every candidate unreachable
    → the unfiltered draw (the endpoint still answers)."""
    import random
    if not dead:
        ids = store.filter_track_ids(**preds)
        return random.sample(ids, min(len(ids), size))
    cand = store._candidate_ids(**preds) or set()
    if not cand:
        return []
    live_roots, live_n, dead_n = [], 0, 0
    for sd in list((getattr(store, "_scan_dirs", None) or {}).values()):
        h = sd.get("path_hash") if isinstance(sd, dict) else None
        if not h:
            continue
        n = len(_root_track_ids(store, h))
        if h in dead:
            dead_n += n
        else:
            live_roots.append(h)
            live_n += n
    ids = None
    if live_n >= dead_n:
        ids = list(cand)
        if len(ids) > size:
            get = store.get_track
            draw = random.sample(ids, min(len(ids), size * 3))
            picks = [i for i in draw if (get(i) or {}).get("scan_root_hash") not in dead]
            if len(picks) >= size:
                return picks[:size]       # uniform among the reachable ones
    live: list = []
    for h in live_roots:
        live.extend(cand & _root_track_ids(store, h))
    if not live:
        ids = ids if ids is not None else list(cand)
        return random.sample(ids, min(len(ids), size))
    return random.sample(live, min(len(live), size))


@_route("/getRandomSongs")
@_route("/getRandomSongs.view")
@_wrap
async def get_random_songs(
    request: Request,
    size: int = Query(10, ge=1),
    fromYear: int | None = Query(default=None),
    toYear:   int | None = Query(default=None),
    genre:    str | None = Query(default=None),
    musicFolderId: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    import random
    store = get_store()
    size = min(size, 500)
    root = _folder_hash(store, musicFolderId)
    ctx = _song_ctx(store, user)
    # Filtered path — resolve eligibility via the maintained _tag_genre ∩
    # _sorted_year (∩ scan-root) indexes (filter_track_ids), then
    # random.sample over just the candidate IDs and materialise only the
    # picks.  Was a full all_track_metas() walk + filter over 270k on the
    # event loop.
    # Random picks skip songs whose source is offline (listings still show
    # them): a folder filter names one root — reachable, or asked for anyway.
    dead = _dead_root_hashes(store) if not root else frozenset()
    if genre or fromYear is not None or toYear is not None:
        preds = {"genre": genre, "year_min": fromYear, "year_max": toYear}
        if root:
            preds["scan_root_hash"] = root
        pick_ids = _sample_playable(store, preds, size, dead)
        picks = [m for m in (store.get_track(i) for i in pick_ids) if m]
        songs = [_track_to_song(t_, ctx) for t_ in picks]
        return await _ok_async({"randomSongs": {"song": songs}}, fmt=f, n_hint=len(songs))

    # Unfiltered: random.sample over the cached key list (one scan root's,
    # with ``musicFolderId``; the reachable roots' while some are offline),
    # materialising metadata only for the chosen IDs (~10 dict copies vs 170K).
    keys = _random_track_keys(store, root, dead)
    if not keys:
        return _ok({"randomSongs": {"song": []}}, fmt=f)
    take = min(size, len(keys))
    picked_ids = random.sample(keys, take)
    metas = store.get_tracks_batch(picked_ids)
    songs = [_track_to_song(t_, ctx) for t_ in metas if t_]
    return await _ok_async({"randomSongs": {"song": songs}}, fmt=f, n_hint=len(songs))


# ── Streaming proxy ──────────────────────────────────────────────────────────

_REMOTE_SCHEMES = ("smb://", "ftp://", "http://", "https://")


@contextlib.contextmanager
def _stream_preauthed():
    """Run ``api/stream.stream_track`` for a caller THIS module already
    authenticated.  It sets the stream module's internal-caller ContextVar
    (the one the cast byte-server uses): ``stream_track`` then skips its own
    credential check — which re-ran the password check (a scrypt, for
    ``p=``) on every stream request, and can't verify an API key at all.  A
    rendered format is served as the complete file (exact Content-Length),
    not the web player's progressive render; a ``format`` / ``maxBitRate``
    transcode that isn't cached yet streams live (chunked, no
    Content-Length, no Range) until the cache has it.  A ContextVar, never
    a parameter: nothing a request carries can set it."""
    from soniqboom.api import stream as _st
    tok = _st._set_cast_internal_bypass(True)
    try:
        yield
    finally:
        _st._reset_cast_internal_bypass(tok)


async def _serve_stream(request: Request, track_id: str, *, target_format: str | None,
                        max_bitrate_kbps: int, target_sample_rate: int,
                        seek: float, subsong: int = 0,
                        estimate_s: float | None = None) -> Response:
    """The internal stream handler for an authenticated Subsonic request
    (``subsong``: the wire tune of a multi-tune file — ``_split_id``'s).
    ``estimate_s`` (``estimateContentLength=true``; the song's length in
    seconds, 0 = unknown): a live MP3 transcode answers with an estimated
    Content-Length and exactly that many bytes (the stream module's
    ``estimated_content_length``).  A stream failure (file gone, source
    offline, render failed) becomes a Subsonic error envelope with a
    client-safe message (``_stream_http_error``); only a 416 (bad Range)
    stays an HTTP error."""
    from soniqboom.api import stream as _st
    stream_track = _st.stream_track
    est = (_st.estimated_content_length(estimate_s) if estimate_s is not None
           else contextlib.nullcontext())
    with _stream_preauthed(), est:
        try:
            resp = await stream_track(
                track_id=track_id,
                request=request,
                seek=float(seek or 0.0),
                subsong=int(subsong or 0),
                file_path=None,
                target_format=(target_format or None),
                max_bitrate_kbps=int(max_bitrate_kbps or 0),
                target_sample_rate=int(target_sample_rate or 0),
                force_transcode=False,
                sb_session=None, u=None, p=None, s=None, t=None,
            )
        except HTTPException as exc:
            if exc.status_code == 416:
                raise
            raise _stream_http_error(exc) from None
    # Behind nginx, stream as it arrives (a render / transcode / relay would
    # otherwise sit in the proxy buffer).
    if "x-accel-buffering" not in resp.headers:
        resp.headers["X-Accel-Buffering"] = "no"
    return resp


def _head_response(media_type: str, length: int | None) -> Response:
    """Headers-only answer to a HEAD probe.  No Content-Length unless it is
    known exactly (a bare Response would claim 0)."""
    resp = Response(status_code=200, media_type=media_type,
                    headers={"Accept-Ranges": "bytes"})
    if length is None:
        del resp.headers["content-length"]
    else:
        resp.headers["content-length"] = str(int(length))
    return resp


async def _local_size(path: str) -> int | None:
    if not path or path.startswith(_REMOTE_SCHEMES) or "::" in path:
        return None
    try:
        return (await asyncio.to_thread(os.stat, path)).st_size
    except OSError:
        return None


def _source_offline(track: dict) -> bool:
    """The track's source is known to be unreachable right now (its share
    isn't connected / its scan root is marked unavailable) — stored state
    only, no I/O (``tracks._source_unreachable``)."""
    try:
        from soniqboom.api.tracks import _source_unreachable
        return bool(_source_unreachable(track.get("path") or ""))
    except Exception:            # noqa: BLE001 — a probe must never fail a request
        return False


async def _stream_head(request: Request, track_id: str, *, target_format: str | None,
                       max_bitrate_kbps: int = 0, target_sample_rate: int = 0,
                       subsong: int = 0) -> Response:
    """HEAD for the stream routes: the stream module's own HEAD answer
    (``stream_track_head``: the type GET would send, a length only when known
    for free — never a render, transcode, extraction or remote fetch).  A
    track whose source is known to be offline answers the same error
    envelope GET would, not a 200 audio probe."""
    from soniqboom.api.stream import stream_track_head
    track = get_store().get_track(track_id)
    if track is not None and _source_offline(track):
        raise _SubsonicError(0, "The music source for this song is offline.", http_status=503)
    with _stream_preauthed():
        return await stream_track_head(
            track_id=track_id, request=request, subsong=int(subsong or 0),
            target_format=(target_format or None),
            max_bitrate_kbps=int(max_bitrate_kbps or 0),
            target_sample_rate=int(target_sample_rate or 0),
            force_transcode=False, sb_session=None, u=None, p=None, s=None, t=None,
        )


# ── Next-track render prewarm ────────────────────────────────────────────────
# The web player warms the render of what plays next; a Subsonic client gets
# the same: when /rest/stream starts a track, the NEXT one — the next entry of
# the caller's saved play queue, else the next song of the playing track's
# album (its tunes included) — is rendered / converted in the background when
# it isn't a native format (SID, tracker, Amiga …; DSD, ALAC …), so the
# client's own prefetch finds it cached instead of waiting out a whole
# render.  One track, the low ``PRIO_AHEAD`` background priority, the web
# endpoint's gates (``render_prewarm`` holds back rendered formats only);
# never for a remote file that isn't in the local cache yet.

_PREWARM_BG: set = set()
# Album id → (the album's (track, tune) play order, position map), memoised
# in the catalogue snapshot's own ``memo`` (it dies with the snapshot — a
# module-level map holding the snapshot pinned every stale catalogue, ~20 MB
# each, until evicted) — a big folder album isn't re-listed on every track
# start.  At most ``_ALBUM_ORDER_MAX`` albums per snapshot.
_ALBUM_ORDER_MAX = 32


def _album_order(store, cat: _sx.Catalogue, album_id: str) -> tuple[list, dict]:
    memo = cat.memo.get("album_order")
    if memo is None:
        memo = cat.memo["album_order"] = {}
    hit = memo.get(album_id)
    if hit is not None:
        return hit
    _alb, tracks = _album_and_tracks(store, album_id)
    order = []
    for t in tracks:
        order.append((t["id"], 0))
        order.extend((t["id"], n) for n in _sx.listed_tunes(t))
    pos = {k: i for i, k in enumerate(order)}
    if len(memo) >= _ALBUM_ORDER_MAX:
        memo.pop(next(iter(memo)))
    memo[album_id] = (order, pos)
    return order, pos


def _next_tune(store, user, raw_id: str, tid: str, sub: int) -> tuple[str, int] | None:
    """What plays after ``(tid, sub)``: the next entry of the caller's saved
    play queue when the queue holds that track (``None`` at its end), else
    the next song of its album in listing order."""
    q = _get_state().play_queue(getattr(user, "id", None))
    if q:
        ids = q["ids"]
        i = q["current_index"]
        pos = None
        if _split_id(store, ids[i]) == (tid, sub):
            pos = i
        else:
            pre = tid + "~"
            pos = next((k for k, x in enumerate(ids) if x == raw_id), None)
            if pos is None:
                pos = next((k for k, x in enumerate(ids) if x == tid or x.startswith(pre)), None)
        if pos is not None:
            if pos + 1 >= len(ids):
                return None
            ntid, nsub = _split_id(store, ids[pos + 1])
            return (ntid, nsub) if store.get_track(ntid) else None
    t = store.get_track(tid)
    # The current snapshot as is (possibly stale — fine for an album's
    # order): the prewarm never triggers or waits for a catalogue build.
    c = _ALBUM_LIST_CACHE
    cat = c.get("cat") if c.get("store") is store else None
    if cat is None:
        return None
    e = cat.track_entry(t) if t else None
    aid = e.id if e is not None else None
    if aid is None and t is not None and t.get("dir_hash") and not (t.get("album") or "").strip():
        aid = _sx.folder_album_id(t["dir_hash"])          # folder albums off
    if aid is None:
        return None
    order, pos = _album_order(store, cat, aid)
    k = pos.get((tid, sub))
    return order[k + 1] if k is not None and k + 1 < len(order) else None


def _prewarm_next_for_subsonic(request: Request, user, raw_id: str, tid: str, sub: int,
                               time_offset: float) -> None:
    """Schedule the next track's background render (see above).  Plain work
    on the event loop, no awaits — the stream response is never delayed; the
    render itself runs as a task.  Only a real play start schedules: a HEAD,
    a Range continuation / seek or a ``timeOffset`` start does not."""
    try:
        if request.method != "GET" or time_offset:
            return
        rng = (request.headers.get("range") or "").replace(" ", "").lower()
        if rng and not rng.startswith("bytes=0-"):
            return
        from soniqboom.api import stream as _st
        store = get_store()
        nxt = _next_tune(store, user, raw_id, tid, sub)
        if nxt is None or nxt == (tid, sub):
            return
        ntid, nsub = nxt
        t = store.get_track(ntid)
        path = (t or {}).get("path") or ""
        if not path:
            return
        # The web endpoint's gates: routed with the record (a verified Amiga
        # module keeps its uade key), native formats need nothing, the
        # "Prepare upcoming tracks" setting gates rendered formats only, and
        # a remote file must already be in the local cache.
        ext, uade_named = _st._render_ident(path, t)
        if ((ext in _st.NATIVE and not uade_named) or not _st._prewarm_allowed(ext, uade_named)
                or not _st._remote_bytes_local(path)):
            return
        who = _st._prewarm_requester(request, None, getattr(user, "username", None)
                                     or getattr(user, "id", None) or "", None)
        task = asyncio.get_running_loop().create_task(_start_prewarm(ntid, nsub, ext, who))
        _PREWARM_BG.add(task)
        task.add_done_callback(_PREWARM_BG.discard)
    except Exception:              # noqa: BLE001 — a prewarm must never fail a stream
        log.debug("Subsonic next-track prewarm not scheduled", exc_info=True)


async def _start_prewarm(track_id: str, subsong: int, ext: str, who: str) -> None:
    """Register the prewarm in the stream module's registry
    (``stream._schedule_prewarm`` — the one the web player's
    ``POST /api/stream/{id}/prewarm`` uses), so both share its FIFO cap,
    per-owner ``/prewarm/retain`` and dedupe by (track, format, tune)."""
    from soniqboom.api import stream as _st
    from soniqboom.core.data import get_track as _gt
    track = await _gt(track_id)
    if track is None:
        return
    _st._schedule_prewarm(track_id, track, subsong, _st.PRIO_AHEAD, who, ext)


@_media_route("/stream")
@_media_route("/stream.view")
@_wrap
async def stream(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    maxBitRate: int | None = Query(default=None),
    format: str | None = Query(default=None),
    sampleRate: int | None = Query(default=None),
    timeOffset: float | None = Query(default=None, ge=0),
    estimateContentLength: bool = Query(default=False),
    c: str = "",
    f: str = Query(default="xml"),
):
    """Serve a track's bytes inline to a Subsonic client.

    History: we used to 307-redirect to ``/api/stream/{id}`` so the same
    code path served both browser and Subsonic clients.  Two production
    bugs killed that:

      1. iOS AVPlayer (which Amperfy hands the URL to) occasionally drops
         the original query string when following the redirect — the
         second hop arrived at ``/api/stream/{id}`` with no auth params,
         was rejected by the cookie-only middleware, Amperfy treated the
         track as zero bytes / zero seconds, and the queue burned
         through track after track.
      2. AVPlayer infers content type from the URL extension as much as
         the ``Content-Type`` header.  The redirect target had no
         extension, so even when auth survived, AVPlayer sometimes
         refused to decode the bytes (especially for FLAC, where its
         framework support depends on container hints).

    Inline serving sidesteps both — auth happens once here, then we call
    the internal stream handler directly (pre-authenticated, see
    ``_stream_preauthed``) to reuse its range / transcode / rendered-format
    logic.  ``timeOffset`` (seconds) starts a transcoded stream that far in
    (a file served as is ignores it — clients seek those with Range).  A
    ``format`` / ``maxBitRate`` transcode that isn't cached yet streams live
    (chunked, no Content-Length, no Range) while the cache fills;
    ``estimateContentLength=true`` gives such an MP3 stream an estimated
    length, and exactly that many bytes (``_serve_stream``).  Files, cached
    transcodes and rendered WAVs always carry their exact length.  An
    unknown id is a Subsonic error document (code 70), not an HTTP 404.
    ``<id>~<n>`` plays wire ``n`` of a multi-tune file.
    """
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    tid, sub = _split_id(store, id)
    track = store.get_track(tid)
    if not track:
        raise _SubsonicError(70, "Song not found.")
    if request.method == "HEAD":                        # a probe is not playback
        return await _stream_head(request, tid, target_format=format,
                                  max_bitrate_kbps=int(maxBitRate or 0),
                                  target_sample_rate=int(sampleRate or 0), subsong=sub)
    _note_now_playing(user, tid, c, subsong=sub)        # getNowPlaying (O(1), in memory)
    _prewarm_next_for_subsonic(request, user, id, tid, sub, float(timeOffset or 0.0))
    return await _serve_stream(request, tid, target_format=format,
                               max_bitrate_kbps=int(maxBitRate or 0),
                               target_sample_rate=int(sampleRate or 0),
                               seek=float(timeOffset or 0.0), subsong=sub,
                               estimate_s=(float(_tune_duration(track, sub))
                                           if estimateContentLength else None))


@_media_route("/download")
@_media_route("/download.view")
@_wrap
async def download(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    maxBitRate: int | None = Query(default=None),
    format: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """The ORIGINAL file, byte for byte (spec: no transcoding) — so the
    bytes match the song's ``suffix`` / ``contentType`` / ``size``.  An
    archive member is extracted and a remote-share file fetched first; a
    two-file Amiga module (mdat + smpl) downloads its primary half only.
    A download that explicitly asks for ``format`` (other than ``raw``) or a
    ``maxBitRate`` — or names one tune of a multi-tune file (``<id>~<n>``) —
    is served like ``stream`` instead: the way to fetch a playable offline
    copy of a rendered format.  Needs the download role getUser advertises
    (admin / edit).  Not a now-playing signal."""
    user = _require_user(request, sb_session, u, p, s, t)
    if not _can_download(user):
        raise _SubsonicError(50, "User is not authorized to download files.")
    store = get_store()
    tid, sub = _split_id(store, id)
    track = store.get_track(tid)
    if not track:
        raise _SubsonicError(70, "Song not found.")
    id = tid
    fr = (format or "").lower()
    if (fr and fr != "raw") or (maxBitRate or 0) > 0 or sub:
        if request.method == "HEAD":
            return await _stream_head(request, id, target_format=format,
                                      max_bitrate_kbps=int(maxBitRate or 0), subsong=sub)
        return await _serve_stream(request, id, target_format=format,
                                   max_bitrate_kbps=int(maxBitRate or 0),
                                   target_sample_rate=0, seek=0.0, subsong=sub)
    _suffix, mime, _ts, _tm = _delivery(track)
    raw_path = track.get("path") or ""
    member = raw_path.split("::")[-1].replace("\\", "/").rsplit("/", 1)[-1] or f"{id}.{_suffix}"
    from urllib.parse import quote
    disp = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(member, safe="")}
    if request.method == "HEAD":
        if _source_offline(track):
            raise _SubsonicError(0, "The music source for this song is offline.", http_status=503)
        size = await _local_size(raw_path)
        if size is None:
            size = _safe_int(track.get("file_size")) or None
        resp = _head_response(mime, size)
        resp.headers.update(disp)
        return resp
    from starlette.background import BackgroundTask
    from soniqboom.api.stream import _range_file_response, _resolve_play_source, _zip_unpin
    from soniqboom.core.data import get_track as _gt
    model = await _gt(id)
    if model is None:
        raise _SubsonicError(70, "Song not found.")
    try:
        path, _ext, _named, pin = await _resolve_play_source(id, model)
    except HTTPException as exc:
        if exc.status_code == 416:
            raise
        raise _stream_http_error(exc) from None
    try:
        return await _range_file_response(
            request, path, media_type=mime, headers=disp,
            background=BackgroundTask(_zip_unpin, pin) if pin else None)
    except BaseException as exc:
        if pin:
            _zip_unpin(pin)               # the response never took ownership
        if isinstance(exc, HTTPException) and exc.status_code != 416:
            raise _stream_http_error(exc) from None
        raise


# ── OpenSubsonic Transcoding extension ──────────────────────────────────────
# Spec: https://opensubsonic.netlify.app/docs/extensions/transcoding/
#
# ``getTranscodeDecision?mediaId=&mediaType=song`` with a POSTed ClientInfo
# JSON body (directPlayProfiles / transcodingProfiles, bitrates in bits per
# second) answers ``transcodeDecision``: ``canDirectPlay`` / ``canTranscode``,
# ``transcodeReason`` [..], ``sourceStream`` / ``transcodeStream`` and, when a
# transcode is needed, ``transcodeParams`` — a signed token that
# ``getTranscodeStream?mediaId=&mediaType=&transcodeParams=[&offset=]`` takes.
# The older SoniqBoom form (``id`` + ``clientCodecs`` + ``maxBitRate`` kbps,
# ``token`` on the stream call) keeps working, and the response still carries
# its keys (``transcoded`` / ``token`` / ``streamUrl`` …) alongside.
#
# The token carries (track id, codec, bitrate kbps, sample rate, file mtime +
# size, expiry, nonce).  When the file changes on disk its mtime / size moves
# and the token is refused, so the client asks for a fresh decision instead of
# streaming bytes that don't match it.
#
# We sign with HMAC-SHA256 keyed off the server secret.  Tokens are valid
# for 24 h by default — long enough that clients can cache the decision
# across a session, short enough that a stolen token has bounded lifetime.

_TOKEN_TTL_SECONDS = 24 * 60 * 60


@functools.cache
def _server_secret() -> bytes:
    """Server-local secret for signing transcode / radio tokens (memoised:
    the derivation is a 100k-iteration PBKDF2 — ~11 ms on the event loop per
    call, which a per-station token listing multiplied).  Reuses the same
    machine-identity-derived key the credential store uses for Fernet —
    deterministic across restarts on the same host, distinct per host,
    no separate key file needed."""
    try:
        from soniqboom.core.credentials import _derive_key
        # ``_derive_key`` returns urlsafe-base64-encoded raw bytes; HMAC
        # works fine with either form, but decoding gives us the raw
        # 32-byte secret which is the standard Fernet key material.
        import base64
        return base64.urlsafe_b64decode(_derive_key())
    except Exception:
        # Fallback for test/dev environments — deliberately fixed so
        # tokens issued in one test run verify in the next.
        return b"sb-fallback-secret-do-not-use-in-production-32b"


def _sign_token(claims: dict[str, Any]) -> str:
    """Compact, dependency-free HMAC-SHA256 token (JWT-shaped but we
    don't claim it's RFC 7519 — no need to drag in a JWT library)."""
    import base64, hashlib, hmac, json
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"SBT"}').rstrip(b"=").decode()
    body   = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    sig    = hmac.new(_server_secret(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    sig64  = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{header}.{body}.{sig64}"


def _verify_token(token: str) -> dict[str, Any] | None:
    """Return claims dict if valid + unexpired, else None."""
    import base64, hashlib, hmac, json
    try:
        header_b64, body_b64, sig_b64 = token.split(".")
    except ValueError:
        return None
    expected = hmac.new(
        _server_secret(), f"{header_b64}.{body_b64}".encode(), hashlib.sha256,
    ).digest()
    try:
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except ValueError:            # binascii.Error — a mangled / truncated token
        return None
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        body = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        claims = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return None
    if int(claims.get("exp", 0)) < int(time.time()):
        return None
    return claims


# ── Transcode-token binding ──────────────────────────────────────────────────
# A signed transcode token is a transcode *decision*, valid for
# ``_TOKEN_TTL_SECONDS``; every getTranscodeStream request is authenticated on
# its own as well.  The token carries the minting account (``uid``) and is
# refused when another account presents it — the same client keeps using its
# link across Range requests, pause / resume and network changes (Wi-Fi ↔
# cellular, IPv4 ↔ IPv6), which an address binding broke.

# Format → quality tier mapping for the decision heuristic.  Codec names
# here MUST be post-``_normalise_codec`` canonical forms (it collapses
# vorbis → ogg, m4a → aac before lookup).
_DECODER_QUALITY = {
    "flac": 100, "alac": 100, "wav": 100, "ape": 100, "wv": 100,
    "opus": 70, "ogg": 60, "aac": 50, "mp3": 40,
}



def _normalise_codec(name: str) -> str:
    """Map mutagen / ffprobe / user-input codec names to a canonical set
    so a client claiming ``opus`` and a server tagging ``Opus`` agree."""
    n = (name or "").lower().strip()
    return {"vorbis": "ogg", "ogg vorbis": "ogg", "m4a": "aac",
            "alac/aac": "alac", "alac": "alac", "wavpack": "wv",
            "musepack": "mpc"}.get(n, n)


# Targets the stream handler produces on request (``format=``); WAV is what it
# sends when no format is given (and the ONLY output of the renderers).
_TX_TARGETS = ("flac", "mp3", "ogg", "wav")
_CLIENT_INFO_MAX_BYTES = 256 * 1024
# Spec codec names for a container we emit.
_CODEC_NAME = {"ogg": "vorbis", "wav": "pcm"}


def _codec_key(name: str) -> str:
    """Client / tag codec or container name → our canonical key."""
    n = _normalise_codec(name)
    return {"pcm": "wav", "pcm_s16le": "wav", "wave": "wav", "mpeg": "mp3",
            "mp4": "aac", "m4b": "aac"}.get(n, n)


async def _client_info(request: Request) -> dict | None:
    """The POSTed ClientInfo JSON (spec form), or None for the legacy
    query-string form.  Bounded read; malformed → error 10."""
    if request.method != "POST":
        return None
    if not (request.headers.get("content-type") or "").lower().startswith("application/json"):
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _CLIENT_INFO_MAX_BYTES:
            raise _SubsonicError(10, "ClientInfo body too large.")
    import json
    try:
        data = json.loads(bytes(body) or b"{}")
    except ValueError:
        raise _SubsonicError(10, "ClientInfo is not valid JSON.")
    if not isinstance(data, dict):
        raise _SubsonicError(10, "ClientInfo must be a JSON object.")
    return data


def _num_field(d: dict, key: str) -> int:
    v = d.get(key)
    return _safe_int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _http_ok(prof: dict) -> bool:
    protos = prof.get("protocols") if "protocols" in prof else prof.get("protocol")
    if isinstance(protos, str):
        protos = [protos]
    if not protos:
        return True
    return any(str(x).lower() in ("http", "*") for x in protos)


def _source_codec(track: dict, suffix: str) -> str:
    label = _codec_key((track.get("format") or "").split("/", 1)[0])
    if suffix in _MP4_EXTS:
        return label if label in ("aac", "alac") else "aac"
    if suffix in _DIRECT_EXTS:
        return "opus" if label == "opus" else _codec_key(suffix)
    return label or suffix


# A codec-profile limitation's name → the source value it constrains, and the
# transcode reason a failing required one gives.  (``audioProfile`` has no
# source value here and is not evaluated.)
_LIMIT_REASON = {
    "audiochannels":   "audio channels not supported",
    "audiobitrate":    "audio bitrate not supported",
    "audiosamplerate": "audio samplerate not supported",
    "audiobitdepth":   "audio bitdepth not supported",
}


def _limit_values(lim: dict) -> list[int] | None:
    """A Limitation's ``values`` as ints, or None when missing / malformed
    (a malformed limitation is ignored, never an error)."""
    vals = lim.get("values")
    if isinstance(vals, (str, int, float)) and not isinstance(vals, bool):
        vals = [vals]
    if not isinstance(vals, list) or not vals:
        return None
    try:
        return [int(float(str(v).strip())) for v in vals]
    except (TypeError, ValueError, OverflowError):
        return None


def _limitation_fails(lim: dict, actual: int) -> bool:
    """Whether ``actual`` violates one ClientInfo codec-profile Limitation.
    An unknown actual value (0) or a malformed limitation passes.
    ``LessThanEqual`` / ``GreaterThanEqual`` use the first value only (spec);
    ``Equals`` / ``NotEquals`` test membership in all of them."""
    if not actual:
        return False
    vals = _limit_values(lim)
    if vals is None:
        return False
    cmp = str(lim.get("comparison") or "").strip().lower()
    if cmp == "lessthanequal":
        return actual > vals[0]
    if cmp == "greaterthanequal":
        return actual < vals[0]
    if cmp == "equals":
        return actual not in vals
    if cmp == "notequals":
        return actual in vals
    return False


def _limit_cap(lims: list[dict], name: str) -> int:
    """The tightest ``LessThanEqual`` cap on ``name`` among ``lims`` (0: none)."""
    cap = 0
    for lim in lims:
        if (str(lim.get("name") or "").strip().lower() == name
                and str(lim.get("comparison") or "").strip().lower() == "lessthanequal"):
            vals = _limit_values(lim)
            if vals and vals[0] > 0:
                cap = min(cap, vals[0]) if cap else vals[0]
    return cap


def _stream_desc(container: str, codec: str, *, channels: int = 0, bitrate_bps: int = 0,
                 samplerate: int = 0, bitdepth: int = 0) -> dict:
    out: dict = {"protocol": "http", "container": container,
                 "codec": _CODEC_NAME.get(codec, codec)}
    for k, v in (("audioChannels", channels), ("audioBitrate", bitrate_bps),
                 ("audioSamplerate", samplerate), ("audioBitdepth", bitdepth)):
        if v:
            out[k] = int(v)
    return out


@_route("/getTranscodeDecision")
@_route("/getTranscodeDecision.view")
@_wrap
async def get_transcode_decision(
    request: Request,
    mediaId: str | None = Query(default=None),
    mediaType: str | None = Query(default=None),
    id: str | None = Query(default=None, description="Legacy alias of mediaId"),
    clientCodecs: str | None = Query(
        default=None, max_length=512,
        description="Legacy: comma-separated codecs the client decodes natively",
    ),
    maxBitRate: int = Query(
        default=0, ge=0, le=2_500_000,
        description="Legacy: max bitrate in kbps; 0 means no limit",
    ),
    maxSampleRate: int = Query(
        default=0, ge=0, le=384_000,
        description="Legacy: max sample rate in Hz; 0 means no limit",
    ),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """Decide how ``mediaId`` reaches a client with the given capabilities.

    Direct play only when ``/rest/stream`` sends the file itself (browser-
    native formats, AAC-in-MP4) AND a direct-play profile covers its
    container / codec (and its channel count, ``maxAudioChannels``) AND its
    bitrate is within ``maxAudioBitrate`` AND every *required* limitation of
    the source codec's ``codecProfiles`` entry holds (audioChannels /
    audioBitrate / audioSamplerate / audioBitdepth; a non-required one never
    blocks direct play).  Otherwise the target is the client's first
    transcoding profile the stream handler can produce (FLAC / MP3 / Ogg
    Vorbis / WAV — a lossy one first under a bitrate cap, MP3 — always ≤ 2
    channels — first when the source has more channels than a profile
    allows); rendered formats (SID, tracker, Amiga, chip, MIDI) and
    ffmpeg-decoded ones default to WAV.  The target codec's own
    ``LessThanEqual`` audioSamplerate / audioBitrate limits cap the
    transcode.  Cheap and idempotent; the ``transcodeParams`` token is only
    minted when a transcode is needed."""
    user = _require_user(request, sb_session, u, p, s, t)
    mid = mediaId or id
    if not mid:
        raise _SubsonicError(10, "Required parameter 'mediaId' is missing.")
    mtype = (mediaType or "song").strip().lower()
    if mtype == "podcast":
        raise _SubsonicError(70, "Podcast episode not found.")
    if mtype != "song":
        raise _SubsonicError(10, "mediaType must be 'song' or 'podcast'.")
    store = get_store()
    tid, sub = _split_id(store, mid)          # a tune id decides for that tune
    track = store.get_track(tid)
    if track is None:
        raise _SubsonicError(70, "Song not found.")
    info = await _client_info(request)

    suffix, _mime, t_suffix, _tm = _delivery(track)
    src_codec = _source_codec(track, suffix)
    src_br_bps = _safe_int(track.get("bitrate"))
    src_sr = _safe_int(track.get("sample_rate"))
    src_ch = _safe_int(track.get("channels"))

    if info is not None:
        profiles = [x for x in (info.get("directPlayProfiles") or [])
                    if isinstance(x, dict) and _http_ok(x)]
        max_br_kbps = _num_field(info, "maxAudioBitrate") // 1000
        max_tx_kbps = _num_field(info, "maxTranscodingAudioBitrate") // 1000 or max_br_kbps
        tx_order = []
        tx_max_ch: dict[str, int] = {}          # codec → its profile's maxAudioChannels
        for x in info.get("transcodingProfiles") or []:
            if isinstance(x, dict) and _http_ok(x):
                k = _codec_key(str(x.get("audioCodec") or x.get("container") or ""))
                if k and k not in tx_order:
                    tx_order.append(k)
                    tx_max_ch[k] = _num_field(x, "maxAudioChannels")
        # codecProfiles: codec → its limitations (type AudioCodec, the only
        # one the spec defines; a profile without a type counts as one).
        codec_limits: dict[str, list[dict]] = {}
        for cp in info.get("codecProfiles") or []:
            if not isinstance(cp, dict) or \
                    str(cp.get("type") or "AudioCodec").strip().lower() != "audiocodec":
                continue
            k = _codec_key(str(cp.get("name") or ""))
            lims = cp.get("limitations")
            if k and isinstance(lims, list):
                codec_limits.setdefault(k, []).extend(x for x in lims if isinstance(x, dict))

        def _profile_ok(prof: dict) -> bool:
            conts = {_codec_key(str(c)) for c in (prof.get("containers") or [])}
            cods = {_codec_key(str(c)) for c in (prof.get("audioCodecs") or [])}
            return ((not conts or "*" in conts or _codec_key(suffix) in conts or src_codec in conts)
                    and (not cods or "*" in cods or src_codec in cods))
        matching = [x for x in profiles if _profile_ok(x)]
        codec_ok = bool(matching)
        channels_ok = any(not src_ch or not _num_field(x, "maxAudioChannels")
                          or src_ch <= _num_field(x, "maxAudioChannels") for x in matching)
    else:
        accept = {_codec_key(c) for c in (clientCodecs or "mp3,aac,ogg,flac,opus").split(",")
                  if c.strip()}
        max_br_kbps = max_tx_kbps = maxBitRate
        tx_order = sorted(accept, key=lambda c: _DECODER_QUALITY.get(c, 0), reverse=True)
        codec_ok = src_codec in accept
        channels_ok = True
        tx_max_ch = {}
        codec_limits = {}

    src_bd = _safe_int(track.get("bit_depth"))
    src_vals = {"audiochannels": src_ch, "audiobitrate": src_br_bps,
                "audiosamplerate": src_sr, "audiobitdepth": src_bd}
    reasons: list[str] = []
    if t_suffix is not None:
        reasons.append("container not supported")   # rendered / ffmpeg-decoded source
    else:
        if not codec_ok:
            reasons.append("audio codec not supported")
        elif not channels_ok:
            reasons.append("audio channels not supported")
        if max_br_kbps and src_br_bps // 1000 > max_br_kbps:
            reasons.append("audio bitrate not supported")
        # The source codec's required limitations (a non-required one is a
        # preference and never blocks direct play).
        for lim in codec_limits.get(src_codec, ()):
            name = str(lim.get("name") or "").strip().lower()
            why = _LIMIT_REASON.get(name)
            if (why and lim.get("required") in (True, "true", "True", 1)
                    and why not in reasons and _limitation_fails(lim, src_vals[name])):
                reasons.append(why)
    can_direct = not reasons

    target: str | None = None
    if not can_direct:
        cands = [c for c in tx_order if c in _TX_TARGETS]
        if t_suffix is None:
            # A file the stream handler serves as is gets re-encoded only into
            # a codec it produces on request: another codec, or — when only
            # the bitrate is too high — its own lossy codec at the cap.
            # Never "wav" (the handler would send the file itself).
            same_ok = reasons == ["audio bitrate not supported"]
            cands = [c for c in cands if c != "wav" and (same_ok or c != src_codec)]
        elif not cands:
            cands = ["wav"]           # what the stream handler sends with no format
        if max_tx_kbps and cands and cands[0] in ("wav", "flac"):
            # A bitrate cap is only honoured by a lossy codec: prefer the
            # client's first lossy profile when it has one.
            lossy = [c for c in cands if c in ("mp3", "ogg")]
            if lossy:
                cands = lossy + [c for c in cands if c not in lossy]
        if src_ch > 2 and cands:
            # More channels than a target's profile / required channel limit
            # allows: MP3 output is always ≤ 2 channels (the encoder downmixes),
            # the others keep the source's channels — targets that fit first.
            def _ch_fits(c: str) -> bool:
                out_ch = min(src_ch, 2) if c == "mp3" else src_ch
                if tx_max_ch.get(c) and out_ch > tx_max_ch[c]:
                    return False
                return not any(
                    str(lim.get("name") or "").strip().lower() == "audiochannels"
                    and lim.get("required") in (True, "true", "True", 1)
                    and _limitation_fails(lim, out_ch) for lim in codec_limits.get(c, ()))
            fits = [c for c in cands if _ch_fits(c)]
            cands = fits + [c for c in cands if c not in fits]
        target = cands[0] if cands else None
    tbr = max_tx_kbps if target in ("mp3", "ogg") else 0
    tsr = maxSampleRate if (target and info is None) else 0
    if target and codec_limits.get(target):
        # The target codec's own caps: a sample-rate cap resamples (only
        # down — never above what the source has), a bitrate cap bounds a
        # lossy encode.
        sr_cap = _limit_cap(codec_limits[target], "audiosamplerate")
        if sr_cap and src_sr > sr_cap:
            tsr = min(tsr, sr_cap) if tsr else sr_cap
        br_cap = _limit_cap(codec_limits[target], "audiobitrate") // 1000
        if br_cap and target in ("mp3", "ogg"):
            tbr = min(tbr, br_cap) if tbr else br_cap
    out_ch = (min(src_ch, 2) if target == "mp3" else src_ch) if src_ch else 0

    decision: dict[str, Any] = {
        "canDirectPlay": can_direct,
        "canTranscode": target is not None,
    }
    if reasons:
        decision["transcodeReason"] = reasons
    if not can_direct and target is None:
        decision["errorReason"] = "no supported transcoding profile for this source"
    decision["sourceStream"] = _stream_desc(suffix, src_codec, channels=src_ch,
                                            bitrate_bps=src_br_bps, samplerate=src_sr,
                                            bitdepth=src_bd)
    # Legacy (pre-spec SoniqBoom) keys, kept for existing callers.
    decision["track_id"] = mid
    if can_direct:
        decision["transcoded"] = False
        decision["streamUrl"] = f"/rest/stream?id={mid}"
        decision["sourceCodec"] = src_codec
    elif target is not None:
        decision["transcodeStream"] = _stream_desc(
            target, target, channels=out_ch, bitrate_bps=tbr * 1000, samplerate=tsr)
        decision["transcoded"] = True
        decision["targetCodec"] = target
        if tbr:
            decision["targetBitRate"] = tbr
        if tsr:
            decision["targetSampleRate"] = tsr
        mtime: float = 0.0
        size: int = 0
        path = track.get("path") or ""
        if not path.startswith(_REMOTE_SCHEMES) and "::" not in path:
            try:
                st = await asyncio.to_thread(os.stat, path)
                mtime = float(st.st_mtime)
                size = int(st.st_size)
            except OSError:
                pass
        # Float mtime + size together close the "rename-then-replace at
        # identical mtime" silent-stale window.  ``jti`` is a per-issue random
        # nonce (every link differs); ``uid`` binds the link to this account.
        import secrets as _secrets
        now = int(time.time())
        token = _sign_token({
            "tid": tid, "sub": sub, "tc": target, "tbr": tbr, "tsr": tsr,
            "mt": f"{mtime:.6f}", "sz": size, "iat": now,
            "exp": now + _TOKEN_TTL_SECONDS, "jti": _secrets.token_urlsafe(12),
            "uid": str(getattr(user, "id", "") or ""),
        })
        decision["transcodeParams"] = token
        decision["token"] = token
        decision["expiresIn"] = _TOKEN_TTL_SECONDS
    return _ok({"transcodeDecision": decision}, fmt=f)


def _transcode_stream_error(spec: bool, status: int, code: int, message: str, fmt: str):
    """Spec form (``transcodeParams``): a plain HTTP error — the client is
    reading an audio stream.  Legacy ``token`` form: a Subsonic envelope."""
    if spec:
        return Response(content=message, status_code=status, media_type="text/plain")
    return _err(code, message, fmt=fmt)


@_media_route("/getTranscodeStream")
@_media_route("/getTranscodeStream.view")
@_wrap
async def get_transcode_stream(
    request: Request,
    transcodeParams: str | None = Query(default=None),
    token: str | None = Query(default=None, description="Legacy alias of transcodeParams"),
    mediaId: str | None = Query(default=None),
    mediaType: str | None = Query(default=None),
    offset: float = Query(default=0, ge=0, description="Start offset in seconds"),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    c: str = "",
    f: str = Query(default="xml"),
):
    """Stream the track described by a previously-issued ``transcodeParams``.

    Auth still applies — the token is a transcode *decision*, not a
    bypass of Subsonic auth.  A token whose file changed on disk since it
    was issued is refused; the client should ask for a new decision.
    ``offset`` (seconds) is passed to the stream handler as the start
    position."""
    user = _require_user(request, sb_session, u, p, s, t)
    spec = transcodeParams is not None or mediaId is not None
    raw = transcodeParams or token
    if not raw:
        return _transcode_stream_error(spec, 400, 10,
                                       "Required parameter 'transcodeParams' is missing.", f)
    if (mediaType or "song").strip().lower() != "song":
        return _transcode_stream_error(spec, 404, 70, "Only songs can be streamed.", f)
    claims = _verify_token(raw)
    if claims is None or not claims.get("tid"):
        # Plain-English message — clients like DSub/Substreamer surface
        # server messages verbatim to end users.  (A token of another kind —
        # e.g. a radio link — carries no track id: treated as invalid.)
        return _transcode_stream_error(spec, 410, 70,
                                       "This stream link expired. Tap play again.", f)
    store = get_store()
    tid = str(claims["tid"])
    # The tune the decision was made for (a token minted before tunes were
    # carried: wire 0, the file's default tune).
    want = (tid, _safe_int(claims.get("sub")))
    if mediaId is not None and _split_id(store, mediaId) != want:
        return _transcode_stream_error(spec, 400, 70,
                                       "transcodeParams belong to a different mediaId.", f)
    sub = want[1]

    # The link belongs to the account that asked for the decision (a token
    # minted before links carried ``uid`` is accepted as is).
    tok_uid = claims.get("uid")
    if tok_uid and str(tok_uid) != str(getattr(user, "id", "")):
        return _transcode_stream_error(spec, 403, 70,
                                       "This stream link belongs to another account. "
                                       "Tap play again.", f)

    track = store.get_track(tid)
    if track is None:
        return _transcode_stream_error(spec, 404, 70, "Song not found.", f)

    # Freshness check — file changed under us, force the client to renegotiate.
    # Compare both mtime (float, sub-second precision) AND size — defeats the
    # "rename-and-replace preserves mtime" silent-stale window.
    path = track.get("path") or ""
    if claims.get("mt") and not path.startswith(_REMOTE_SCHEMES) and "::" not in path:
        try:
            st = await asyncio.to_thread(os.stat, path)
            claimed_mt = float(claims["mt"])
            claimed_sz = int(claims.get("sz") or 0)
            if (abs(float(st.st_mtime) - claimed_mt) > 0.001 or
                    (claimed_sz and int(st.st_size) != claimed_sz)):
                return _transcode_stream_error(
                    spec, 410, 0, "This song changed on the server. Tap play again to start over.", f)
        except OSError:
            # Stat failure mid-stream — let the stream handler surface the
            # real error (404, permission denied).
            pass

    # Serve inline via the internal stream handler — same architectural
    # reason as ``/rest/stream`` (iOS AVPlayer occasionally drops query params
    # on 307s and infers content-type from URL extensions).  The negotiated
    # codec / bitrate / sample rate come from the token, so the bytes match
    # the decision the client already cached.  A stream failure answers the
    # spec form as plain text with its real HTTP status.
    try:
        if request.method == "HEAD":
            return await _stream_head(
                request, tid, target_format=(str(claims["tc"]) if claims.get("tc") else None),
                max_bitrate_kbps=int(claims.get("tbr") or 0),
                target_sample_rate=int(claims.get("tsr") or 0), subsong=sub)
        _note_now_playing(user, tid, c, subsong=sub)
        return await _serve_stream(
            request, tid,
            target_format=(str(claims["tc"]) if claims.get("tc") else None),
            max_bitrate_kbps=int(claims.get("tbr") or 0),
            target_sample_rate=int(claims.get("tsr") or 0),
            seek=float(offset or 0.0), subsong=sub,
        )
    except _SubsonicError as exc:
        if not spec:
            raise
        return _transcode_stream_error(True, exc.http_status or 500, exc.code, exc.message, f)


_COVER_TOKEN_TTL = 30 * 24 * 3600
# Real art: private (the request is authenticated) but cacheable for a day,
# revalidated by ETag (the cached art file's mtime is part of it).
_ART_CACHE_CONTROL = "private, max-age=86400"
# Strong refs for fire-and-forget thumbnail writes.
_ART_BG: set = set()


def _cover_url(request: Request, user, cover_id: str, size: int) -> str:
    """A getCoverArt URL a client can load WITHOUT its own credentials (the
    ``imageUrl`` fields of getAlbumInfo): a signed token bound to the cover
    id and the user, revoked by a password or app-password change (and by
    revoking the API key that minted it), 30-day expiry."""
    from urllib.parse import urlencode
    tok = _sign_token({**_link_claims(request, user, "cover"), "cid": cover_id,
                       "exp": int(time.time()) + _COVER_TOKEN_TTL})
    return f"{_public_base_url(request)}/rest/getCoverArt.view?" + urlencode(
        {"id": cover_id, "size": int(size), "tok": tok})


def _cover_token_ok(tok: str, cover_id: str) -> bool:
    claims = _verify_token(tok)
    if not claims or claims.get("typ") != "cover" or claims.get("cid") != cover_id:
        return False
    return _link_owner_ok(claims) is not None


def _art_response(data: bytes, media_type: str, etag: str | None) -> Response:
    headers = {"Cache-Control": _ART_CACHE_CONTROL}
    if etag:
        headers["ETag"] = etag
    return Response(content=data, media_type=media_type, headers=headers)


@_media_route("/getCoverArt")
@_media_route("/getCoverArt.view")
@_wrap
async def get_cover_art(
    request: Request,
    id: str = Query(...),
    size: int | None = Query(default=None),
    tok: str | None = Query(default=None),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """Return cover-art bytes inline.

    We used to 307-redirect to ``/api/art/{id}`` to share the cache logic
    with the browser UI.  That broke Subsonic clients that authenticated
    via ``?u&s&t`` (Amperfy, DSub, Symfonium): the ``/api/art/*`` endpoint
    is protected by the cookie-only auth middleware, the redirect dropped
    everything but ``?size=``, and the follow-up request landed at the
    middleware with no cookie → 401.  Now we resolve auth once via the
    Subsonic helpers, fetch the bytes via the same internal art-cache
    helpers the SPA uses, and stream them back inline — no redirect, no
    second auth check, no cross-endpoint surprise.

    ``tok`` (a signed cover token from getAlbumInfo's image URLs) stands in
    for credentials for that one id.  Real art carries an ETag and
    ``Cache-Control: private, max-age=86400``; a matching ``If-None-Match``
    is a 304 before any art work.  An unknown album / artist id never
    triggers a catalogue rebuild (it gets the short-lived placeholder).
    """
    if not (tok and _cover_token_ok(tok, id)):
        try:
            _require_user(request, sb_session, u, p, s, t)
        except _SubsonicError as exc:
            if tok and exc.code == 10:
                # The signed link WAS the credential — it's invalid, not missing.
                raise _SubsonicError(40, "This cover link has expired — reload the album.")
            raise
    if not _valid_cover_id(id):
        # Only a MALFORMED id is an error; a well-formed id with no art gets
        # the placeholder below so clients stop re-requesting it.
        raise _SubsonicError(70, "Cover art not found.")
    # Subsonic "size in px" → the smallest cached rendition at least that
    # big (never a smaller image than asked for); no size → the original.
    bucket = _cover_bucket(size)
    # The placeholder too: the 512 px icon unless a small tile was asked for.
    large_ph = not size or size <= 0 or size > 192
    # Album / artist / folder-album IDs are synthetic — resolve them to a
    # sample track (the catalogue's newest-track sample, the same pick the
    # album lists show), then fetch that track's art.  O(1) for albums;
    # never a sorted-index walk over the library, and never a rebuild: a
    # miss is answered from the current snapshot.
    if id.startswith("rs:"):
        return await _station_logo_response(request, id, bucket, large_ph)
    store = get_store()
    resolved_id: str | None = None
    if id.startswith(("al:", _sx.FOLDER_PREFIX)):
        e = _resolve_album_entry(store, id, _catalogue(store))
        resolved_id = (e.sample or {}).get("id") if e is not None else None
    elif id.startswith("ar:"):
        cat = _catalogue(store)
        low = cat.union.by_id.get(id)
        if low is not None:
            # The artist's own photo (getArtistInfo stored it) first; else a
            # representative track's cover.
            if low:
                photo = await _artist_photo_response(request, cat.union.display.get(low, low),
                                                     bucket)
                if photo is not None:
                    return photo
            resolved_id = _artist_sample_track_id(store, low, cat)
    else:
        tid = _split_id(store, id)[0]             # a tune shows its file's art
        if store.get_track(tid):
            resolved_id = tid
    if not resolved_id:
        # Unknown id: answer without touching the art pipeline (which would
        # persist a negative-cache marker for an arbitrary client string).
        # Short-lived: an album / artist id can be missing only briefly (a
        # scan in progress), and clients should pick up its real art then.
        return _placeholder_art(_PLACEHOLDER_SHORT_MAX_AGE, large=large_ph)

    try:
        # Late import so subsonic.py stays loadable even if art.py has a
        # transient init error (we'd rather degrade art than crash routing).
        from soniqboom.api.art import (
            _resolve_full_art,
            _SIZE_MAP,
            _make_etag,
            _art_cached_mtime,
        )
        from soniqboom.core import art_cache

        mtime = _art_cached_mtime(resolved_id, bucket)
        etag = _make_etag(resolved_id, bucket, mtime) if mtime is not None else None
        if etag and request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag,
                                                      "Cache-Control": _ART_CACHE_CONTROL})

        if bucket in _SIZE_MAP:
            thumb = await art_cache.get_art(resolved_id, bucket)
            if thumb:
                return _art_response(thumb, "image/jpeg", etag)
            full_data, _mime = await _resolve_full_art(resolved_id)
            if full_data:
                # Resize ONLY the requested bucket, off the loop.  A fresh
                # extraction already queued the full + both thumbs for the
                # cache (_resolve_full_art's persist job); the write below
                # covers "full image cached, this thumb not" — a duplicate
                # write racing that job is harmless.
                from soniqboom.core.metadata import resize_cover
                data = await asyncio.get_running_loop().run_in_executor(
                    None, resize_cover, full_data, _SIZE_MAP[bucket])
                task = asyncio.get_running_loop().create_task(
                    art_cache.store_art(resolved_id, data, bucket))
                _ART_BG.add(task)
                task.add_done_callback(_ART_BG.discard)
                return _art_response(data, "image/jpeg", None)
            return _placeholder_art(_placeholder_ttl(store, resolved_id), large=large_ph)

        full_data, mime = await _resolve_full_art(resolved_id)
        if full_data:
            return _art_response(full_data, mime or "image/jpeg", etag)
    except Exception:              # noqa: BLE001 — e.g. a corrupt embedded image
        # An image request must get an image: an undecodable cover (the thumb
        # resize raises) degrades to the placeholder, not an error envelope —
        # short-lived, since the failure may be transient (I/O).
        log.debug("Subsonic cover art failed for %s", resolved_id, exc_info=True)
        return _placeholder_art(_PLACEHOLDER_SHORT_MAX_AGE, large=large_ph)
    return _placeholder_art(_placeholder_ttl(store, resolved_id), large=large_ph)


# Ids a client may legitimately ask art for: track ids (uuid5, ``~<n>`` for
# one tune), our synthetic ``ar:``/``al:``/``fa:`` + 16 hex (a mixed folder's
# share: ``fa:…~<key>``), or another server's opaque ids (letters, digits,
# ``-_.:~``).  Anything else (empty, slashes, spaces, 200+ chars) is
# malformed → 404.
_COVER_ID_RE = re.compile(r"^[A-Za-z0-9._:~\-]{1,128}$")
_SYNTH_COVER_RE = re.compile(r"^(?:ar:[0-9a-f]{16}|al:[0-9a-f]{16}|rs:[0-9a-f]{16}"
                             r"|fa:[0-9a-f]{16}(?:~g?(?:0|[0-9a-f]{12}))?)$")


def _valid_cover_id(raw: str) -> bool:
    if not raw or not _COVER_ID_RE.match(raw):
        return False
    if raw.startswith(("ar:", "al:", "rs:", _sx.FOLDER_PREFIX)):
        return bool(_SYNTH_COVER_RE.match(raw))
    return True


def _artist_sample_track_id(store, name_l: str, cat: _sx.Catalogue) -> str | None:
    """A stable representative track for artist art: the sample of the
    artist's newest album (album art is likelier — O(albums)), else the newest
    of their loose tracks."""
    albums = cat.albums_for_artist(name_l)
    if albums:
        e = max(albums, key=lambda x: (x.added or 0, x.id))
        if (e.sample or {}).get("id"):
            return e.sample["id"]
    best = best_any = None
    cands = cat.unknown_loose if name_l == "" else _artist_track_ids(store, name_l)
    for tid in cands:
        t = store.get_track(tid)
        if not t:
            continue
        k = (t.get("added_at") or 0, tid)
        if best_any is None or k > best_any:
            best_any = k
        if (t.get("album") or "").strip() and (best is None or k > best):
            best = k
    pick = best or best_any
    return pick[1] if pick else None


def _cover_bucket(size: int | None) -> str:
    """The getCoverArt rendition for a requested ``size`` (px): the smallest
    cached thumbnail at least that big (api/art.py ``_SIZE_MAP`` — sm 200,
    lg 550), else the original; no size (or ≤ 0) is the original, as the
    spec defines.  Same cached files as before, no extra resize."""
    if not size or size <= 0:
        return "full"
    try:
        from soniqboom.api.art import _SIZE_MAP
    except Exception:                  # noqa: BLE001 — degrade, never break art
        _SIZE_MAP = {"sm": 200, "lg": 550}
    for b, px in sorted(_SIZE_MAP.items(), key=lambda kv: kv[1]):
        if size <= px:
            return b
    return "full"


# Bundled icon bytes per file name, read once.
_PLACEHOLDER: dict = {}
# Minimal 1×1 grey PNG — only if the bundled icon can't be found.
_FALLBACK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b55"
    "0000000a49444154789c636800000082008177cd72b60000000049454e44ae426082")


_PLACEHOLDER_MAX_AGE = 86400          # a resolved track that has no art
_PLACEHOLDER_SHORT_MAX_AGE = 300      # unresolvable id / transient failure


def _placeholder_ttl(store, track_id: str) -> int:
    """How long a client may cache the placeholder for ``track_id``: a day
    when the track genuinely has no art, but only the short TTL when its art
    may just be out of reach — its source offline (a client would otherwise
    keep the icon for a day after the share reconnects), or a remote file
    whose tagged cover hasn't been fetched yet.  Stored state only, no I/O."""
    t = store.get_track(track_id) if track_id else None
    if not t:
        return _PLACEHOLDER_SHORT_MAX_AGE
    if _source_offline(t):
        return _PLACEHOLDER_SHORT_MAX_AGE
    path = t.get("path") or ""
    if (path.startswith(("ftp://", "smb://")) and t.get("cover_art")
            and not store.is_art_absent(track_id)):
        return _PLACEHOLDER_SHORT_MAX_AGE           # backfill pending
    return _PLACEHOLDER_MAX_AGE


def _icon_bytes(name: str) -> bytes | None:
    """A bundled ``frontend/icons`` file, memoised (``None`` if missing)."""
    if name in _PLACEHOLDER:
        return _PLACEHOLDER[name]
    import sys
    from pathlib import Path as _P
    here = _P(__file__).resolve().parent.parent            # soniqboom/
    exe_dir = _P(sys.executable).resolve().parent
    data = None
    # Same candidates main._find_frontend_dir tries (source, frozen, .app).
    for base in (here / "frontend", exe_dir / "frontend",
                 exe_dir.parent / "Resources" / "frontend"):
        try:
            data = (base / "icons" / name).read_bytes()
            break
        except OSError:
            continue
    _PLACEHOLDER[name] = data
    return data


def _placeholder_art(max_age: int = _PLACEHOLDER_MAX_AGE, *, large: bool = False) -> Response:
    """The app icon as a PNG (not SVG — several clients can't render SVG),
    cacheable (a day for genuinely art-less tracks) so clients stop
    re-requesting art that isn't there.  ``large``: the 512 px icon (a
    client asking for a big image or the original), else the 192 px one."""
    data = ((_icon_bytes("icon-512.png") if large else None)
            or _icon_bytes("icon-192.png") or _FALLBACK_PNG)
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": f"public, max-age={int(max_age)}"})


# ── Radio station logos (getInternetRadioStations ``coverArt``) ──────────────
# A favourite station's logo (its directory ``favicon``) is fetched ONCE by the
# server — every hop through the relay's SSRF guard (redirects followed by
# hand, each target re-checked; the validating egress proxy when it runs),
# image bodies only, size-capped — decoded with Pillow off the loop (ICO /
# PNG / JPEG / GIF / WebP → PNG, alpha kept; a non-image is refused) and kept
# under the data dir keyed by the favicon URL, so an edited logo is simply a
# new file.  Served by getCoverArt(rs:…) with an ETag; clients never contact
# the station's host.  A failed fetch leaves a marker honoured for a day.

_LOGO_MAX_BYTES = 1024 * 1024
_LOGO_MAX_PIXELS = 16_000_000        # decoded size cap (a small file can be a bomb)
_LOGO_MAX_PX = 512                   # the stored rendition's longest side
_LOGO_SM_PX = 200                    # the small one (getCoverArt size ≤ 200)
_LOGO_TIMEOUT_SEC = 3.0              # getCoverArt waits this long for a first fetch
_LOGO_RETRY_SEC = 86400
_LOGO_MAX_FILES = 256
_LOGO_MAX_INFLIGHT = 8
_LOGO_TASKS: dict[str, asyncio.Task] = {}


def _station_cover_id(sid: str) -> str:
    """``rs:`` + a fixed-width hash of the station id (sids contain ``:``)."""
    return "rs:" + hashlib.sha1(sid.encode("utf-8")).hexdigest()[:16]


def _logo_url_of(st: Any) -> str:
    """A favourite's http(s) favicon URL, else ``""``."""
    fav = st.get("favicon") if isinstance(st, dict) else None
    if not isinstance(fav, str):
        return ""
    fav = fav.strip()
    return fav if fav.lower().startswith(("http://", "https://")) and len(fav) <= 2048 else ""


def _logo_dir():
    from soniqboom.core import radiodir
    d = radiodir._dir() / "logos"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _fetch_logo_bytes(url: str) -> bytes | None:
    """The favicon's bytes, or None.  Never follows a redirect blindly: each
    hop (≤ 3) passes ``stations._assert_public_url`` first; the body is
    streamed and dropped past ``_LOGO_MAX_BYTES``; only an image (or an
    untyped octet stream — Pillow decides) is kept."""
    import httpx
    from urllib.parse import urljoin
    from soniqboom.api import stations as _stations
    from soniqboom.core import radiodir, ssrf_proxy
    kw: dict = dict(timeout=httpx.Timeout(5.0), follow_redirects=False,
                    headers={"User-Agent": radiodir.USER_AGENT})
    proxy = ssrf_proxy.proxy_url()
    if proxy:
        tr = httpx.AsyncHTTPTransport(proxy=proxy)
        kw["mounts"] = {"https://": tr, "http://": tr}
    try:
        async with httpx.AsyncClient(**kw) as cx:
            for _hop in range(4):
                await _stations._assert_public_url(url)
                async with cx.stream("GET", url) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location") or ""
                        if not loc:
                            return None
                        url = urljoin(url, loc)
                        continue
                    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if r.status_code != 200 or not (ctype.startswith("image/")
                                                     or ctype == "application/octet-stream"):
                        return None
                    if int(r.headers.get("content-length") or 0) > _LOGO_MAX_BYTES:
                        return None
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > _LOGO_MAX_BYTES:
                            return None
                    return bytes(buf) or None
            return None                                   # too many redirects
    except HTTPException:                  # the guard: a non-public / bad host
        return None
    except Exception as exc:               # noqa: BLE001 — a logo is best-effort
        log.debug("station logo fetch failed %s: %s", url, exc)
        return None


def _logo_renditions(data: bytes) -> tuple[bytes, bytes] | None:
    """``(≤ 512 px PNG, ≤ 200 px PNG)`` of an image, or None when it isn't
    one (or is too big to decode).  Never upscales.  Runs in a worker."""
    from io import BytesIO
    try:
        from PIL import Image
        img = Image.open(BytesIO(data))
        w, h = img.size
        if w <= 0 or h <= 0 or w * h > _LOGO_MAX_PIXELS:
            return None
        img.load()
        img = img.convert("RGBA")
        out = []
        for px in (_LOGO_MAX_PX, _LOGO_SM_PX):
            im = img.copy()
            im.thumbnail((px, px), Image.LANCZOS)
            b = BytesIO()
            im.save(b, format="PNG", optimize=True)
            out.append(b.getvalue())
        return out[0], out[1]
    except Exception:                      # noqa: BLE001 — not an image
        return None


async def _ensure_station_logo(url: str) -> str | None:
    """The logo's cache key once it is on disk (fetched at most once per
    ``_LOGO_RETRY_SEC`` on failure), else None."""
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    d = await asyncio.to_thread(_logo_dir)
    main, neg = d / f"{key}.png", d / f"{key}.noimg"

    def _state() -> str:
        if main.exists():
            return "ok"
        try:
            return "neg" if time.time() - neg.stat().st_mtime < _LOGO_RETRY_SEC else ""
        except OSError:
            return ""
    st = await asyncio.to_thread(_state)
    if st == "ok":
        return key
    if st == "neg":
        return None
    data = await _fetch_logo_bytes(url)
    rend = None
    if data:
        rend = await asyncio.get_running_loop().run_in_executor(None, _logo_renditions, data)

    def _write() -> bool:
        if not rend:
            neg.touch()
            return False
        for name, blob in ((f"{key}.sm.png", rend[1]), (f"{key}.png", rend[0])):
            tmp = d / f"{name}.tmp"
            tmp.write_bytes(blob)
            tmp.replace(d / name)
        neg.unlink(missing_ok=True)
        files = sorted(d.glob("*.png"), key=lambda f: f.stat().st_mtime)
        mains = [f for f in files if not f.name.endswith(".sm.png")]
        for old in mains[:max(0, len(mains) - _LOGO_MAX_FILES)]:
            old.unlink(missing_ok=True)
            (d / f"{old.stem}.sm.png").unlink(missing_ok=True)
        return True
    try:
        ok = await asyncio.to_thread(_write)
    except OSError as exc:
        log.debug("station logo write failed for %s: %s", url, exc)
        return None
    return key if ok else None


async def _station_logo_bounded(url: str) -> str | None:
    """``_ensure_station_logo`` as one shared task per logo, waited on for at
    most ``_LOGO_TIMEOUT_SEC`` (it finishes in the background after)."""
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    task = _LOGO_TASKS.get(key)
    if task is None:
        if len(_LOGO_TASKS) >= _LOGO_MAX_INFLIGHT:
            return None
        task = asyncio.get_running_loop().create_task(_ensure_station_logo(url))
        _LOGO_TASKS[key] = task
        task.add_done_callback(lambda t, k=key: (_LOGO_TASKS.pop(k, None),
                                                 t.cancelled() or t.exception()))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=_LOGO_TIMEOUT_SEC)
    except Exception:                      # noqa: BLE001 — timeout / failure: no logo yet
        return None


async def _station_logo_response(request: Request, cover_id: str, bucket: str,
                                 large_ph: bool) -> Response:
    """getCoverArt for an ``rs:`` id: the favourite station's logo, else the
    placeholder (short-lived: a logo may still be on its way)."""
    from soniqboom.core import radiodir
    favs = await asyncio.to_thread(radiodir.get_favorites)
    url = ""
    for st in favs:
        sid = st.get("sid") if isinstance(st, dict) else None
        if isinstance(sid, str) and sid and _station_cover_id(sid) == cover_id:
            url = _logo_url_of(st)
            break
    if not url:
        return _placeholder_art(_PLACEHOLDER_SHORT_MAX_AGE, large=large_ph)
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    name = f"{key}.sm.png" if bucket == "sm" else f"{key}.png"

    def _load() -> tuple[bytes, float] | None:
        f = _logo_dir() / name
        try:
            return f.read_bytes(), f.stat().st_mtime
        except OSError:
            return None
    # Warm: straight from disk (one worker hop); cold: the shared fetch first.
    hit = await asyncio.to_thread(_load)
    if hit is None:
        if await _station_logo_bounded(url) is None:
            return _placeholder_art(_PLACEHOLDER_SHORT_MAX_AGE, large=large_ph)
        hit = await asyncio.to_thread(_load)
        if hit is None:
            return _placeholder_art(_PLACEHOLDER_SHORT_MAX_AGE, large=large_ph)
    data, mtime = hit
    etag = f'"rs-{key[:16]}:{bucket}:{int(mtime)}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag,
                                                  "Cache-Control": _ART_CACHE_CONTROL})
    return _art_response(data, "image/png", etag)


# ── Scrobble ────────────────────────────────────────────────────────────────

_KW_MEMO: dict = {}


def _accepts_kw(fn, name: str) -> bool:
    """Whether ``fn`` takes keyword ``name`` (memoised per function) — lets
    the scrobble path pass a play's own time to store / scrobbler versions
    that support it, and fall back cleanly on those that don't."""
    key = (getattr(fn, "__qualname__", repr(fn)), getattr(fn, "__module__", ""), name)
    hit = _KW_MEMO.get(key)
    if hit is None:
        import inspect
        try:
            hit = name in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            hit = False
        _KW_MEMO[key] = hit
    return hit


async def _record_subsonic_play(store, user, track: dict, *, ts: int | None = None) -> None:
    """Count one play of ``track`` by ``user`` — the same effects as the web
    player's ``/api/tracks/{id}/played``: the play-stat update, a listening-
    history entry and the external scrobblers (last.fm / ListenBrainz — a
    noop until the user links them).  ``ts`` (epoch s) is when it happened;
    passed on to the store / scrobbler / history versions that take it."""
    tid = track["id"]
    if ts is not None and _accepts_kw(store.record_play, "at"):
        store.record_play(tid, at=ts)
    else:
        store.record_play(tid)
    try:
        from soniqboom.api.smart import push_history
        kw = {"at": ts} if ts is not None and _accepts_kw(push_history, "at") else {}
        await push_history(tid, title=track.get("title") or "",
                           artist=track.get("artist") or "", **kw)
    except Exception:                      # noqa: BLE001 — history is best-effort
        log.debug("history append failed for %s", tid, exc_info=True)
    try:
        from soniqboom.core.scrobble import submit_play
        if ts is not None and _accepts_kw(submit_play, "ts"):
            await submit_play(user, track, ts=ts)
        else:
            await submit_play(user, track)
    except Exception:                      # noqa: BLE001
        pass


@_route("/scrobble")
@_route("/scrobble.view")
@_wrap
async def scrobble(
    request: Request,
    id: list[str] = Query(default_factory=list),
    times: list[int] = Query(default_factory=list, alias="time"),
    submission: bool = Query(default=True),
    c: str = "",
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """Record plays.  Since API 1.8.0 a client may batch several: repeated
    ``id`` with a matching repeated ``time`` (epoch MILLISECONDS each) —
    every one is recorded with its own time; unknown ids are skipped.
    ``submission=false`` is the now-playing signal (last id)."""
    # Mutating endpoint — refuse cookie-only auth to prevent CSRF via
    # an attacker-origin <img src=…>.
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if not id:
        raise _SubsonicError(10, "Required parameter 'id' is missing.")
    store = get_store()
    if submission:
        now = int(time.time())
        for i, raw in enumerate(id):
            tid = _split_id(store, raw)[0]         # a tune counts for its file
            track = store.get_track(tid)
            if not track:
                continue
            ts = min(now, times[i] // 1000) if i < len(times) and times[i] > 0 else None
            await _record_subsonic_play(store, user, track, ts=ts)
    else:
        # submission=false is the "now playing" signal (OpenSubsonic clients
        # send it at track start).  Push it to the external now-playing widgets
        # if the user has linked last.fm / ListenBrainz; otherwise a cheap noop.
        tid, sub = _split_id(store, id[-1])
        track = store.get_track(tid)
        if track:
            _note_now_playing(user, tid, c, from_scrobble=True, subsong=sub)
            try:
                from soniqboom.core.scrobble import submit_now_playing
                await submit_now_playing(user, track)
            except Exception:
                pass
    return _ok(fmt=f)


# ── Playlists ───────────────────────────────────────────────────────────────
# Native playlist entries are bare track ids OR ``{id, subsong}`` objects (a
# tune pinned with the web picker).  Subsonic sees the latter as tune ids
# (``<id>~<n>``) and createPlaylist / updatePlaylist map them back, so a
# round trip keeps the pin.


def _playlist_tracks(store, pl: dict) -> list[tuple[dict, int]]:
    """``(track, wire)`` for each entry of a normal playlist that still
    resolves, in order (a bare entry: wire 0, the file's default tune)."""
    raw = pl.get("track_ids") or []
    ids, subs = [], []
    for e in raw:
        if isinstance(e, dict):
            ids.append(e.get("id"))
            subs.append(_safe_int(e.get("subsong")))
        else:
            ids.append(e)
            subs.append(0)
    out = []
    for tr, n in zip(store.get_tracks_batch(ids), subs):
        if tr:
            out.append((tr, n if 0 < n < _safe_int(tr.get("subsongs")) else 0))
    return out


def _tune_duration(t: dict, n: int) -> int:
    """A tune's length as its Child reports it: the bare id's duration for
    wire 0 (the default tune), else the tune's HVSC length (0 when
    unknown)."""
    if not n:
        return _safe_int(_sx.default_duration(t), round_=True)
    return _safe_int(_sx.wire_length(t, n), round_=True)


def _playlist_entry(store, raw: str):
    """A Subsonic song id → a native playlist entry: a tune id becomes
    ``{id, subsong}`` (``~0``, the default tune: the bare id), anything else
    stays as sent."""
    tid, n = _split_id(store, raw)
    if tid == raw:
        return raw
    return {"id": tid, "subsong": n} if n else tid


@_route("/getPlaylists")
@_route("/getPlaylists.view")
@_wrap
async def get_playlists(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    pls = []
    # Owner-filtered: a user sees only their own playlists plus any
    # legacy/shared ones (owner_user_id None) so existing single-user
    # libraries keep working until they're explicitly migrated.
    for pl in store.list_playlists_for_user(user.id):
        owner_id = pl.get("owner_user_id")
        owner = get_user_store().get(owner_id) if owner_id and owner_id != user.id else None
        owner_name = user.username if owner_id == user.id else (
            owner.username if owner is not None else "shared")
        row = {
            "id":         pl["id"],
            "name":       pl["name"],
            "owner":      owner_name,
            "public":     owner_id is None,
            "created":    _iso_req(pl.get("created_at"), pl.get("updated_at")),
            "changed":    _iso_req(pl.get("updated_at"), pl.get("created_at")),
        }
        if pl.get("query"):
            # A smart playlist is evaluated only when opened (getPlaylist is
            # authoritative) — running every saved search here would make
            # the list cost N searches.
            row["songCount"] = 0
            row["duration"] = 0
        else:
            # The same entries / durations getPlaylist reports: O(n) dict lookups.
            pairs = _playlist_tracks(store, pl)
            row["songCount"] = len(pairs)
            row["duration"] = sum(_tune_duration(x, n) for x, n in pairs)
        pls.append(row)
    pls.sort(key=lambda x: x.get("changed") or "", reverse=True)
    return _ok({"playlists": {"playlist": pls}}, fmt=f)


@_route("/getPlaylist")
@_route("/getPlaylist.view")
@_wrap
async def get_playlist(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    pl = store._playlists.get(id)
    if not pl:
        raise _SubsonicError(70, "Playlist not found.")
    # Owner check — return 70 (not 50) so we don't reveal existence to
    # someone who shouldn't see the playlist at all.
    owner_id = pl.get("owner_user_id")
    if owner_id is not None and owner_id != user.id:
        raise _SubsonicError(70, "Playlist not found.")
    ctx = _song_ctx(store, user)
    if pl.get("query"):
        # Smart playlist — evaluate the saved search live (same engine as the
        # native API) so Subsonic clients see the computed tracks too.
        from soniqboom.api.search import run_search
        _results = await run_search(pl["query"], limit=500)
        entry_tracks = [r.model_dump() if hasattr(r, "model_dump") else r for r in _results]
        entries = [_track_to_song(t_, ctx) for t_ in entry_tracks if t_]
    else:
        # Entries pinned to one tune ({id, subsong}) come back as tune ids.
        entries = []
        for tr, n in _playlist_tracks(store, pl):
            entries.append(_tune_child(_track_to_song(tr, ctx), tr, n, ctx))
    return await _ok_async({
        "playlist": {
            "id":        pl["id"],
            "name":      pl["name"],
            "owner":     user.username if owner_id == user.id else "shared",
            "public":    owner_id is None,
            "songCount": len(entries),
            "duration":  sum(e.get("duration") or 0 for e in entries),
            "created":   _iso_req(pl.get("created_at"), pl.get("updated_at")),
            "changed":   _iso_req(pl.get("updated_at"), pl.get("created_at")),
            "entry":     entries,
        },
    }, fmt=f, n_hint=len(entries))


@_route("/createPlaylist")
@_route("/createPlaylist.view")
@_wrap
async def create_playlist(
    request: Request,
    name: str | None = Query(default=None),
    playlistId: str | None = Query(default=None),
    songId: list[str] = Query(default_factory=list),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if user.role == "readonly":
        raise _SubsonicError(50, "Your account is read-only. Ask an admin to upgrade you to 'edit' to manage playlists.")
    from soniqboom.core.data import create_playlist as _create, update_playlist as _update
    store = get_store()
    if playlistId:
        # Owner check before mutating someone else's playlist
        existing = store._playlists.get(playlistId)
        if not existing:
            raise _SubsonicError(70, "Playlist not found.")
        owner_id = existing.get("owner_user_id")
        if owner_id is not None and owner_id != user.id and user.role != "admin":
            raise _SubsonicError(50, "You can only edit your own playlists.")
        # Spec: with a playlistId the songs REPLACE the playlist's.
        upd: dict = {"track_ids": [_playlist_entry(store, x) for x in songId]}
        if name:
            upd["name"] = name
        await _update(playlistId, upd)
        pl = store._playlists.get(playlistId)
    else:
        # Subsonic spec: createPlaylist needs either playlistId (update) or a
        # name (create).  With neither, return error 10 rather than silently
        # fabricating a "New playlist" — a param-less request must not mutate.
        if not name:
            raise _SubsonicError(10, "Required parameter 'name' is missing.")
        # Only ids that exist are kept (updatePlaylist prunes the same way).
        entries = [_playlist_entry(store, x) for x in songId]
        entries = [e for e in entries
                   if store.get_track(e["id"] if isinstance(e, dict) else e)]
        pl = await _create(name, track_ids=entries, owner_user_id=user.id)
    if not pl:
        raise _SubsonicError(70, "Playlist not found.")
    return await get_playlist(
        request, id=pl["id"], sb_session=sb_session,
        u=u, p=p, s=s, t=t, f=f,
    )


@_route("/updatePlaylist")
@_route("/updatePlaylist.view")
@_wrap
async def update_playlist(
    request: Request,
    playlistId: str = Query(...),
    name: str | None = Query(default=None),
    songIdToAdd:    list[str] = Query(default_factory=list),
    songIndexToRemove: list[int] = Query(default_factory=list),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if user.role == "readonly":
        raise _SubsonicError(50, "Your account is read-only. Ask an admin to upgrade you to 'edit' to manage playlists.")
    store = get_store()
    pl = store._playlists.get(playlistId)
    if not pl:
        raise _SubsonicError(70, "Playlist not found.")
    owner_id = pl.get("owner_user_id")
    if owner_id is not None and owner_id != user.id and user.role != "admin":
        raise _SubsonicError(50, "You can only edit your own playlists.")
    ids = list(pl.get("track_ids") or [])
    # ``songIndexToRemove`` indexes the list getPlaylist SHOWED — entries
    # whose track still exists — so map it onto the stored list (which may
    # still hold a since-deleted id) before removing, by descending index so
    # earlier indices stay valid.
    shown = [i for i, e in enumerate(ids)
             if store.get_track(e.get("id") if isinstance(e, dict) else e)]
    for idx in sorted(set(songIndexToRemove), reverse=True):
        if 0 <= idx < len(shown):
            ids.pop(shown[idx])
    ids.extend(_playlist_entry(store, x) for x in songIdToAdd)
    from soniqboom.core.data import update_playlist as _update
    upd: dict = {"track_ids": ids}
    if name is not None and name.strip():
        upd["name"] = name.strip()
    await _update(playlistId, upd)
    return _ok(fmt=f)


@_route("/deletePlaylist")
@_route("/deletePlaylist.view")
@_wrap
async def delete_playlist(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if user.role == "readonly":
        raise _SubsonicError(50, "Your account is read-only. Ask an admin to upgrade you to 'edit' to manage playlists.")
    store = get_store()
    pl = store._playlists.get(id)
    if not pl:
        return _ok(fmt=f)
    owner_id = pl.get("owner_user_id")
    if owner_id is not None and owner_id != user.id and user.role != "admin":
        raise _SubsonicError(50, "You can only delete your own playlists.")
    from soniqboom.core.data import delete_playlist as _delete
    await _delete(id)
    return _ok(fmt=f)


# ── Empty-but-compliant endpoints (clients call these on first connect) ─────

@_route("/getUser")
@_route("/getUser.view")
@_wrap
async def get_user(
    request: Request,
    username: str | None = Query(default=None),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    me = _require_user(request, sb_session, u, p, s, t)
    target = username or me.username
    if target.lower() != me.username.lower() and me.role != "admin":
        raise _SubsonicError(50, "Not authorised to view this user.")
    target_user = get_user_store().get_by_username(target)
    if target_user is None:
        raise _SubsonicError(70, "User not found.")
    return _ok({"user": _user_payload(target_user)}, fmt=f)


@_route("/getStarred")
@_route("/getStarred.view")
@_route("/getStarred2")
@_route("/getStarred2.view")
@_wrap
async def get_starred(
    request: Request,
    musicFolderId: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    uid = getattr(user, "id", None)
    state = _get_state()
    is_v2 = "starred2" in request.url.path.lower()
    key = "starred2" if is_v2 else "starred"

    def _newest_first(m: dict) -> list:
        return sorted(m.items(), key=lambda kv: -_ts_num(kv[1]))

    # Stars are stored by id and resolved lazily: an id whose track / album /
    # artist no longer exists is skipped, never an error.  O(#starred).
    # ``musicFolderId``: only what has music in that scan root.
    root = _folder_hash(store, musicFolderId)
    ctx = _song_ctx(store, user)
    songs = []
    for tid, _ts in _newest_first(state.starred(uid, "song")):
        tr = store.get_track(tid)
        if tr and (not root or tr.get("scan_root_hash") == root):
            songs.append(_track_to_song(tr, ctx))
    st_albums = state.starred(uid, "album")
    albums = []
    if st_albums:
        cat = _catalogue(store)
        await _prepare_folder_view(store, cat, root)
        in_root = _folder_view(store, cat, root)[0] if root else None
        mapper = _album_id3 if is_v2 else _album_child
        rt_albums = state.ratings(uid, "album")
        for aid, _ts in _newest_first(st_albums):
            e = _resolve_album_entry(store, aid, cat)
            if e is not None and (in_root is None or e.id in in_root):
                albums.append(mapper(e, st_albums, rt_albums))
    st_artists = state.starred(uid, "artist")
    artists = []
    if st_artists:
        cat = _catalogue(store)
        union = _artist_union(store)
        await _prepare_folder_view(store, cat, root)
        in_root = _folder_view(store, cat, root)[1] if root else None
        rt_artists = state.ratings(uid, "artist")
        for arid, ts in _newest_first(st_artists):
            low = union.by_id.get(arid)
            if low is None or (in_root is not None and low not in in_root):
                continue
            row = {"id": arid, "name": union.display[low],
                   "albumCount": cat.album_count(low),
                   "coverArt": arid, "starred": _iso(ts)}
            if rt_artists.get(arid):
                row["userRating"] = rt_artists[arid]
            artists.append(row)
    return await _ok_async({key: {"artist": artists, "album": albums, "song": songs}}, fmt=f,
                           n_hint=len(songs) + len(albums) + len(artists))


@_route("/getGenres")
@_route("/getGenres.view")
@_wrap
async def get_genres(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    rows = store.aggregate_genres()
    # albumCount = albums with ≥1 track carrying the genre (catalogue
    # membership, incl. folder albums when on) — the same set byGenre lists.
    ga = _catalogue(store).genre_albums
    return _ok({
        "genres": {
            "genre": [
                # ``_text``: the spec puts the genre name in the element BODY in
                # XML (<genre songCount=".." albumCount="..">Rock</genre> —
                # DSub / Amperfy read it there) and in ``value`` in JSON.
                {"_text": r["genre"], "songCount": r["count"],
                 "albumCount": len(ga.get(r["genre"].strip().lower(), ()))}
                for r in rows
            ],
        },
    }, fmt=f)


# ── Lyrics (OpenSubsonic songLyrics extension) ───────────────────────────────

def _parse_lrc(text: str) -> tuple[bool, list[dict]]:
    """Split lyrics into OpenSubsonic ``structuredLyrics`` lines.

    Returns ``(synced, lines)`` where each line is ``{"start": ms, "_text": …}``
    for a timestamped LRC line or ``{"_text": …}`` for plain text.  ``_text``
    becomes element character data in XML and a ``value`` field in JSON."""
    import re
    # Minutes allow up to 3 digits — long-form tracks (DJ mixes, audiobooks,
    # tracker "songs") can exceed 99 minutes, and a 2-digit cap would treat
    # ``[123:45.67]`` as plain text and drop the sync for the whole line.
    ts_re = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
    # LRC ID tags ([ar:…], [ti:…], [offset:…], [length:…]) are metadata, not
    # lyric lines.
    id_tag_re = re.compile(r"^\s*\[[A-Za-z#]+:[^\]]*\]\s*$")
    lines: list[dict] = []
    synced = False
    # Cap the emitted line count — a crafted .lrc with many timestamps per line
    # (``[00:01][00:02]…text``) would otherwise amplify one line into hundreds
    # in the response.  A real song is well under this.
    _MAX_LINES = 5000
    for raw in text.splitlines():
        if len(lines) >= _MAX_LINES:
            break
        stamps = list(ts_re.finditer(raw))
        if not stamps and id_tag_re.match(raw):
            continue
        body = ts_re.sub("", raw).strip()
        if stamps:
            synced = True
            for m in stamps:
                if len(lines) >= _MAX_LINES:
                    break
                ms = (int(m.group(1)) * 60000 + int(m.group(2)) * 1000
                      + int((m.group(3) or "0").ljust(3, "0")[:3]))
                lines.append({"start": ms, "_text": body})
        else:
            lines.append({"_text": body})
    if synced:
        # Stable-sort by timestamp, but an untimed line inherits the previous
        # line's start so a trailing/interleaved plain line keeps its position
        # instead of being hoisted to the top (where a plain "start" of 0 lands).
        keyed = []
        last_ms = 0
        for ln in lines:
            if "start" in ln:
                last_ms = ln["start"]
                keyed.append((ln["start"], ln))
            else:
                keyed.append((last_ms, ln))
        keyed.sort(key=lambda kv: kv[0])     # stable: equal keys keep input order
        lines = [ln for _, ln in keyed]
    return synced, lines


async def _lyrics_for_track_id(track_id: str) -> dict | None:
    """Resolve lyrics for a track id via the shared native resolver
    (embedded tags → LRCLib).  Returns ``{text, synced, artist, title}`` or
    ``None`` when the track or its lyrics are unavailable."""
    t = get_store().get_track(track_id)
    if not t:
        return None
    # Chip / tracker / Amiga formats carry no lyrics a tag reader can find,
    # and an online lookup by "artist + title" for them only ever misses —
    # ~1 s per call spent sending tune titles to third parties.  MIDI keeps
    # the full path (karaoke files can carry lyrics).
    from soniqboom.core.retro import chip_family, is_retro_format
    fmt = t.get("format")
    if is_retro_format(fmt) and chip_family(fmt) != "midi":
        return None
    try:
        from soniqboom.api.tracks import get_lyrics as _impl
        res = await _impl(track_id)
    except Exception:
        res = None
    text = (res or {}).get("lyrics")
    if not text:
        return None
    # Bound the raw text so an oversized embedded-lyrics blob can't produce an
    # unbounded response (the line count is separately capped in _parse_lrc).
    if len(text) > 200_000:
        text = text[:200_000]
    return {
        "text": text,
        "synced": bool((res or {}).get("synced")),
        "artist": t.get("artist") or t.get("album_artist") or "",
        "title": t.get("title") or "",
    }


@_route("/getLyrics")
@_route("/getLyrics.view")
@_wrap
async def get_lyrics_ss(
    request: Request,
    artist: str = Query(default=""),
    title: str = Query(default=""),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    # Legacy form: match by artist+title.  Modern clients use
    # getLyricsBySongId instead, so this linear scan runs only for old ones.
    a = (artist or "").strip().lower()
    ti = (title or "").strip().lower()
    tid = None
    if a or ti:
        # Candidates from the tag / word indexes instead of a library scan
        # (the old scan ran in a worker thread, reading the non-thread-safe
        # store off the event loop): the artist's tracks, or the tracks whose
        # title words match — then the exact predicate.  O(candidates).
        store = get_store()
        if a:
            cands = _artist_track_ids(store, a)
        elif any(ch.isalnum() for ch in ti):
            cands = store.filter_track_ids(query=ti)
        else:
            cands = ()           # no words to match on: nothing to find
        hits = []
        for cid in cands:
            cand = store.get_track(cid)
            if not cand:
                continue
            if ti and (cand.get("title") or "").strip().lower() != ti:
                continue
            if a and (cand.get("artist") or cand.get("album_artist") or "").strip().lower() != a:
                continue
            hits.append(cid)
        tid = min(hits) if hits else None        # deterministic pick
    lyr = await _lyrics_for_track_id(tid) if tid else None
    if not lyr:
        # Spec: always return a <lyrics> element (empty body) so clients
        # don't treat "no lyrics" as a protocol error.
        return _ok({"lyrics": {"artist": artist, "title": title, "_text": ""}}, fmt=f)
    # The legacy element carries plain text: synced (LRC) lyrics lose their
    # [mm:ss.xx] stamps and ID tags here (getLyricsBySongId keeps the timing).
    text = lyr["text"]
    synced, lines = _parse_lrc(text)
    if synced or lyr.get("synced"):
        text = "\n".join(ln["_text"] for ln in lines)
    return _ok({"lyrics": {"artist": lyr["artist"], "title": lyr["title"],
                           "_text": text}}, fmt=f)


@_route("/getLyricsBySongId")
@_route("/getLyricsBySongId.view")
@_wrap
async def get_lyrics_by_song_id(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    lyr = await _lyrics_for_track_id(id)
    if not lyr:
        # Spec: lyricsList.structuredLyrics is a required array — return it
        # empty, not a bare {}, so typed clients don't null-deref on the
        # (common) no-lyrics path.
        return _ok({"lyricsList": {"structuredLyrics": []}}, fmt=f)
    synced, lines = _parse_lrc(lyr["text"])
    return _ok({"lyricsList": {"structuredLyrics": [{
        "displayArtist": lyr["artist"],
        "displayTitle":  lyr["title"],
        "lang":          "xxx",       # unknown; OpenSubsonic uses ISO-639 or "xxx"
        "offset":        0,
        "synced":        synced,
        "line":          lines,
    }]}}, fmt=f)


# ── Similar songs (core getSimilarSongs / getSimilarSongs2) ──────────────────
# Both accept a song, album or artist id (artist / album "radio").  An album or
# artist is reduced to a few seed tracks; each seed runs the audio-envelope
# similarity engine (same one as the web "More like this") and the results are
# merged by best score, seeds excluded.

_SIMILAR_MAX_SEEDS = 5


def _rank_key(store):
    """Top-songs order: play count desc, rating desc, title, id."""
    plays = getattr(store, "_play_stats", None) or {}
    ratings = getattr(store, "_ratings", None) or {}

    def _rank(tid: str):
        tr = store.get_track(tid) or {}
        return (-((plays.get(tid) or {}).get("count") or 0), -(ratings.get(tid) or 0),
                (tr.get("title") or "").lower(), tid)
    return _rank


def _similar_seeds(store, raw: str) -> list[dict]:
    import heapq
    if raw.startswith("ar:"):
        hit = _decode_artist(raw, store)
        if hit is None:
            raise _SubsonicError(70, "Artist not found.")
        tids = _artist_track_ids(store, hit[0]) if hit[0] else set()
        top = heapq.nsmallest(_SIMILAR_MAX_SEEDS, tids, key=_rank_key(store))
        seeds = [tr for tr in store.get_tracks_batch(top) if tr]
    elif raw.startswith(("al:", _sx.FOLDER_PREFIX)):
        _album, tracks = _album_and_tracks(store, raw)
        if not tracks:
            raise _SubsonicError(70, "Album not found.")
        if len(tracks) <= _SIMILAR_MAX_SEEDS:
            seeds = list(tracks)
        else:
            # Most-played first, then spread across the tracklist so an album
            # nobody has played yet isn't represented by its intro alone.
            by_id = {tr["id"]: tr for tr in tracks}
            played = [i for i in heapq.nsmallest(_SIMILAR_MAX_SEEDS, by_id, key=_rank_key(store))
                      if ((getattr(store, "_play_stats", None) or {}).get(i) or {}).get("count")]
            step = len(tracks) / _SIMILAR_MAX_SEEDS
            spread = [tracks[int(k * step)]["id"] for k in range(_SIMILAR_MAX_SEEDS)]
            order = list(dict.fromkeys(played + spread))[:_SIMILAR_MAX_SEEDS]
            seeds = [by_id[i] for i in order]
    else:
        tr = store.get_track(raw)
        seeds = [tr] if tr else []
    if not seeds:
        raise _SubsonicError(70, "Song not found.")
    return seeds


async def _similar_song_scored(raw_id: str, count: int) -> list[tuple[dict, float]]:
    """Seed the audio-envelope similarity engine from a song / album / artist
    id and return ``(track, score)`` pairs, best first — ``score`` in 0..1
    (relative: the best match of a seed is 1.0).  Store reads stay on the
    event loop, with a yield between seeds (each seed's candidate pool +
    sample overlap is one ~20 ms step, kept together so it can't race an
    index swap); the scoring for ALL seeds runs in one worker-thread hop."""
    from soniqboom.core.data import get_all_ratings
    from soniqboom.core.similar import find_similar
    store = get_store()
    seeds = _similar_seeds(store, raw_id)
    k = max(1, min(count, 100))
    ratings = await get_all_ratings()
    jobs = []
    for i, seed in enumerate(seeds):
        if i:
            await asyncio.sleep(0)                # let other requests in between seeds
        candidates = store.similar_candidates(seed)   # on-loop: atomic vs scanner, cheap
        jobs.append((seed, candidates, store.retro_sample_jaccard(seed, candidates)))
    waves = store.waveforms_view()

    def _run() -> list[list[dict]]:
        return [find_similar(seed, cands, waves, ratings=ratings or {}, k=k,
                             sample_jaccard=sj) for seed, cands, sj in jobs]
    results = await asyncio.to_thread(_run)
    seed_ids = {sd["id"] for sd in seeds}
    best: dict[str, tuple[float, dict]] = {}
    for res in results:
        for r in res:
            tr = r.get("track")
            if not tr or (len(seeds) > 1 and tr.get("id") in seed_ids):
                continue
            sc = r.get("score")
            sc = float(sc) if isinstance(sc, (int, float)) and math.isfinite(sc) else 0.0
            sc = min(1.0, max(0.0, sc))
            cur = best.get(tr["id"])
            if cur is None or sc > cur[0]:
                best[tr["id"]] = (sc, tr)
    if len(seeds) == 1:
        # One seed: the engine's own order (stable for equal scores).
        order = [r["track"]["id"] for r in results[0] if r.get("track")]
        return [(best[i][1], best[i][0]) for i in dict.fromkeys(order) if i in best][:k]
    ranked = sorted(best.values(), key=lambda v: (-v[0], v[1].get("id") or ""))
    return [(tr, sc) for sc, tr in ranked[:k]]


async def _similar_song_tracks(raw_id: str, count: int) -> list[dict]:
    """``_similar_song_scored`` without the scores (getSimilarSongs / 2)."""
    return [tr for tr, _sc in await _similar_song_scored(raw_id, count)]


@_route("/getSimilarSongs")
@_route("/getSimilarSongs.view")
@_route("/getSimilarSongs2")
@_route("/getSimilarSongs2.view")
@_wrap
async def get_similar_songs(
    request: Request,
    id: str = Query(...),
    count: int = Query(default=50),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    tracks = await _similar_song_tracks(_split_id(get_store(), id)[0], count)
    ctx = _song_ctx(get_store(), user)
    songs = [_track_to_song(tr, ctx) for tr in tracks]
    is_v2 = "similarsongs2" in request.url.path.lower()
    key = "similarSongs2" if is_v2 else "similarSongs"
    return _ok({key: {"song": songs}}, fmt=f)


# ── OpenSubsonic sonicSimilarity ─────────────────────────────────────────────
# getSonicSimilarTracks / findSonicPath answer ``sonicMatch`` entries — a song
# Child plus a similarity — from the same audio-envelope engine as
# getSimilarSongs (core/similar.py).  Songs only (an album / artist seed is
# getSimilarSongs' job).  The engine's score only RANKS candidates within one
# result set (its top hit is always 1.0), not the absolute "1.0 = the same
# song" measure the spec defines, so every match reports -1 — the spec's
# "similarity not supported" — and the ORDER carries the ranking.

def _sonic_match(tr: dict, ctx: _SongCtx) -> dict:
    return {"entry": _track_to_song(tr, ctx), "similarity": -1}


@_route("/getSonicSimilarTracks")
@_route("/getSonicSimilarTracks.view")
@_wrap
async def get_sonic_similar_tracks(
    request: Request,
    id: str = Query(...),
    count: int = Query(default=10),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    tid = _split_id(store, id)[0]
    if tid.startswith(("ar:", "al:", _sx.FOLDER_PREFIX)) or not store.get_track(tid):
        raise _SubsonicError(70, "Song not found.")
    pairs = await _similar_song_scored(tid, max(1, min(int(count or 10), 100)))
    ctx = _song_ctx(store, user)
    return _ok({"sonicMatch": [_sonic_match(tr, ctx) for tr, _sc in pairs]}, fmt=f)


# Path search bounds: the end song's neighbourhood scored once, then one
# bounded scoring per step (each in a worker thread).
_SONIC_PATH_MAX = 50
_SONIC_END_K = 500
_SONIC_STEP_K = 40


@_route("/findSonicPath")
@_route("/findSonicPath.view")
@_wrap
async def find_sonic_path(
    request: Request,
    startSongId: str = Query(...),
    endSongId: str = Query(...),
    count: int = Query(default=25),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """A path of songs from ``startSongId`` to ``endSongId`` through audio
    similarity: a greedy walk that, at each step, takes the unvisited
    neighbour of the current song that best balances "like the current
    song" against "like the end song" — the pull towards the end growing
    along the path.  Starts with the start song, ends with the end song, no
    duplicates, at most ``count`` entries (≤ 50).  Each entry's similarity
    is -1 (not supported — see ``_sonic_match``)."""
    user = _require_user(request, sb_session, u, p, s, t)
    from soniqboom.core.data import get_all_ratings
    from soniqboom.core.similar import find_similar
    store = get_store()
    start = store.get_track(_split_id(store, startSongId)[0])
    end = store.get_track(_split_id(store, endSongId)[0])
    if not start or not end:
        raise _SubsonicError(70, "Song not found.")
    n = max(2, min(int(count or 25), _SONIC_PATH_MAX))
    ratings = await get_all_ratings() or {}
    waves = store.waveforms_view()

    def _score(seed: dict, cands: list, sj, k: int) -> list[dict]:
        return find_similar(seed, cands, waves, ratings=ratings, k=k, sample_jaccard=sj)

    cands = store.similar_candidates(end)
    sj = store.retro_sample_jaccard(end, cands)
    to_end = {r["track"]["id"]: float(r.get("score") or 0.0)
              for r in await asyncio.to_thread(_score, end, cands, sj, _SONIC_END_K)
              if r.get("track")}
    path: list[dict] = [start]
    seen = {start["id"], end["id"]}
    cur = start
    steps = n - 2
    if start["id"] != end["id"]:
        for step in range(steps):
            await asyncio.sleep(0)
            cands = store.similar_candidates(cur)
            sj = store.retro_sample_jaccard(cur, cands)
            res = await asyncio.to_thread(_score, cur, cands, sj, _SONIC_STEP_K)
            nbrs = [(r["track"], float(r.get("score") or 0.0)) for r in res
                    if r.get("track") and r["track"].get("id")]
            if any(tr["id"] == end["id"] for tr, _sc in nbrs):
                break                              # the end is one step away
            w = 0.3 + 0.7 * (step + 1) / max(1, steps)
            best = None
            for tr, sc in nbrs:
                if tr["id"] in seen:
                    continue
                v = (1 - w) * sc + w * to_end.get(tr["id"], 0.0)
                if best is None or v > best[0]:
                    best = (v, tr)
            if best is None:
                break
            nxt = best[1]
            path.append(nxt)
            seen.add(nxt["id"])
            cur = nxt
        path.append(end)
    ctx = _song_ctx(store, user)
    return _ok({"sonicMatch": [_sonic_match(tr, ctx) for tr in path]}, fmt=f)


# ── Jukebox (Subsonic jukeboxControl) ─────────────────────────────────────────

@_route("/jukeboxControl")
@_route("/jukeboxControl.view")
@_wrap
async def jukebox_control(
    request: Request,
    action: str = Query(...),
    index: str | None = Query(default=None),
    offset: str | None = Query(default=None),
    id: list[str] | None = Query(default=None),
    gain: str | None = Query(default=None),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    # Mutating control surface — refuse cookie-only auth (CSRF) and gate on the
    # same roles that advertise jukeboxRole=true in getUser.
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if user.role not in ("admin", "edit"):
        raise _SubsonicError(50, "Not authorised to control the jukebox.")

    from soniqboom.core.jukebox import get_jukebox
    jb = get_jukebox()
    act = (action or "").strip().lower()

    # Parse the numeric params by hand (they're typed str above) so a bad value
    # returns a Subsonic error envelope (code 10) instead of FastAPI's raw 422 —
    # keeps every client-facing error in one shape.
    def _num(name: str, val: str | None, cast):
        if val is None:
            return None
        try:
            return cast(val)
        except (TypeError, ValueError):
            raise _SubsonicError(10, f"jukeboxControl {name} must be a number.")
    idx  = _num("index", index, int)
    offs = _num("offset", offset, float)
    gn   = _num("gain", gain, float)
    store = get_store()

    def _song_ids(raw_ids: list[str] | None) -> list[str]:
        # Queue FILE ids only: a tune id (``<id>~<n>``) is queued as its file
        # — the Jukebox room plays a file's default tune, so listing the tune
        # would name a tune it never plays — and ids that don't resolve are
        # dropped (every queued item must map to exactly one listed entry).
        out = []
        for raw in raw_ids or ():
            tid = _split_id(store, raw)[0]
            if store.get_track(tid):
                out.append(tid)
        if raw_ids and not out:
            raise _SubsonicError(70, "Song not found.")
        return out

    def _listed(q: list[str]) -> list[int]:
        """Queue positions of the entries ``get`` lists (a track deleted
        since it was queued is not listed)."""
        return [k for k, tid in enumerate(q) if store.get_track(tid)]

    def _queue_index(i: int) -> int:
        """A client index (into the listed entries) → the queue position."""
        q = jb.queue_ids()
        vis = _listed(q)
        if len(vis) == len(q) or i < 0:
            return i
        return vis[i] if i < len(vis) else len(q)

    if act == "set":
        jb.set_queue(_song_ids(id))
    elif act == "add":
        jb.add(_song_ids(id))
    elif act == "clear":
        jb.clear()
    elif act == "remove":
        if idx is None:
            raise _SubsonicError(10, "jukeboxControl remove requires index.")
        jb.remove(_queue_index(idx))
    elif act == "shuffle":
        jb.shuffle()
    elif act == "skip":
        if idx is None:
            raise _SubsonicError(10, "jukeboxControl skip requires index.")
        jb.skip(_queue_index(idx), offs or 0.0)
    elif act == "start":
        jb.start()
    elif act == "stop":
        jb.stop()
    elif act == "setgain":
        jb.set_gain(gn if gn is not None else 1.0)
    elif act in ("get", "status"):
        pass
    else:
        raise _SubsonicError(10, f"Unknown jukebox action: {action}")

    # Realise the change as audio: push the new state to the Jukebox multiroom
    # room so a browser joined to it plays it (best-effort; a read never emits).
    if act not in ("get", "status"):
        try:
            from soniqboom.api.multiroom import notify_jukebox_room
            await notify_jukebox_room()
        except Exception:
            pass

    st = jb.status()
    q = jb.queue_ids()
    if act == "get":
        # Full playlist view — resolve queued ids to Child entries, with
        # ``currentIndex`` counted in the LISTED entries (a queued track
        # deleted since is skipped, so the index still names the playing one;
        # -1 when that one is gone).
        ctx = _song_ctx(store, user)
        entries: list[dict] = []
        cur = -1
        for k, tid in enumerate(q):
            tr = store.get_track(tid)
            if tr:
                if k == st["currentIndex"]:
                    cur = len(entries)
                entries.append(_track_to_song(tr, ctx))
        return _ok({"jukeboxPlaylist": {
            "currentIndex": cur,
            "playing":      st["playing"],
            "gain":         st["gain"],
            "position":     st["position"],
            "entry":        entries,
        }}, fmt=f)
    # Every other action returns the compact status object — its index in
    # the same listed-entries terms as ``get``.
    vis = _listed(q)
    if len(vis) != len(q):
        st = {**st, "currentIndex": vis.index(st["currentIndex"])
              if st["currentIndex"] in vis else -1}
    return _ok({"jukeboxStatus": st}, fmt=f)


# ── Folder browsing (getMusicDirectory) ──────────────────────────────────────
# Folder-mode clients (DSub's default, Substreamer, older players) browse
# getIndexes → getMusicDirectory(artist) → getMusicDirectory(album).  Ids are
# the same ``ar:`` / ``al:`` / ``fa:`` ids the ID3 endpoints use, so a client
# can mix both modes.  An artist directory lists its albums as child dirs PLUS
# its album-less songs (retro archives) as direct children, so a library of
# untagged SID / tracker files is browsable without the folder-albums setting.

@_route("/getMusicDirectory")
@_route("/getMusicDirectory.view")
@_wrap
async def get_music_directory(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    uid = getattr(user, "id", None)
    state = _get_state()
    ctx = _song_ctx(store, user)
    if id.startswith("ar:"):
        hit = _decode_artist(id, store)
        if hit is None:
            raise _SubsonicError(70, "Directory not found.")
        low, name = hit
        cat = _catalogue(store)
        st_albums = state.starred(uid, "album")
        rt_albums = state.ratings(uid, "album")
        own = cat.albums_for_artist(low)
        children = [_album_child(e, st_albums, rt_albums) for e in own]
        if low == "":
            # [Unknown Artist]: album-less tracks as folder directories.
            dirs, no_dir = _unknown_folder_entries(store, cat)
            shown = {e.id for e in own}
            children.extend(_album_child(e, st_albums, rt_albums)
                            for e in dirs if e.id not in shown)
            children.extend(_track_to_song(tr, ctx) for tr in no_dir)
        else:
            listed = {e.id for e in own}
            children.extend(_track_to_song(tr, ctx)
                            for tr in _loose_tracks(store, low, cat, listed))
        directory: dict = {"id": id, "name": name, "child": children}
        st = state.starred(uid, "artist").get(id)
        rt = state.ratings(uid, "artist").get(id)
    elif id.startswith("al:") or id.startswith(_sx.FOLDER_PREFIX):
        album, tracks = _album_and_tracks(store, id)
        if album is None:
            raise _SubsonicError(70, "Directory not found.")
        if id.startswith(_sx.FOLDER_PREFIX):
            ctx = _song_ctx(store, user, album=(album["id"], album["name"]))
        directory = {"id": id, "name": album["name"],
                     "child": await _songs_with_tunes(tracks, ctx)}
        if album.get("artistId"):
            directory["parent"] = album["artistId"]
        st = state.starred(uid, "album").get(id)
        rt = state.ratings(uid, "album").get(id)
    else:
        raise _SubsonicError(70, "Directory not found.")
    if st:
        directory["starred"] = _iso(st)
    if rt:
        directory["userRating"] = rt
    return await _ok_async({"directory": directory}, fmt=f, n_hint=len(directory["child"]),
                           splice=("directory", "child"))


# ── getSongsByGenre ──────────────────────────────────────────────────────────
# The genre predicate is the store's own (``filter_track_ids(genre=…)`` →
# ``_candidate_ids``, identical to ``filter_tracks(genre=…)``): O(genre size)
# set lookup.  The newest-first order is built once per genre per library
# snapshot (same scan debounce as the catalogue) so paging is a slice and stays
# stable across pages.  A small genre is sorted directly (O(k log k)); a big
# one (a retro "Module" genre is half the library) is read off the store's
# pre-sorted added-at index with a C-speed membership filter instead — ~25 ms
# vs ~130 ms for a Python-keyed sort of 127k ids.

_GENRE_SONGS_CACHE: dict = {"store": None, "seq": None, "built_at": 0.0, "lists": {},
                             "inflight": {}}
_GENRE_SONGS_MAX = 32


# Ids walked per step of a big genre's ordering: the event loop gets a turn
# between steps (a 127k-id genre was one 34-42 ms walk on the loop).
_GENRE_STEP = 20_000


async def _genre_song_ids(store, genre: str, root_hash: str | None = None) -> list[str]:
    """A genre's song ids, newest first (``root_hash``: one music folder's
    share, same order) — memoised per library snapshot.  A cold build walks
    in ``_GENRE_STEP`` slices with the event loop getting a turn between
    them; concurrent requests for the same cold list share one build."""
    c = _GENRE_SONGS_CACHE
    seq = _catalog_seq(store)
    fresh = c.get("store") is store and (
        c["seq"] == seq or (time.monotonic() - c["built_at"]) < _CACHE_DEBOUNCE_SEC)
    if not fresh:
        c.update(store=store, seq=seq, built_at=time.monotonic(), lists={}, inflight={})
    lists = c["lists"]
    g = genre.strip().lower()
    key = (g, root_hash) if root_hash else g
    ids = lists.get(key)
    if ids is not None:
        return ids
    inflight = c.setdefault("inflight", {})
    task = inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_build_genre_ids(store, genre, root_hash))
        inflight[key] = task

        def _done(t, key=key, inflight=inflight, lists=lists):
            if inflight.get(key) is t:
                inflight.pop(key, None)
            if t.cancelled() or t.exception() is not None:
                return
            # Only into the generation it was built for: a reset meanwhile
            # (a newer snapshot) must not receive stale ids.
            if c["lists"] is lists:
                if len(lists) >= _GENRE_SONGS_MAX:
                    lists.pop(next(iter(lists)))
                lists[key] = t.result()
        task.add_done_callback(_done)
    return await asyncio.shield(task)


async def _build_genre_ids(store, genre: str, root_hash: str | None) -> list[str]:
    if root_hash:
        full = await _genre_song_ids(store, genre)
        in_root = _root_track_ids(store, root_hash)     # membership tests only
        out: list[str] = []
        for lo in range(0, len(full), _GENRE_STEP):
            if lo:
                await asyncio.sleep(0)
            out.extend(i for i in full[lo:lo + _GENRE_STEP] if i in in_root)
        return out
    tracks = store._tracks
    ids = store.filter_track_ids(genre=genre.strip())
    idx = store._sorted_added_at
    if len(ids) * 16 <= len(idx):
        # Small genre: sort directly — same order as the index walk (added_at
        # desc, then id desc).
        ids.sort(key=lambda tid: ((tracks.get(tid) or {}).get("added_at") or 0, tid),
                 reverse=True)
        return ids
    gset: set = set()
    for lo in range(0, len(ids), _GENRE_STEP):   # a 127k-id set is ~5 ms in one go
        await asyncio.sleep(0)
        gset.update(ids[lo:lo + _GENRE_STEP])
    snap = idx[:]            # the live index may change (delta-apply) mid-walk
    ordered: list[str] = []
    for hi in range(len(snap), 0, -_GENRE_STEP):
        if hi != len(snap):
            await asyncio.sleep(0)
        ordered.extend(tid for _a, tid in reversed(snap[max(0, hi - _GENRE_STEP):hi])
                       if tid in gset)
    if len(ordered) < len(gset):
        # Tracks with no added_at (or indexed after a deferred batch rebuild)
        # aren't in the sort index — append them, stably.
        seen = set(ordered)
        ordered.extend(sorted((t for t in gset if t not in seen), reverse=True))
    return ordered


@_route("/getSongsByGenre")
@_route("/getSongsByGenre.view")
@_wrap
async def get_songs_by_genre(
    request: Request,
    genre: str = Query(...),
    count: int = Query(10, ge=0),
    offset: int = Query(0, ge=0),
    musicFolderId: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    count = min(count, 500)
    root = _folder_hash(store, musicFolderId)
    page = ((await _genre_song_ids(store, genre, root))[offset:offset + count]
            if genre.strip() else [])
    ctx = _song_ctx(store, user)
    songs = [_track_to_song(tr, ctx) for tr in store.get_tracks_batch(page) if tr]
    return await _ok_async({"songsByGenre": {"song": songs}}, fmt=f, n_hint=len(songs))


# ── Stars / ratings ──────────────────────────────────────────────────────────

_SYNTH_ID_RE = _SYNTH_COVER_RE            # ar: / al: / fa: (incl. a mixed folder's share)


def _split_star_ids(store, ids: list[str], album_ids: list[str],
                    artist_ids: list[str], *, must_exist: bool) -> tuple[list, list, list]:
    """Sort star/unstar params into (songs, albums, artists).  ``id`` may carry
    any kind — folder-mode clients star a directory by its ``id`` — so route
    by prefix.  When starring, every id must resolve now; unstar accepts any
    well-formed id, so a star on something since deleted can still be
    removed.  A tune id (``<track>~<n>``) stars / unstars its file."""
    songs, albums, artists = [], [], []
    for i in ids:
        if i.startswith("ar:"):
            artists.append(i)
        elif i.startswith(("al:", _sx.FOLDER_PREFIX)):
            albums.append(i)
        else:
            i = _split_id(store, i)[0]
            if not must_exist or store.get_track(i):
                songs.append(i)
    albums.extend(album_ids)
    artists.extend(artist_ids)
    albums = [a for a in albums if _SYNTH_ID_RE.match(a) and not a.startswith("ar:")]
    artists = [a for a in artists if _SYNTH_ID_RE.match(a) and a.startswith("ar:")]
    if must_exist and (albums or artists):
        # Only ids that resolve NOW are stored — O(1) each against the cached
        # catalogue (a miss on a stale snapshot retries on a rebuilt one;
        # folder albums come straight from the dir-hash index), so a client
        # can't grow the state file with ids nothing will ever show.
        albums = [a for a in albums if _resolve_album_entry(store, a) is not None]
        artists = [a for a in artists if _decode_artist(a, store) is not None]
    return songs, albums, artists


async def _star_common(request, star: bool, id, albumId, artistId,
                       sb_session, u, p, s, t, f):
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if not (id or albumId or artistId):
        raise _SubsonicError(10, "Required parameter 'id', 'albumId' or 'artistId' is missing.")
    store = get_store()
    songs, albums, artists = _split_star_ids(store, id or [], albumId or [], artistId or [],
                                             must_exist=star)
    state = _get_state()
    op = state.star if star else state.unstar
    for kind, ids in (("song", songs), ("album", albums), ("artist", artists)):
        if ids:
            await op(user.id, kind, ids)
    return _ok(fmt=f)


@_route("/star")
@_route("/star.view")
@_wrap
async def star(
    request: Request,
    id: list[str] = Query(default_factory=list),
    albumId: list[str] = Query(default_factory=list),
    artistId: list[str] = Query(default_factory=list),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    # Mutating — cookie-only auth refused (CSRF), like scrobble / playlists.
    return await _star_common(request, True, id, albumId, artistId,
                              sb_session, u, p, s, t, f)


@_route("/unstar")
@_route("/unstar.view")
@_wrap
async def unstar(
    request: Request,
    id: list[str] = Query(default_factory=list),
    albumId: list[str] = Query(default_factory=list),
    artistId: list[str] = Query(default_factory=list),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    return await _star_common(request, False, id, albumId, artistId,
                              sb_session, u, p, s, t, f)


@_route("/setRating")
@_route("/setRating.view")
@_wrap
async def set_rating(
    request: Request,
    id: str = Query(...),
    rating: int = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """Song ids: the library-wide track rating the web UI edits (one value per
    track, shared by all users).  Album / artist ids: the caller's own rating
    of that album / artist (per user, shown as ``userRating``).  Rating 0
    removes it.  Same edit-role gate as the web API for both."""
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if user.role == "readonly":
        raise _SubsonicError(50, "Your account is read-only. Ask an admin to upgrade you to 'edit' to rate.")
    if not 0 <= rating <= 5:
        raise _SubsonicError(10, "rating must be between 0 and 5 (0 removes the rating).")
    store = get_store()
    if id.startswith(("al:", _sx.FOLDER_PREFIX)):
        # Only ids that resolve NOW are stored (a client can't grow the state
        # file with ids nothing will show); clearing accepts any known-shaped id.
        if rating and (not _SYNTH_ID_RE.match(id) or _resolve_album_entry(store, id) is None):
            raise _SubsonicError(70, "Album not found.")
        await _get_state().rate(user.id, "album", id, rating)
        return _ok(fmt=f)
    if id.startswith("ar:"):
        if rating and (not _SYNTH_ID_RE.match(id) or _decode_artist(id, store) is None):
            raise _SubsonicError(70, "Artist not found.")
        await _get_state().rate(user.id, "artist", id, rating)
        return _ok(fmt=f)
    tid = _split_id(store, id)[0]                 # a tune rates its file
    if not store.get_track(tid):
        raise _SubsonicError(70, "Song not found.")
    # The store API ``core.data.set_rating`` wraps (AOF-journalled; bumps
    # ``_rating_seq`` so rating-ranked views refresh).  Rating 0 removes it.
    store.set_rating(tid, rating)
    return _ok(fmt=f)


# ── Artist / album info ──────────────────────────────────────────────────────
# Bios come from core/artistinfo.py (MusicBrainz → Wikipedia, disk-cached) or,
# for scene musicians, Demozoo.  A cold lookup is several rate-limited network
# round trips, so a request never waits on it for long: ``_INFO_TIMEOUT_SEC``
# for a scene musician (Demozoo answers in one call, ~0.4 s cold), only
# ``_INFO_TIMEOUT_MB_SEC`` for the MusicBrainz path (four calls behind a 1.1 s
# rate gate can't finish in a request anyway — the short wait still catches a
# disk-cache hit or a quick "not found").  The lookup keeps running in the
# background (its result lands in the disk cache) and the client gets
# whatever is cached now — the next call is served from cache.  Background
# lookups are capped so a client that prefetches info for every artist can't
# queue thousands of them.

_INFO_TIMEOUT_SEC = 1.5
_INFO_TIMEOUT_MB_SEC = 0.3
_INFO_MAX_INFLIGHT = 8
_INFO_TASKS: dict[str, asyncio.Task] = {}
_MBID_RE = re.compile(r"musicbrainz\.org/artist/([0-9a-f-]{36})")
# Sample bound for deciding an artist's universe (scene vs mainstream).
_INFO_SAMPLE = 200
# When the minority universe holds at least this share of the sample, the
# name is ambiguous (a mainstream act with a namesake scener, or vice versa):
# no bio / image / MusicBrainz id is guessed — only the last.fm link.
_INFO_MIXED_SHARE = 0.2
# XSD order of ArtistInfoBase's children (strict XML clients validate it).
_ARTIST_INFO_ORDER = ("biography", "musicBrainzId", "lastFmUrl", "smallImageUrl",
                      "mediumImageUrl", "largeImageUrl", "similarArtist")
# The sizes the info image links ask getCoverArt for: exactly the cached
# renditions (``_cover_bucket``: sm 200, lg 550, then the original), so each
# link is a distinct image and none is a bigger download than it says.
_INFO_IMAGE_SIZES = (("smallImageUrl", 200), ("mediumImageUrl", 550),
                     ("largeImageUrl", 1200))


def _info_task_done(key: str, task: asyncio.Task) -> None:
    _INFO_TASKS.pop(key, None)
    if not task.cancelled():
        task.exception()          # mark retrieved — failures are non-fatal here


async def _artist_card_bounded(name: str, *, album: str | None, track: str | None,
                               is_retro: bool) -> dict | None:
    from soniqboom.core import artistinfo
    key = f"{name.lower()}|{int(is_retro)}"
    task = _INFO_TASKS.get(key)
    if task is None:
        if len(_INFO_TASKS) >= _INFO_MAX_INFLIGHT:
            # Saturated: answer from the disk cache only (MusicBrainz path).
            try:
                return artistinfo._read_cache(name)
            except Exception:              # noqa: BLE001
                return None
        task = asyncio.get_running_loop().create_task(
            artistinfo.get_artist_info(name, album=album, track=track, is_retro=is_retro))
        _INFO_TASKS[key] = task
        task.add_done_callback(functools.partial(_info_task_done, key))
    timeout = _INFO_TIMEOUT_SEC if is_retro else _INFO_TIMEOUT_MB_SEC
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        return None               # still resolving in the background
    except Exception:              # noqa: BLE001 — enrichment never fails the call
        return None


# ── Artist photos ────────────────────────────────────────────────────────────
# getArtistInfo's image URLs used to point straight at Wikimedia: some client
# HTTP stacks got 403, every client's IP went to a third party, and all three
# sizes were the same 330 px thumbnail.  The card's photo is now fetched ONCE
# by the server (its own User-Agent; the 960 px rendition when Wikimedia has
# one, else the card's own), kept next to the card (``<slug>.img`` in the
# artist-info cache, bounded) and served by getCoverArt(ar:…) through signed
# links, resized per size bucket.  getCoverArt itself never looks anything up
# on the network — an artist grid stays offline-only.

_PHOTO_TIMEOUT_SEC = 1.5          # getArtistInfo waits this long for a first fetch
_PHOTO_MAX_FILES = 5000
_PHOTO_RETRY_SEC = 7 * 86400      # a failed fetch is retried after this
_PHOTO_MIN_BYTES = 256
_PHOTO_MAX_BYTES = 12 * 1024 * 1024
_PHOTO_TASKS: dict[str, asyncio.Task] = {}
_PHOTO_INDEX: dict = {"slugs": None}   # slug → mtime of its photo (listed once)
_WIKI_THUMB_RE = re.compile(r"^(https://(?:upload|thumb)\.wikimedia\.org/.+/thumb/.+/)\d+px-([^/?]+)")


def _photo_dir():
    from soniqboom.core import artistinfo
    return artistinfo._cache_dir()


def _photo_slug(name: str) -> str:
    from soniqboom.core import artistinfo
    return artistinfo._slug(name)


def _photo_candidates(url: str) -> list[str]:
    """The card's image URL, preceded by its 960 px Wikimedia rendition (a
    standard thumbnail size; a smaller original answers an error and the
    card's own rendition is used)."""
    m = _WIKI_THUMB_RE.match(url or "")
    return ([f"{m.group(1)}960px-{m.group(2)}"] if m else []) + ([url] if url else [])


async def _photo_index() -> dict:
    idx = _PHOTO_INDEX["slugs"]
    if idx is None:
        def _scan() -> dict:
            out = {}
            try:
                for f in _photo_dir().glob("*.img"):
                    try:
                        out[f.stem] = f.stat().st_mtime
                    except OSError:
                        pass
            except Exception:              # noqa: BLE001 — no data dir: no photos
                pass
            return out
        scanned = await asyncio.to_thread(_scan)
        idx = _PHOTO_INDEX["slugs"]
        if idx is None:
            idx = _PHOTO_INDEX["slugs"] = scanned
    return idx


async def _fetch_photo_bytes(url: str) -> bytes | None:
    import httpx
    from soniqboom.core.artistinfo import _UA
    try:
        async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": _UA}) as cx:
            r = await cx.get(url, follow_redirects=True)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            return None
        data = r.content
        return data if _PHOTO_MIN_BYTES <= len(data) <= _PHOTO_MAX_BYTES else None
    except Exception as exc:               # noqa: BLE001 — a photo is best-effort
        log.debug("artist photo fetch failed %s: %s", url, exc)
        return None


async def _ensure_artist_photo(name: str, url: str) -> bool:
    """Have the artist's photo on disk (fetching it at most once — a failure
    leaves a marker honoured for ``_PHOTO_RETRY_SEC``).  True when present."""
    slug = _photo_slug(name)
    idx = await _photo_index()
    if slug in idx:
        return True
    d = _photo_dir()
    neg = d / f"{slug}.noimg"

    def _neg_fresh() -> bool:
        try:
            return time.time() - neg.stat().st_mtime < _PHOTO_RETRY_SEC
        except OSError:
            return False
    if await asyncio.to_thread(_neg_fresh):
        return False
    data = None
    for u in _photo_candidates(url):
        data = await _fetch_photo_bytes(u)
        if data:
            break

    def _write() -> float | None:
        if not data:
            neg.touch()
            return None
        tmp = d / f"{slug}.img.tmp"
        tmp.write_bytes(data)
        tmp.replace(d / f"{slug}.img")
        for stale in (f"{slug}.sm.jpg", f"{slug}.lg.jpg", f"{slug}.noimg"):
            (d / stale).unlink(missing_ok=True)
        return (d / f"{slug}.img").stat().st_mtime
    try:
        mtime = await asyncio.to_thread(_write)
    except OSError as exc:
        log.debug("artist photo write failed for %s: %s", name, exc)
        return False
    if mtime is None:
        return False
    idx[slug] = mtime
    if len(idx) > _PHOTO_MAX_FILES:
        # Bounded: drop the oldest photos (they re-fetch on demand).
        for old in sorted(idx, key=idx.get)[:len(idx) - _PHOTO_MAX_FILES]:
            idx.pop(old, None)

            def _rm(o=old):
                for ext in (".img", ".sm.jpg", ".lg.jpg"):
                    (d / f"{o}{ext}").unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                await asyncio.to_thread(_rm)
    return True


async def _artist_photo_bounded(name: str, url: str) -> bool:
    """``_ensure_artist_photo`` as one shared task per artist, waited on for
    at most ``_PHOTO_TIMEOUT_SEC`` (it finishes in the background after)."""
    key = _photo_slug(name)
    task = _PHOTO_TASKS.get(key)
    if task is None:
        if len(_PHOTO_TASKS) >= _INFO_MAX_INFLIGHT:
            return key in (await _photo_index())
        task = asyncio.get_running_loop().create_task(_ensure_artist_photo(name, url))
        _PHOTO_TASKS[key] = task
        task.add_done_callback(lambda t, k=key: (_PHOTO_TASKS.pop(k, None),
                                                 t.cancelled() or t.exception()))
    try:
        return bool(await asyncio.wait_for(asyncio.shield(task), timeout=_PHOTO_TIMEOUT_SEC))
    except Exception:                      # noqa: BLE001 — timeout / failure: no photo yet
        return False


async def _artist_photo_response(request: Request, name: str, bucket: str) -> Response | None:
    """The artist's stored photo for a getCoverArt ``bucket`` (sm / lg /
    full), with an ETag (304 on a match) — or None when there is none.
    Disk only; the sm / lg renditions are made once, off the loop."""
    slug = _photo_slug(name)
    idx = await _photo_index()
    mtime = idx.get(slug)
    if mtime is None:
        return None
    etag = f'"ar-{slug}:{bucket}:{int(mtime)}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag,
                                                  "Cache-Control": _ART_CACHE_CONTROL})
    d = _photo_dir()

    def _load() -> bytes | None:
        src = d / f"{slug}.img"
        if bucket == "full":
            return src.read_bytes()
        from soniqboom.api.art import _SIZE_MAP
        from soniqboom.core.metadata import resize_cover
        out = d / f"{slug}.{bucket}.jpg"
        try:
            return out.read_bytes()
        except OSError:
            pass
        data = resize_cover(src.read_bytes(), _SIZE_MAP[bucket])
        if data:
            tmp = out.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(out)
        return data
    try:
        data = await asyncio.to_thread(_load)
    except OSError:
        idx.pop(slug, None)                # pruned / removed meanwhile
        return None
    if not data:
        return None
    media = "image/jpeg"
    if bucket == "full" and data[:4] == b"\x89PNG":
        media = "image/png"
    return _art_response(data, media, etag)


def _artist_for_id(store, raw: str) -> tuple[str, str, list[dict] | None] | None:
    """getArtistInfo accepts an artist, album or song id → ``(key, name,
    the tracks that decide the artist's universe or None)``: a song decides
    by itself, an album by its tracks, an artist id by a sample of all their
    tracks (``None``).  Key ``""`` is the reserved ``[Unknown Artist]``."""
    if raw.startswith("ar:"):
        hit = _decode_artist(raw, store)
        return (hit[0], hit[1], None) if hit is not None else None
    if raw.startswith(("al:", _sx.FOLDER_PREFIX)):
        e = _resolve_album_entry(store, raw)
        if e is None:
            return None
        return (e.artist_l, e.artist, _album_and_tracks(store, raw)[1][:_INFO_SAMPLE])
    tr = store.get_track(_split_id(store, raw)[0])
    if tr:
        name = _sx.norm_owner(tr.get("artist")) or _sx.owner_of(tr)
        return (name.lower(), name or _sx.UNKNOWN_ARTIST, [tr])
    return None


def _artist_context(store, name_l: str, tracks: list[dict] | None = None
                    ) -> tuple[int, int, dict[bool, tuple[str | None, str | None]]]:
    """``(retro count, other count, {is_retro: (album, title)})`` over
    ``tracks`` or a bounded sample (``_INFO_SAMPLE``) of the artist's tracks —
    counted per universe, so a mainstream artist with a few tracker covers
    isn't treated as a scener, and the album / title that disambiguate the
    lookup come from the universe the lookup is made in."""
    from soniqboom.core.retro import is_retro_format
    counts = {True: 0, False: 0}
    ctx: dict[bool, list] = {True: [None, None], False: [None, None]}
    if tracks is None:
        tracks = []
        for n, tid in enumerate(_artist_track_ids(store, name_l)):
            if n >= _INFO_SAMPLE:
                break
            tr = store.get_track(tid)
            if tr:
                tracks.append(tr)
    for tr in tracks:
        r = bool(is_retro_format(tr.get("format")))
        counts[r] += 1
        c = ctx[r]
        c[0] = c[0] or (tr.get("album") or "").strip() or None
        c[1] = c[1] or (tr.get("title") or "").strip() or None
    return counts[True], counts[False], {k: (v[0], v[1]) for k, v in ctx.items()}


def _scene_similar(store, name_l: str, count: int) -> list[str]:
    """Library artists sharing a Demozoo scene group with ``name_l`` (the one
    artist relationship the library itself knows).  Bounded walk."""
    tag_groups = getattr(store, "_tag_scene_group", None) or {}
    groups: set[str] = set()
    for n, tid in enumerate(_artist_track_ids(store, name_l)):
        if n >= 200:
            break
        sg = (store.get_track(tid) or {}).get("scene_group") or ""
        groups.update(g.strip().lower() for g in str(sg).split("•") if g.strip())
    freq: Counter = Counter()
    for g in groups:
        for n, tid in enumerate(tag_groups.get(g) or ()):
            if n >= 2000:
                break
            o = _sx.owner_of(store.get_track(tid) or {}).lower()
            if o and o != name_l:
                freq[o] += 1
    return [o for o, _c in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:count]]


@_route("/getArtistInfo")
@_route("/getArtistInfo.view")
@_route("/getArtistInfo2")
@_route("/getArtistInfo2.view")
@_wrap
async def get_artist_info(
    request: Request,
    id: str = Query(...),
    count: int = Query(20, ge=0),
    includeNotPresent: bool = False,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """Bio / image / MusicBrainz id from the artist's own universe: a song id
    decides by its format, an album by its tracks, an artist id by the
    majority of a bounded sample of their tracks.  A name whose tracks are
    substantially in both universes (a namesake) gets no guessed bio — only
    the last.fm link, which is a link, not data.  Similar artists are the
    scene-group neighbours, so only for an unambiguous scener.  The image
    URLs are signed getCoverArt links to the artist's photo, which the
    server fetched once and stores (never a third-party URL); omitted while
    there is none."""
    user = _require_user(request, sb_session, u, p, s, t)
    count = min(count, 100)       # page size clamped, never an error
    store = get_store()
    cat = _catalogue(store)       # before the card lookup: a retry never re-waits on it
    hit = _artist_for_id(store, id)
    if hit is None:
        raise _SubsonicError(70, "Artist not found.")
    low, name, decide = hit
    key = "artistInfo2" if "artistinfo2" in request.url.path.lower() else "artistInfo"
    info: dict = {}
    if not low:
        # The reserved [Unknown Artist] — nothing to look up (and a fuzzy
        # external match on a placeholder name would only invent a bio).
        return _ok({key: info}, fmt=f)
    retro_n, plain_n, per = _artist_context(store, low, decide)
    retro = retro_n > plain_n
    total = retro_n + plain_n
    ambiguous = total > 0 and min(retro_n, plain_n) >= _INFO_MIXED_SHARE * total
    if not ambiguous:
        album, title = per[retro]
        card = await _artist_card_bounded(name, album=album, track=title, is_retro=retro)
        if card and card.get("found"):
            if card.get("bio"):
                info["biography"] = card["bio"]
            # An explicit ``mbid`` on the card wins; otherwise the
            # MusicBrainz-only records carry it in their URL.  (Wikipedia-
            # backed records currently store neither — see core/artistinfo.py.)
            mbid = card.get("mbid")
            m = None if mbid else _MBID_RE.search(card.get("url") or "")
            if mbid or m:
                info["musicBrainzId"] = mbid or m.group(1)
            img = card.get("image")
            if img and await _artist_photo_bounded(name, img):
                aid = cat.union.artist_id(low)
                for key_, px in _INFO_IMAGE_SIZES:
                    info[key_] = _cover_url(request, user, aid, px)
    # Similar artists: only ones present in the library (includeNotPresent is
    # accepted but there is no external similarity source to add others).
    similar = []
    if count and retro and not ambiguous:
        union = cat.union
        for o in _scene_similar(store, low, count):
            similar.append({"id": _artist_id(o), "name": union.display.get(o, o),
                            "albumCount": cat.album_count(o), "coverArt": _artist_id(o)})
    if similar:
        info["similarArtist"] = similar
    # A link (not data): last.fm resolves any artist name to its page.
    from urllib.parse import quote
    info["lastFmUrl"] = "https://www.last.fm/music/" + quote(name, safe="")
    info = {k: info[k] for k in _ARTIST_INFO_ORDER if k in info}
    if (f or "xml").lower() not in ("json", "jsonp"):
        # XML: the spec carries these as child ELEMENTS with text content
        # (<biography>…</biography>) — DSub/Amperfy read them there, not as
        # attributes.  ``_text`` renders exactly that; JSON keeps plain strings.
        info = {k: ({"_text": v} if isinstance(v, str) else v) for k, v in info.items()}
    return _ok({key: info}, fmt=f)


@_route("/getAlbumInfo")
@_route("/getAlbumInfo.view")
@_route("/getAlbumInfo2")
@_route("/getAlbumInfo2.view")
@_wrap
async def get_album_info(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """What the library itself knows about an album (no network lookup):
    small / medium / large image URLs (the album's cover, via getCoverArt
    with a signed token so a client can load them without its own
    credentials), ``notes`` only when EVERY track carries the same
    non-trivial comment (a release note, not a per-track remark), and
    ``lastFmUrl`` for a tagged album of a known artist (a link last.fm
    resolves by name — never for a folder album, whose name is only a
    directory's).  No MusicBrainz release id is stored, so ``musicBrainzId``
    is never sent.  A song id resolves to its album; an unknown id is
    error 70."""
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    album_key = id
    hdr = None
    if not id.startswith(("al:", _sx.FOLDER_PREFIX)):
        tr = store.get_track(_split_id(store, id)[0])
        if tr is None:
            raise _SubsonicError(70, "Album not found.")
        e = _catalogue(store).track_entry(tr)
        album_key = e.id if e is not None else None
        if album_key is None:
            tracks = [tr]
        else:
            hdr, tracks = _album_and_tracks(store, album_key)
        cover = tr["id"]
    else:
        hdr, tracks = _album_and_tracks(store, id)
        if not tracks:
            raise _SubsonicError(70, "Album not found.")
        cover = max(tracks, key=_sx.sample_key).get("id")    # the album list's pick
    info: dict = {}
    if album_key is not None and tracks:
        comments = {(x.get("comment") or "").strip() for x in tracks}
        if len(comments) == 1:
            note = comments.pop()
            if len(note.split()) >= 3 and not note.lower().startswith(("http://", "https://")):
                info["notes"] = note[:4000]
    if (hdr and album_key and album_key.startswith("al:")
            and hdr.get("artist") and hdr["artist"] != _sx.UNKNOWN_ARTIST and hdr.get("name")):
        from urllib.parse import quote
        info["lastFmUrl"] = ("https://www.last.fm/music/" + quote(hdr["artist"], safe="")
                             + "/" + quote(hdr["name"], safe=""))
    if cover:
        for key, px in _INFO_IMAGE_SIZES:
            info[key] = _cover_url(request, user, cover, px)
    if (f or "xml").lower() not in ("json", "jsonp"):
        # XML: child elements with text content, as for artistInfo.
        info = {k: {"_text": v} for k, v in info.items()}
    return _ok({"albumInfo": info}, fmt=f)


@_route("/getTopSongs")
@_route("/getTopSongs.view")
@_wrap
async def get_top_songs(
    request: Request,
    artist: str | None = Query(default=None),
    id: str | None = Query(default=None, description="Artist id (topSongsByArtistId)"),
    count: int = Query(50, ge=0),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    # Ranked by this server's own play counts, then rating, then title — there
    # is no external (last.fm) popularity source.  O(k log count) over the
    # artist's tracks only.  ``id`` (an artist id, OpenSubsonic
    # topSongsByArtistId) takes precedence over the ``artist`` name.
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    count = min(count, 500)
    if id:
        hit = _decode_artist(id, store)
        if hit is None:
            raise _SubsonicError(70, "Artist not found.")
        low = hit[0]
    elif artist is not None:
        low = artist.strip().lower()
    else:
        raise _SubsonicError(10, "Required parameter 'artist' or 'id' is missing.")
    # The reserved [Unknown Artist] ("") has no tag-index bucket: no top songs.
    tids = _artist_track_ids(store, low) if low else set()
    import heapq
    top = heapq.nsmallest(count, tids, key=_rank_key(store)) if count else []
    ctx = _song_ctx(store, user)
    songs = [_track_to_song(tr, ctx) for tr in store.get_tracks_batch(top) if tr]
    return _ok({"topSongs": {"song": songs}}, fmt=f)


# ── Play queue (cross-device resume) ─────────────────────────────────────────
# Queue / bookmark ids are stored verbatim — a tune id (``<track>~<n>``)
# included — and resolved on read; an id whose track is gone drops out.


def _queue_entry(store, raw: str, ctx: _SongCtx) -> dict | None:
    """A stored song id (track or tune) → its Child, or None when gone."""
    tid, n = _split_id(store, raw)
    tr = store.get_track(tid)
    if not tr:
        return None
    return _tune_child(_track_to_song(tr, ctx), tr, n, ctx)


def _resolve_play_queue(store, q: dict, ctx: _SongCtx) -> tuple[list[dict], int | None, bool]:
    """A saved queue → ``(entries, current index into entries, whether the
    saved current song survived)``, one walk.  Deleted tracks drop out and
    the saved index is re-mapped onto what remains.  When the current song
    itself is gone the queue still has a current entry (the spec requires
    one whenever there are entries): the first survivor after it, else the
    last entry — its saved position no longer applies."""
    entries: list[dict] = []
    saved = q["current_index"]
    cur = None
    after = None
    for i, raw in enumerate(q["ids"]):
        e = _queue_entry(store, raw, ctx)
        if e is None:
            continue
        if i == saved:
            cur = len(entries)
        elif after is None and i > saved:
            after = len(entries)
        entries.append(e)
    if cur is not None or not entries:
        return entries, cur, cur is not None
    return entries, (after if after is not None else len(entries) - 1), False


def _empty_play_queue(user) -> dict:
    """The required ``playQueue`` element when nothing is saved (as
    Navidrome): no entries, no current song."""
    return {"username": getattr(user, "username", ""), "changed": _iso_req(),
            "changedBy": ""}


@_route("/getPlayQueue")
@_route("/getPlayQueue.view")
@_wrap
async def get_play_queue(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    q = _get_state().play_queue(getattr(user, "id", None))
    if not q:
        # Nothing saved: the (required) element without entries.
        return _ok({"playQueue": _empty_play_queue(user)}, fmt=f)
    store = get_store()
    ctx = _song_ctx(store, user)
    entries, cur, survived = _resolve_play_queue(store, q, ctx)
    pq: dict = {
        # The saved position belongs to the saved current song; if that song
        # is gone, the re-mapped current entry starts from 0.
        "position":  int(q.get("position") or 0) if survived else 0,
        "username":  getattr(user, "username", ""),
        "changed":   _iso_req(q.get("changed")),
        "changedBy": q.get("changed_by") or "",
        "entry":     entries,
    }
    # By index, so a queue holding the same song twice names the right one;
    # the entry id is already canonical (``<id>~0`` listed as the bare id).
    if cur is not None:
        pq["current"] = entries[cur]["id"]
    return await _ok_async({"playQueue": pq}, fmt=f, n_hint=len(entries),
                           splice=("playQueue", "entry"))


@_route("/savePlayQueue")
@_route("/savePlayQueue.view")
@_wrap
async def save_play_queue(
    request: Request,
    id: list[str] = Query(default_factory=list),
    current: str | None = None,
    position: int | None = None,
    c: str = "",
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    await _get_state().save_play_queue(user.id, id, current=current,
                                       position=position or 0, changed_by=c)
    return _ok(fmt=f)


# ── Library scan ─────────────────────────────────────────────────────────────

def _scan_status_payload() -> dict:
    from soniqboom.core import scanner
    prog = scanner.get_progress()
    scanning = bool(prog.running or scanner.is_scanning())
    # While scanning: files processed so far in this scan; idle: library size.
    count = int(prog.processed or 0) if scanning else get_store().track_count()
    return {"scanStatus": {"scanning": scanning, "count": count}}


async def _trigger_scan() -> None:
    """Start a scan of every registered folder via the SAME code path as the
    admin UI's "Scan" button (``POST /api/admin/scan``) — progress broadcast,
    local/remote split and all.  Indirection so tests can stub it."""
    from soniqboom.api.admin import admin_scan
    await admin_scan(body=None, _tok="subsonic")


@_route("/getScanStatus")
@_route("/getScanStatus.view")
@_wrap
async def get_scan_status(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    return _ok(_scan_status_payload(), fmt=f)


@_route("/startScan")
@_route("/startScan.view")
@_wrap
async def start_scan(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if user.role != "admin":
        raise _SubsonicError(50, "Only an admin can start a library scan.")
    from soniqboom.core import scanner
    if not scanner.full_scan_active():     # a watcher's scoped scan doesn't count
        try:
            await _trigger_scan()
        except HTTPException as exc:       # e.g. 400 "No scan directories configured."
            raise _SubsonicError(0, str(exc.detail))
    return _ok(_scan_status_payload(), fmt=f)


# ── Internet radio ───────────────────────────────────────────────────────────
# The station favorites (core/radiodir.py) are the server's radio list.  The
# web player streams them through ``/api/stations/relay/{sid}``, which sits
# behind the session-cookie middleware a Subsonic client can't satisfy.  So
# Subsonic gets its own relay route, ``/rest/radioStream``, which calls the
# SAME relay handler (``api.stations.relay``: one shared StationHub per
# station, the SSRF guard on every URL incl. redirects / playlist targets, the
# SSRF-validating egress proxy, HLS → ffmpeg, ICY metadata stripped).
#
# Clients open ``streamUrl`` as-is, without adding their own credentials, so
# the URL carries a signed token (HMAC, bound to user + station, 30-day
# expiry — the station list is re-fetched far more often) instead of a
# password.  Standard Subsonic auth (u+p / u+t+s) is accepted on the route
# too, for clients that do append it.

_RADIO_TOKEN_TTL = 30 * 24 * 3600
_MAX_FAVORITE_STATIONS = 500


def _station_stream_index(st: dict) -> int:
    """Preferred stream of a station for Subsonic clients: a plain (non-HLS,
    non-playlist) mount, ranked MP3 → AAC → anything else (every mobile
    decoder plays MP3/AAC; Ogg/Opus isn't a given on iOS); else the first —
    the relay transcodes HLS anyway."""
    streams = [x for x in (st.get("streams") or []) if isinstance(x, dict)]

    def _plain(x: dict) -> bool:
        path = (x.get("url") or "").split("?", 1)[0].lower()
        return bool(x.get("url")) and not x.get("hls") and \
            not path.endswith((".m3u8", ".m3u", ".pls"))

    def _rank(x: dict) -> int:
        codec = (x.get("codec") or "").upper()
        return 0 if codec == "MP3" else 1 if codec.startswith("AAC") else 2

    plain = [(_rank(x), i) for i, x in enumerate(streams) if _plain(x)]
    return min(plain)[1] if plain else 0


def _password_version(user) -> str:
    """A short digest of the user's password hash AND Subsonic app password:
    bound into radio / cover tokens so a password or app-password change
    revokes every link handed out before it."""
    raw = ((getattr(user, "password_hash", "") or "") + "\0"
           + (getattr(user, "subsonic_password", "") or ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _link_claims(request: Request, user, typ: str) -> dict:
    """The claims every signed link (radio / cover) carries: its type, the
    user, their password version and — when the request authenticated with
    an API key — that key's id, so revoking the key kills the link."""
    claims = {"typ": typ, "uid": getattr(user, "id", "") or "",
              "pv": _password_version(user)}
    kid = getattr(getattr(request, "state", None), "sb_api_key_id", None)
    if kid:
        claims["kid"] = kid
    return claims


def _link_owner_ok(claims: dict):
    """The enabled owner of a signed link whose password version (and minting
    API key, if any) still holds — else None."""
    ustore = get_user_store()
    owner = ustore.get(str(claims.get("uid") or ""))
    if owner is None or not owner.enabled or claims.get("pv") != _password_version(owner):
        return None
    kid = claims.get("kid")
    if kid:
        has = getattr(ustore, "has_api_key", None)
        if has is None or not has(owner.id, str(kid)):
            return None
    return owner


def _public_base_url(request: Request) -> str:
    """The URL the CLIENT reached us at — honouring a TLS-terminating reverse
    proxy's ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` (as the session
    cookie's Secure flag does), so a proxied deployment hands out a reachable
    ``streamUrl``.  The value only ever goes back to the requester."""
    base = str(request.base_url).rstrip("/")
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if proto in ("http", "https") or host:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(base)
        if host and all(ch.isalnum() or ch in "-.:[]" for ch in host):
            parts = parts._replace(netloc=host)
        if proto in ("http", "https"):
            parts = parts._replace(scheme=proto)
        base = urlunsplit(parts)
    return base


def _radio_stream_url(request: Request, sid: str, user) -> str:
    from urllib.parse import urlencode
    tok = _sign_token({**_link_claims(request, user, "radio"), "rs": sid,
                       "exp": int(time.time()) + _RADIO_TOKEN_TTL})
    return f"{_public_base_url(request)}/rest/radioStream.view?" + urlencode(
        {"id": sid, "token": tok})


@_media_route("/radioStream")
@_media_route("/radioStream.view")
@_wrap
async def radio_stream(
    request: Request,
    id: str = Query(...),
    token: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """Relay an internet-radio station to a Subsonic client (SoniqBoom
    extension; the ``streamUrl`` getInternetRadioStations hands out)."""
    if token:
        claims = _verify_token(token)
        if (not claims or claims.get("typ") != "radio" or claims.get("rs") != id
                or _link_owner_ok(claims) is None):
            raise _SubsonicError(40, "This radio link has expired — refresh the station list.")
    else:
        _require_user(request, sb_session, u, p, s, t)
    from soniqboom.api import stations as _stations
    from soniqboom.core import radiodir
    st = await radiodir.resolve_station(id)
    if not st:
        raise _SubsonicError(70, "Radio station not found.")
    idx = _station_stream_index(st)
    if request.method == "HEAD":
        # A probe: the type only — no upstream connection is opened.
        streams = [x for x in (st.get("streams") or []) if isinstance(x, dict)]
        codec = ((streams[idx] if idx < len(streams) else {}).get("codec") or "").upper()
        mime = ("audio/aac" if codec.startswith("AAC") else
                "audio/ogg" if codec in ("OGG", "OPUS", "VORBIS") else "audio/mpeg")
        resp = _head_response(mime, None)
        del resp.headers["accept-ranges"]              # a live stream isn't seekable
        return resp
    try:
        return await _stations.relay(sid=id, v=idx)
    except HTTPException as exc:
        # Dead / unreachable station, or the SSRF guard refusing a non-public
        # target — never bypassed, only reported.
        raise _SubsonicError(70 if exc.status_code == 404 else 0,
                             f"Radio station unavailable: {exc.detail}")


@_route("/getInternetRadioStations")
@_route("/getInternetRadioStations.view")
@_wrap
async def get_internet_radio_stations(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    from soniqboom.core import radiodir
    out = []
    for st in radiodir.get_favorites():
        sid = st.get("sid") if isinstance(st, dict) else None
        if not sid or not any(isinstance(x, dict) and x.get("url")
                              for x in (st.get("streams") or [])):
            continue
        row = {"id": sid, "name": st.get("name") or sid,
               "streamUrl": _radio_stream_url(request, sid, user)}
        # Directory / Radio Browser homepages are third-party data: only an
        # http(s) URL is ever handed to clients (never ``javascript:``).
        hp = _clean_homepage(st.get("homepage"))
        if hp:
            row["homePageUrl"] = hp
        if _logo_url_of(st):
            row["coverArt"] = _station_cover_id(sid)      # OpenSubsonic: the logo
        out.append(row)
    return _ok({"internetRadioStations": {"internetRadioStation": out}}, fmt=f)


def _require_radio_editor(request, sb_session, u, p, s, t):
    # Same gate as the web API's favorites endpoints (require_edit).
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if user.role not in ("admin", "edit"):
        raise _SubsonicError(50, "Your account is read-only — only admin / edit users "
                                 "can manage radio stations.")
    return user


async def _checked_stream_url(url: str | None) -> str:
    """A client-supplied stream URL: http(s) only, and it must pass the relay's
    SSRF guard now (the relay re-checks it — and every redirect / playlist
    hop — on each play)."""
    from urllib.parse import urlsplit
    from soniqboom.api import stations as _stations
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        parts.port                        # raises ValueError on a bad port
    except ValueError:
        raise _SubsonicError(10, "streamUrl is not a valid URL.")
    if not url or parts.scheme.lower() not in ("http", "https") or not parts.hostname \
            or len(url) > 2048:
        raise _SubsonicError(10, "streamUrl must be an http(s) URL.")
    try:
        await _stations._assert_public_url(url)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise _SubsonicError(50, "streamUrl points at a non-public address.")
        if exc.status_code == 400:
            raise _SubsonicError(10, str(exc.detail))
        raise _SubsonicError(0, str(exc.detail))
    return url


def _clean_homepage(url: str | None) -> str:
    """Only http(s) homepages are stored (a ``javascript:`` URL would be
    handed to every client)."""
    from urllib.parse import urlsplit
    url = (url or "").strip()[:2048]
    try:
        return url if urlsplit(url).scheme.lower() in ("http", "https") else ""
    except ValueError:
        return ""


def _station_with_url(stations: list, url: str) -> dict | None:
    for st in stations:
        if isinstance(st, dict) and any(isinstance(x, dict) and x.get("url") == url
                                        for x in (st.get("streams") or [])):
            return st
    return None


@_route("/createInternetRadioStation")
@_route("/createInternetRadioStation.view")
@_wrap
async def create_internet_radio_station(
    request: Request,
    streamUrl: str | None = None,
    name: str | None = None,
    homepageUrl: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """Add a station to the favorites.  A URL belonging to a curated station
    adds THAT station (its trusted stream list); any other public URL becomes
    a custom favorite with its own ``custom:`` id — it can never take over an
    existing station's id."""
    _require_radio_editor(request, sb_session, u, p, s, t)
    if not (name or "").strip():
        raise _SubsonicError(10, "Required parameter 'name' is missing.")
    url = await _checked_stream_url(streamUrl)
    from soniqboom.core import radiodir
    favs = radiodir.get_favorites()
    if _station_with_url(favs, url):
        return _ok(fmt=f)                                    # already there
    if len(favs) >= _MAX_FAVORITE_STATIONS:
        raise _SubsonicError(0, f"Too many radio stations (limit {_MAX_FAVORITE_STATIONS}) — "
                                "delete some first.")
    known = _station_with_url(radiodir.SCENE_PACK, url)
    if known is not None:
        radiodir.add_favorite(dict(known))
        return _ok(fmt=f)
    import secrets as _secrets
    radiodir.add_favorite({
        # Random, never derived from the URL: an edited custom station's old
        # URL can be added again without colliding with it.
        "sid": "custom:" + _secrets.token_hex(8),
        "name": name.strip()[:200],
        "homepage": _clean_homepage(homepageUrl),
        "favicon": "", "country": "", "tags": "", "votes": 0,
        "streams": [{"url": url, "codec": "", "bitrate": 0, "hls": 0}],
        "custom": True,
    })
    return _ok(fmt=f)


@_route("/updateInternetRadioStation")
@_route("/updateInternetRadioStation.view")
@_wrap
async def update_internet_radio_station(
    request: Request,
    id: str = Query(...),
    streamUrl: str | None = None,
    name: str | None = None,
    homepageUrl: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_radio_editor(request, sb_session, u, p, s, t)
    from soniqboom.core import radiodir
    cur = next((x for x in radiodir.get_favorites()
                if isinstance(x, dict) and x.get("sid") == id), None)
    if cur is None:
        raise _SubsonicError(70, "Radio station not found.")
    upd = dict(cur)
    if streamUrl is not None:
        url = await _checked_stream_url(streamUrl)
        if id.startswith("custom:"):
            upd["streams"] = [{"url": url, "codec": "", "bitrate": 0, "hls": 0}]
        elif not _station_with_url([cur], url):
            # A curated station's streams come from the trusted directory.
            raise _SubsonicError(50, "The stream URL of a directory station can't be "
                                     "changed — add the URL as a new station instead.")
    if name is not None and name.strip():
        upd["name"] = name.strip()[:200]
    if homepageUrl is not None:
        upd["homepage"] = _clean_homepage(homepageUrl)
    if upd != cur and radiodir.update_favorite(id, upd) is None:
        raise _SubsonicError(70, "Radio station not found.")    # removed meanwhile
    return _ok(fmt=f)


@_route("/deleteInternetRadioStation")
@_route("/deleteInternetRadioStation.view")
@_wrap
async def delete_internet_radio_station(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    # Maps 1:1 onto removing a favorite; same edit-role gate as the web API.
    _require_radio_editor(request, sb_session, u, p, s, t)
    from soniqboom.core import radiodir
    if not radiodir.is_favorite(id):
        raise _SubsonicError(70, "Radio station not found.")
    radiodir.remove_favorite(id)
    return _ok(fmt=f)


# ── Play queue by index (OpenSubsonic indexBasedQueue) ──────────────────────

@_route("/getPlayQueueByIndex")
@_route("/getPlayQueueByIndex.view")
@_wrap
async def get_play_queue_by_index(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    q = _get_state().play_queue(getattr(user, "id", None))
    if not q:
        return _ok({"playQueueByIndex": _empty_play_queue(user)}, fmt=f)
    store = get_store()
    ctx = _song_ctx(store, user)
    # Deleted tracks drop out, so the saved index is re-mapped onto what
    # remains (``_resolve_play_queue``).
    entries, cur_index, survived = _resolve_play_queue(store, q, ctx)
    pq: dict = {
        # The saved position belongs to the saved current song; if that song
        # is gone, the re-mapped current entry starts from 0.
        "position":  q["position"] if survived else 0,
        "username":  getattr(user, "username", ""),
        "changed":   _iso_req(q["changed"]),
        "changedBy": q["changed_by"],
        "entry":     entries,
    }
    if cur_index is not None:
        pq["currentIndex"] = cur_index
    return await _ok_async({"playQueueByIndex": pq}, fmt=f, n_hint=len(entries),
                           splice=("playQueueByIndex", "entry"))


@_route("/savePlayQueueByIndex")
@_route("/savePlayQueueByIndex.view")
@_wrap
async def save_play_queue_by_index(
    request: Request,
    id: list[str] = Query(default_factory=list),
    currentIndex: int | None = None,
    position: int | None = None,
    c: str = "",
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    if id and (currentIndex is None or not 0 <= currentIndex < len(id)):
        raise _SubsonicError(10, "currentIndex is required and must index into the id list.")
    # Unusable ids (empty / oversized) are dropped by the state layer — drop
    # them here first so the index still points at the SAME song.
    keep = [i for i in id if isinstance(i, str) and 0 < len(i) <= 256]
    if id and keep is not id and len(keep) != len(id):
        if not (0 < len(id[currentIndex]) <= 256):
            raise _SubsonicError(10, "currentIndex points at an invalid id.")
        currentIndex = sum(1 for i in id[:currentIndex] if 0 < len(i) <= 256)
    await _get_state().save_play_queue(user.id, keep, current_index=currentIndex,
                                       position=position or 0, changed_by=c)
    return _ok(fmt=f)


# ── Bookmarks ────────────────────────────────────────────────────────────────

@_route("/getBookmarks")
@_route("/getBookmarks.view")
@_wrap
async def get_bookmarks(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    ctx = _song_ctx(store, user)
    marks = _get_state().bookmarks(getattr(user, "id", None))
    out = []
    for tid, b in sorted(marks.items(), key=lambda kv: -_ts_num(kv[1].get("changed"))):
        entry = _queue_entry(store, tid, ctx)
        if entry is None:
            continue                       # bookmarked track since deleted
        row = {"position": int(_ts_num(b.get("position"))),
               "username": getattr(user, "username", ""),
               "created": _iso_req(b.get("created"), b.get("changed")),
               "changed": _iso_req(b.get("changed"), b.get("created")),
               "entry": entry}
        if b.get("comment"):
            row["comment"] = str(b["comment"])
        out.append(row)
    return _ok({"bookmarks": {"bookmark": out}}, fmt=f)


@_route("/createBookmark")
@_route("/createBookmark.view")
@_wrap
async def create_bookmark(
    request: Request,
    id: str = Query(...),
    position: int = Query(..., ge=0),
    comment: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    store = get_store()
    # A tune id (``<track>~<n>``) is kept: the bookmark is that tune's
    # (``~0``, the default tune's, is the bare id's).
    tid, sub = _split_id(store, id)
    if not store.get_track(tid):
        raise _SubsonicError(70, "Song not found.")
    if not await _get_state().set_bookmark(user.id, _canon_song_id(store, id), position=position,
                                           comment=comment or ""):
        raise _SubsonicError(0, "Too many bookmarks — delete some first.")
    return _ok(fmt=f)


@_route("/deleteBookmark")
@_route("/deleteBookmark.view")
@_wrap
async def delete_bookmark(
    request: Request,
    id: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    await _get_state().delete_bookmark(user.id, _canon_song_id(get_store(), id))  # idempotent
    return _ok(fmt=f)


# ── Now playing ──────────────────────────────────────────────────────────────
# The server's now-playing registry, in memory: (user, player) → track.
# Subsonic playback feeds it through ``stream`` / ``getTranscodeStream``,
# ``scrobble(submission=false)`` and — authoritatively, with state and
# position — the OpenSubsonic ``reportPlayback``.  Web and mobile (/m) plays
# are recorded too: POST /api/tracks/{id}/played (tracks.mark_played)
# registers the signed-in user under player "SoniqBoom Web" once the play
# crosses the record threshold (never on a preload), as an authoritative
# entry.  A plain entry lives for the track's duration plus a grace period;
# a reported one until its projected end plus ``_REPORT_GRACE`` (paused:
# ``_NOW_PLAYING_GRACE`` after the last report), then drops out.

_NOW_PLAYING: dict[tuple[str, str], dict] = {}
_NOW_PLAYING_GRACE = 300.0
_REPORT_GRACE = 1800.0              # spec: stop tracking 30 min past the planned end
_NOW_PLAYING_MAX = 256
_REPORT_STATES = ("starting", "playing", "paused", "stopped")


def _np_key(user, player: str) -> tuple[str, str] | None:
    uid = getattr(user, "id", None)
    if not uid:
        return None
    return (uid, "".join(ch for ch in (player or "")[:64] if ch.isprintable()))


def _note_now_playing(user, track_id: str, player: str, *,
                      from_scrobble: bool = False, state: str | None = None,
                      position_ms: int | None = None, rate: float | None = None,
                      subsong: int = 0) -> dict | None:
    """Record what ``user`` plays on ``player``; returns the entry.  Once a
    player has sent a now-playing scrobble — or a ``reportPlayback`` (a
    ``state``) — it is authoritative: its later ``stream`` requests (often a
    PREFETCH of the next track) no longer overwrite the entry.  A reported
    entry also keeps ``state`` / position / rate for getNowPlaying, and
    whether this play was already counted (``counted``, reset when the track
    or tune changes or a new play starts).  ``subsong``: the wire played (0 =
    the file's default tune, e.g. a web play)."""
    subsong = int(subsong or 0)
    key = _np_key(user, player)
    if key is None or not track_id:
        return None
    now = time.time()
    cur = _NOW_PLAYING.get(key)
    authoritative = from_scrobble or state is not None
    if cur is not None and cur.get("scrobbled") and not authoritative:
        return cur
    if cur is None and len(_NOW_PLAYING) >= _NOW_PLAYING_MAX:
        oldest = min(_NOW_PLAYING, key=lambda k: _NOW_PLAYING[k]["at"])
        _NOW_PLAYING.pop(oldest, None)
    if (cur and cur["track"] == track_id and (cur.get("sub") or 0) == subsong
            and now - cur["at"] < 60 and not authoritative):
        return cur                 # Range re-requests of the same track / tune
    entry = {"track": track_id, "sub": subsong, "at": now,
             "username": getattr(user, "username", ""),
             "scrobbled": authoritative or bool(cur and cur.get("scrobbled"))}
    if state is not None:
        same = (cur is not None and cur.get("track") == track_id
                and (cur.get("sub") or 0) == subsong and state != "starting")
        entry.update(state=state, pos_ms=max(0, int(position_ms or 0)),
                     rate=float(rate or 1.0),
                     counted=bool(same and cur.get("counted")))
    _NOW_PLAYING[key] = entry
    return entry


def _player_id(uid: str, player: str) -> int:
    """A stable (across restarts — ``hash()`` of a str is salted per process)
    positive 31-bit id for a (user, client) pair."""
    return int(hashlib.sha1(f"{uid}\0{player}".encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF


def _play_counts(pos_sec: float, dur_sec: float) -> bool:
    """The web player's play-record rule: ≥ 30 s heard, or ≥ 20 s and at
    least half of the track."""
    return pos_sec >= 30 or (pos_sec >= 20 and dur_sec > 0 and pos_sec / dur_sec >= 0.5)


def _np_position_ms(e: dict, dur_ms: int, now: float) -> int:
    """A reported entry's position now: projected from the last report while
    playing, frozen otherwise; never past the track's end."""
    pos = float(e.get("pos_ms") or 0)
    if e.get("state") == "playing":
        pos += (now - e["at"]) * 1000.0 * float(e.get("rate") or 1.0)
    pos = max(0.0, pos)
    return int(min(pos, dur_ms) if dur_ms > 0 else pos)


def _np_expired(e: dict, dur_sec: float, now: float) -> bool:
    st = e.get("state")
    if st is None:
        return now - e["at"] > dur_sec + _NOW_PLAYING_GRACE
    if st == "paused":
        return now - e["at"] > _NOW_PLAYING_GRACE
    rate = float(e.get("rate") or 1.0)
    remaining = max(0.0, dur_sec - float(e.get("pos_ms") or 0) / 1000.0) / rate
    return now > e["at"] + remaining + _REPORT_GRACE


@_route("/reportPlayback")
@_route("/reportPlayback.view")
@_wrap
async def report_playback(
    request: Request,
    mediaId: str = Query(...),
    mediaType: str = Query(default="song"),
    positionMs: int = Query(...),
    state: str = Query(...),
    playbackRate: float = Query(default=1.0),
    ignoreScrobble: bool = Query(default=False),
    c: str = "",
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    """OpenSubsonic ``playbackReport``: a player reports its state
    (starting / playing / paused / stopped), position and speed.  It drives
    getNowPlaying (state, a projected ``positionMs``, ``playbackRate``) and —
    unless ``ignoreScrobble`` — records ONE play per start, once the position
    crosses the web player's play-record rule (or at ``stopped`` past it),
    with the same effects as ``scrobble``.  ``stopped`` ends the entry.
    Mutating: explicit credentials only (CSRF), like scrobble."""
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    mt = (mediaType or "song").strip().lower()
    if mt == "podcast":
        raise _SubsonicError(70, "Podcast episode not found.")
    if mt != "song":
        raise _SubsonicError(10, "mediaType must be 'song' or 'podcast'.")
    st = (state or "").strip().lower()
    if st not in _REPORT_STATES:
        raise _SubsonicError(10, "state must be one of starting, playing, paused, stopped.")
    store = get_store()
    tid, sub = _split_id(store, mediaId)
    track = store.get_track(tid)
    if not track:
        raise _SubsonicError(70, "Song not found.")
    pos = max(0, int(positionMs))
    rate = float(playbackRate) if math.isfinite(playbackRate) and playbackRate > 0 else 1.0
    rate = min(rate, 16.0)
    dur = float(_tune_duration(track, sub))
    key = _np_key(user, c)
    if st == "stopped":
        cur = _NOW_PLAYING.get(key) if key else None
        if (not ignoreScrobble and cur is not None and cur.get("state") is not None
                and cur.get("track") == tid and (cur.get("sub") or 0) == sub
                and not cur.get("counted") and _play_counts(pos / 1000.0, dur)):
            await _record_subsonic_play(store, user, track)
        if key:
            _NOW_PLAYING.pop(key, None)
        return _ok(fmt=f)
    entry = _note_now_playing(user, tid, c, state=st, position_ms=pos, rate=rate, subsong=sub)
    if st == "starting":
        try:
            from soniqboom.core.scrobble import submit_now_playing
            await submit_now_playing(user, track)
        except Exception:                  # noqa: BLE001
            pass
    if (entry is not None and not ignoreScrobble and not entry.get("counted")
            and _play_counts(pos / 1000.0, dur)):
        entry["counted"] = True
        await _record_subsonic_play(store, user, track)
    return _ok(fmt=f)


@_route("/getNowPlaying")
@_route("/getNowPlaying.view")
@_wrap
async def get_now_playing(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    ctx = _song_ctx(store, user)
    now = time.time()
    out = []
    for (uid, player), e in sorted(_NOW_PLAYING.items(), key=lambda kv: -kv[1]["at"]):
        tr = store.get_track(e["track"])
        sub = int(e.get("sub") or 0)
        dur = float(_tune_duration(tr, sub)) if tr else 0.0
        # A tune of unknown length expires like its file would.
        if not tr or _np_expired(e, dur or _safe_int(tr.get("duration")), now):
            _NOW_PLAYING.pop((uid, player), None)
            continue
        row = _tune_child(_track_to_song(tr, ctx), tr, sub, ctx)
        row.update({"username": e["username"], "minutesAgo": int((now - e["at"]) // 60),
                    "playerId": _player_id(uid, player)})
        if player:
            row["playerName"] = player
        if e.get("state") is not None:
            row["state"] = e["state"]
            row["positionMs"] = _np_position_ms(e, int(dur * 1000), now)
            row["playbackRate"] = float(e.get("rate") or 1.0)
        out.append(row)
    return _ok({"nowPlaying": {"entry": out}}, fmt=f)


async def forget_user(uid: str) -> None:
    """Drop everything the Subsonic surface keeps for a deleted user: their
    stars, ratings, play queue and bookmarks (persisted state) and their
    now-playing entries.  Called by Subsonic deleteUser and by the web UI's
    DELETE /api/users/{id}."""
    await _get_state().delete_user(uid)
    for key in [k for k in _NOW_PLAYING if k[0] == uid]:
        _NOW_PLAYING.pop(key, None)


# ── User management ──────────────────────────────────────────────────────────
# Mapped onto the app's user store and its three roles.  Subsonic's many role
# flags collapse onto them: adminRole → admin; any content-changing role
# (download, upload, cover art, comment, podcast, share, jukebox, playlist) →
# edit; otherwise readonly (``settingsRole`` is only "may change own settings",
# which every account can).  Admin-only like the web UI's user admin; a user
# may change their own password.  Password hashes never leave the store.
#
# Account-changing calls demand the MAIN password (``p=``, verified with
# scrypt against the login hash like the web UI's change-password — never the
# O(1) plaintext copy): a token-mode URL (u+t+s) is replayable from any
# captured stream / cover-art link and an API key is a bearer credential —
# neither may create admins or rotate passwords.

_EDIT_FLAGS = ("downloadRole", "uploadRole", "coverArtRole", "commentRole",
               "podcastRole", "shareRole", "jukeboxRole", "playlistRole")


async def _require_account_auth(request, sb_session, u, p, s, t) -> User:
    """Explicit ``u`` + ``p`` (or HTTP Basic) checked against the main login
    password hash — no cookie, no token mode, no API key, no plaintext-copy
    fast path."""
    if request.query_params.get("apiKey") is not None:
        if u or p or s or t or _has_basic_auth(request):
            raise _SubsonicError(43, "Multiple conflicting authentication mechanisms provided.")
        raise _SubsonicError(42, "Changing accounts requires password authentication "
                                 "(u + p) — an API key can't be used for this.")
    _refuse_password_and_token(u, p, s, t)
    if not u and _has_basic_auth(request):
        import base64
        try:
            u, _, p = base64.b64decode(request.headers["authorization"][6:]).decode("utf-8") \
                .partition(":")
        except Exception:                 # noqa: BLE001
            u = p = None
    if not u or p is None:
        raise _SubsonicError(42, "Changing accounts requires password authentication "
                                 "(u + p) — token auth can't be used for this; or use "
                                 "the SoniqBoom web UI.")
    store = get_user_store()
    # scrypt off the event loop, as the web UI's change-password does.
    user = await asyncio.to_thread(store.authenticate, u, _decode_password(p))
    if user is None or not user.enabled:
        raise _SubsonicError(40, "Wrong username or password (account changes need the "
                                 "main login password).")
    return user


def _can_download(user) -> bool:
    """The download role getUser advertises (``downloadRole``) — and
    /rest/download enforces: admin / edit."""
    return getattr(user, "role", None) in ("admin", "edit")


def _user_payload(target: User) -> dict:
    folders = [x["id"] for x in _music_folders(get_store())] or [_FOLDER_ID]
    return {
        "username":          target.username,
        "email":             "",
        "scrobblingEnabled": bool(target.lastfm_session_key or target.listenbrainz_token),
        "adminRole":         target.role == "admin",
        # "May change own settings" — every account can (changePassword on
        # itself), and a disabled one can't sign in at all.
        "settingsRole":      bool(getattr(target, "enabled", True)),
        "downloadRole":      _can_download(target),
        "uploadRole":        target.role == "admin",
        # Read-only accounts can't create / edit playlists (createPlaylist
        # refuses them), so don't advertise the role to them.
        "playlistRole":      target.role in ("admin", "edit"),
        "coverArtRole":      target.role == "admin",
        "commentRole":       False,
        "podcastRole":       False,
        "streamRole":        True,
        "jukeboxRole":       target.role in ("admin", "edit"),
        "shareRole":         False,
        "videoConversionRole": False,
        "folder":            folders,
    }


def _role_from_flags(params, current: dict | None) -> str | None:
    """Role implied by the Subsonic role flags: the ``current`` flags (the
    target user's, for updateUser — so sending just one flag never flips
    the rest) overlaid with those present in ``params``.  ``None`` when the
    request carries no role flag at all."""
    given = {}
    for name in ("adminRole", *_EDIT_FLAGS):
        v = params.get(name)
        if v is not None:
            given[name] = str(v).strip().lower() in ("1", "true", "yes", "on")
    if not given:
        return None
    flags = {**(current or {}), **given}
    if flags.get("adminRole"):
        return "admin"
    return "edit" if any(flags.get(n) for n in _EDIT_FLAGS) else "readonly"


async def _require_admin_user(request, sb_session, u, p, s, t):
    user = await _require_account_auth(request, sb_session, u, p, s, t)
    if user.role != "admin":
        raise _SubsonicError(50, "Only an admin can manage users.")
    return user


async def _close_user_sockets(user_id: str) -> None:
    """Drop the user's open WebSockets after a delete / demotion, exactly as
    the web UI's user admin does (best-effort)."""
    try:
        from soniqboom.api.users import close_open_ws_for
        await close_open_ws_for(user_id)
    except Exception:                     # noqa: BLE001
        log.debug("close_open_ws_for failed for %s", user_id, exc_info=True)


@_route("/getUsers")
@_route("/getUsers.view")
@_wrap
async def get_users(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    me = _require_user(request, sb_session, u, p, s, t)
    if me.role != "admin":
        raise _SubsonicError(50, "Only an admin can list users.")
    return _ok({"users": {"user": [_user_payload(x)
                                   for x in get_user_store().list_users()]}}, fmt=f)


@_route("/createUser")
@_route("/createUser.view")
@_wrap
async def create_user(
    request: Request,
    username: str = Query(...),
    password: str = Query(...),
    email: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    await _require_admin_user(request, sb_session, u, p, s, t)
    role = _role_from_flags(request.query_params, None) or "readonly"
    try:
        get_user_store().create(username=username, password=_decode_password(password),
                                role=role)
    except ValueError as exc:
        raise _SubsonicError(10 if "taken" not in str(exc) else 0, str(exc))
    return _ok(fmt=f)


@_route("/updateUser")
@_route("/updateUser.view")
@_wrap
async def update_user(
    request: Request,
    username: str = Query(...),
    password: str | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    await _require_admin_user(request, sb_session, u, p, s, t)
    ustore = get_user_store()
    target = ustore.get_by_username(username)
    if target is None:
        raise _SubsonicError(70, "User not found.")
    role = _role_from_flags(request.query_params, _user_payload(target))
    new_pw = _decode_password(password) if password else None
    if new_pw is not None:
        # Validate BEFORE changing anything — a rejected password must not
        # leave a half-applied role change behind.
        from soniqboom.core.users import validate_password
        try:
            validate_password(new_pw)
        except ValueError as exc:
            raise _SubsonicError(10, str(exc))
    demoted = role is not None and role != target.role and target.role == "admin"
    try:
        if role is not None and role != target.role:
            ustore.update(target.id, role=role)
        if new_pw is not None:
            ustore.set_password(target.id, new_pw)
    except ValueError as exc:            # e.g. demoting the last admin
        raise _SubsonicError(0, str(exc))
    if demoted or (role == "readonly" and target.role != "readonly"):
        await _close_user_sockets(target.id)
    return _ok(fmt=f)


@_route("/deleteUser")
@_route("/deleteUser.view")
@_wrap
async def delete_user(
    request: Request,
    username: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    me = await _require_admin_user(request, sb_session, u, p, s, t)
    ustore = get_user_store()
    target = ustore.get_by_username(username)
    if target is None:
        raise _SubsonicError(70, "User not found.")
    if target.id == me.id:
        raise _SubsonicError(50, "You can't delete your own account.")
    try:
        ustore.delete(target.id)          # refuses the last enabled admin
    except ValueError as exc:
        raise _SubsonicError(0, str(exc))
    await _close_user_sockets(target.id)
    # Their Subsonic state (stars, queue, bookmarks) and now-playing entries go too.
    await forget_user(target.id)
    return _ok(fmt=f)


@_route("/changePassword")
@_route("/changePassword.view")
@_wrap
async def change_password(
    request: Request,
    username: str = Query(...),
    password: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    me = await _require_account_auth(request, sb_session, u, p, s, t)
    ustore = get_user_store()
    target = ustore.get_by_username(username)
    if target is None:
        raise _SubsonicError(70, "User not found.")
    if target.id != me.id and me.role != "admin":
        raise _SubsonicError(50, "You can only change your own password.")
    try:
        # Rotates the login password AND the Subsonic API password (the store
        # keeps them in sync), and ends that user's web sessions.
        ustore.set_password(target.id, _decode_password(password))
    except ValueError as exc:
        raise _SubsonicError(10, str(exc))
    return _ok(fmt=f)


@_media_route("/getAvatar")
@_media_route("/getAvatar.view")
@_wrap
async def get_avatar(
    request: Request,
    username: str = Query(...),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    # SoniqBoom has no user avatars: every username gets the app icon — the
    # same answer for unknown names, so this can't be used to probe which
    # accounts exist.
    _require_user(request, sb_session, u, p, s, t)
    return _placeholder_art()


# ── search (Subsonic ≤1.3, deprecated) ──────────────────────────────────────

@_route("/search")
@_route("/search.view")
@_wrap
async def search_v1(
    request: Request,
    artist: str | None = None,
    album: str | None = None,
    title: str | None = None,
    any: str | None = None,
    count: int = Query(20, ge=0),
    offset: int = Query(0, ge=0),
    newerThan: int | None = None,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    # Word search over the given fields (the store's own tokenised index, same
    # as search3's songs); ``newerThan`` (ms) keeps songs added after it.
    user = _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    count = min(count, 500)
    q = " ".join(x.strip() for x in (any, artist, album, title) if x and x.strip())
    if not q or not any_word(q):
        return _ok({"searchResult": {"offset": offset, "totalHits": 0, "match": []}}, fmt=f)
    ids = store.filter_track_ids(query=q.lower())
    if newerThan:
        cutoff = newerThan / 1000.0
        ids = [i for i in ids if ((store.get_track(i) or {}).get("added_at") or 0) > cutoff]
    tracks = store._tracks
    ids.sort(key=lambda i: ((tracks.get(i) or {}).get("added_at") or 0, i), reverse=True)
    ctx = _song_ctx(store, user)
    page = [tr for tr in store.get_tracks_batch(ids[offset:offset + count]) if tr]
    return _ok({"searchResult": {"offset": offset, "totalHits": len(ids),
                                 "match": [_track_to_song(tr, ctx) for tr in page]}}, fmt=f)


def any_word(q: str) -> bool:
    return any(ch.isalnum() for ch in q)


# ── Features SoniqBoom doesn't have: spec-valid empties / clear errors ───────
# Shares, podcasts, videos and chat have no backing feature.  List endpoints
# answer with the spec's (empty) collection so clients render "nothing here";
# mutating / fetching endpoints answer a Subsonic error naming what's missing
# (code 70 for "that item doesn't exist", 0 for "not supported") — never the
# SPA's HTML and never a 500.  getUser advertises shareRole / podcastRole =
# false, so well-behaved clients hide these sections anyway.

def _empty_endpoint(paths: tuple[str, ...], payload: dict):
    async def handler(
        request: Request,
        sb_session: str | None = Cookie(default=None),
        u: str | None = Query(default=None),
        p: str | None = Query(default=None),
        s: str | None = Query(default=None),
        t: str | None = Query(default=None),
        f: str = Query(default="xml"),
    ):
        _require_user(request, sb_session, u, p, s, t)
        return _ok(payload, fmt=f)
    handler.__name__ = "empty_" + paths[0].strip("/").replace(".", "_")
    wrapped = _wrap(handler)
    for path in paths:
        _route(path)(wrapped)


def _unsupported_endpoint(paths: tuple[str, ...], code: int, message: str):
    async def handler(
        request: Request,
        sb_session: str | None = Cookie(default=None),
        u: str | None = Query(default=None),
        p: str | None = Query(default=None),
        s: str | None = Query(default=None),
        t: str | None = Query(default=None),
        f: str = Query(default="xml"),
    ):
        _require_user(request, sb_session, u, p, s, t)
        raise _SubsonicError(code, message)
    handler.__name__ = "unsupported_" + paths[0].strip("/").replace(".", "_")
    wrapped = _wrap(handler)
    for path in paths:
        _route(path)(wrapped)


def _views(name: str) -> tuple[str, str]:
    return (f"/{name}", f"/{name}.view")


_empty_endpoint(_views("getShares"), {"shares": {"share": []}})
_empty_endpoint(_views("getPodcasts"), {"podcasts": {"channel": []}})
_empty_endpoint(_views("getNewestPodcasts"), {"newestPodcasts": {"episode": []}})
_empty_endpoint(_views("getVideos"), {"videos": {"video": []}})
_empty_endpoint(_views("getChatMessages"), {"chatMessages": {"chatMessage": []}})
for _name in ("createShare", "updateShare", "deleteShare"):
    _unsupported_endpoint(_views(_name), 0, "Sharing is not supported by this server.")
for _name in ("refreshPodcasts", "createPodcastChannel", "deletePodcastChannel",
              "deletePodcastEpisode", "downloadPodcastEpisode"):
    _unsupported_endpoint(_views(_name), 0, "Podcasts are not supported by this server.")
_unsupported_endpoint(_views("getPodcastEpisode"), 70, "Podcast episode not found.")
_unsupported_endpoint(_views("getVideoInfo"), 70, "Video not found.")
_unsupported_endpoint(_views("getCaptions"), 70, "Video not found.")
_unsupported_endpoint(("/hls.m3u8", "/hls.m3u8.view", "/hls", "/hls.view"), 0,
                      "HLS streaming is not supported by this server — use stream.")
_unsupported_endpoint(_views("addChatMessage"), 0, "Chat is not supported by this server.")


# ── Unknown-method catch-all — MUST stay the LAST route on this router ───────
# Registered after every real endpoint, so it only sees /rest/* paths nothing
# above matched.  Without it an unknown method fell through to the SPA fallback
# and answered HTTP 200 text/html — which clients parse as a broken server
# instead of "this server doesn't implement X".  Mirrors the disabled-service
# stub in main.py: a Subsonic failed envelope (code 0) at HTTP 404, in the
# requested ``f`` format (GET query or formPost body).  Bare ``/rest`` too.

@router.api_route("/{rest:path}", methods=["GET", "POST"], include_in_schema=False)
@router.api_route("", methods=["GET", "POST"], include_in_schema=False)
async def unknown_method(request: Request, rest: str = ""):
    name = rest.rsplit("/", 1)[-1]
    if name.endswith(".view"):
        name = name[:-5]
    # Printable characters only: a control char (``%01``) is not legal in
    # XML 1.0 at all, escaped or not, and would make the envelope unparseable.
    name = "".join(ch for ch in name[:64] if ch.isprintable()) or "(none)"
    resp = _err(0, f"Unknown Subsonic method '{name}' — not implemented by this server.",
                fmt=request.query_params.get("f", "xml"))
    resp.status_code = 404
    return resp


# ── HEAD on non-media methods ────────────────────────────────────────────────
# The media routes answer HEAD themselves (``_media_route``); every other
# method is GET/POST only — and must stay so: a HEAD must never run a
# mutating handler (createPlaylist, star, scrobble …).  This HEAD-only
# catch-all is a FULL match where those routes are only PARTIAL ones (right
# path, wrong method), so Starlette picks it instead of answering 405 — with
# headers only: 200 and the envelope's content type for a method this server
# implements, 404 otherwise.  No auth, no handler work.

_KNOWN_METHODS: dict = {"names": None}


def _known_method(name: str) -> bool:
    names = _KNOWN_METHODS["names"]
    if names is None:
        names = set()
        for r in router.routes:
            path = getattr(r, "path", "") or ""
            if path.startswith("/rest/") and "{" not in path:
                n = path[len("/rest/"):]
                names.add(n[:-5] if n.endswith(".view") else n)
        _KNOWN_METHODS["names"] = names
    return name in names


@router.api_route("/{rest:path}", methods=["HEAD"], include_in_schema=False)
@router.api_route("", methods=["HEAD"], include_in_schema=False)
async def head_method(request: Request, rest: str = ""):
    name = rest.rsplit("/", 1)[-1]
    if name.endswith(".view"):
        name = name[:-5]
    fmt = (request.query_params.get("f") or "xml").lower()
    media = "application/json" if fmt in ("json", "jsonp") else _XML_MEDIA
    resp = Response(status_code=200 if name and _known_method(name) else 404,
                    media_type=media)
    del resp.headers["content-length"]
    return resp
