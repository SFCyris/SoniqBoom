# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenSubsonic-compatible REST API (``/rest/*``).

The bare minimum needed by the major Subsonic clients — DSub,
Substreamer, play:Sub, Symfonium, Tempo, Sublime Music, Airsonic-refix,
Feishin — so SoniqBoom inherits their mobile + desktop ecosystem
without writing a native app.

Spec reference: https://www.subsonic.org/pages/api.jsp
OpenSubsonic extensions: https://opensubsonic.netlify.app/docs/

**Auth.** Subsonic supports two query-string auth modes:
``u + p`` (password plain or ``enc:hex``) and ``u + s + t`` (salt + md5
token).  The token mode requires the server to know the plaintext
password, which is incompatible with SoniqBoom's scrypt hashes — we
reject it with error code 41 and recommend clients use password mode.
Several clients (DSub, Substreamer, Symfonium) tunnel ``enc:<hex>``
passwords over HTTPS to avoid plaintext in network logs.

**IDs.**  Tracks have first-class IDs; artists and albums are derived
views, so we synthesise stable IDs of the form ``ar:<sha1-of-key>``,
``al:<sha1-of-key>``.  These are stable across restarts because they
hash the canonical lowercased name.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import logging
import math
import time
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from soniqboom import __version__
from soniqboom.config import settings
from soniqboom.core.store import get_store
from soniqboom.core.users import get_user_store
from soniqboom.models.user import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/rest", tags=["subsonic"])

# Subsonic API version we claim to implement.  1.16.1 is the latest
# stable version most clients require for getArtists / getAlbumList2.
_SUBSONIC_API_VERSION = "1.16.1"
_SERVER_NAME = "SoniqBoom"
_FOLDER_ID = "0"               # we expose one virtual music folder
_FOLDER_NAME = "Library"


# ── Envelope helpers ─────────────────────────────────────────────────────────

# The OpenSubsonic extensions we genuinely implement.  Advertised in every
# response envelope AND returned by the dedicated getOpenSubsonicExtensions
# endpoint, so the two never drift.  ONLY list ones we implement — clients gate
# features on this list, so a phantom entry makes them call routes that 404.
#   songLyrics → getLyrics + getLyricsBySongId (structured/synced)
# NOTE: getSimilarSongs / getSimilarSongs2 (core/similar.py) are CORE Subsonic
# endpoints (≥1.11.0) discovered via the API version, NOT the OpenSubsonic
# `sonicSimilarity` extension (a different pair — getSonicSimilarTracks /
# findSonicPath — we don't implement).
_OPENSUBSONIC_EXTENSIONS = [
    {"name": "songLyrics", "versions": [1]},
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
        "openSubsonicExtensions": _OPENSUBSONIC_EXTENSIONS,
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


def _envelope_to_xml(envelope: dict) -> bytes:
    body = envelope.get("subsonic-response", {})
    root = _ET.Element("subsonic-response",
                       attrib={"xmlns": "http://subsonic.org/restapi"})
    for k, v in body.items():
        if isinstance(v, (dict, list)):
            _xml_walk(root, k, v)
        elif v is not None:
            root.set(k, _xml_scalar(v))
    return (b'<?xml version="1.0" encoding="UTF-8"?>\n'
            + _ET.tostring(root, encoding="utf-8"))


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


def _ok(payload: dict[str, Any] | None = None, *, fmt: str = "xml") -> Response:
    data = _envelope(payload)
    f = (fmt or "xml").lower()
    if f in ("json", "jsonp"):
        # Some clients hand in ``f=jsonp&callback=fn`` — we honour the
        # callback wrapper but the response body itself is identical.
        return JSONResponse(_json_normalize(data))
    # Spec default — XML.
    return Response(content=_envelope_to_xml(data),
                    media_type="application/xml")


def _err(code: int, message: str, *, fmt: str = "xml") -> Response:
    body = _envelope({"error": {"code": code, "message": message}}, status="failed")
    # Subsonic clients expect HTTP 200 even for protocol errors — the
    # error code lives in the envelope.
    f = (fmt or "xml").lower()
    if f in ("json", "jsonp"):
        return JSONResponse(body, status_code=200)
    return Response(content=_envelope_to_xml(body),
                    media_type="application/xml",
                    status_code=200)


# ── ID helpers ───────────────────────────────────────────────────────────────

def _sha(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _artist_id(name: str) -> str:
    return "ar:" + _sha((name or "").lower())


def _album_id(album_artist: str, album: str) -> str:
    return "al:" + _sha((album_artist or "").lower(), (album or "").lower())


# ── Reverse-ID + album-list cache ─────────────────────────────────────────
# Decoding ``ar:<hash>`` / ``al:<hash>`` back to a name is O(N) per call
# because we can't reverse a hash — we have to scan and compare.  Subsonic
# clients call these endpoints often (getArtist + getAlbum + getCoverArt
# on every panel refresh), so we cache the reverse maps and invalidate
# whenever the store's mutation_seq bumps (any track upsert/update/delete).
#
# The ``_REVERSE_CACHE`` and ``_ALBUM_LIST_CACHE`` use a *debounced*
# invalidation strategy: during a watcher-triggered rescan, the seq
# bumps every WRITE_CHUNK=25 tracks.  Naive invalidation would rebuild
# the cache hundreds of times during a scan.  Instead we record the
# last-seen seq and rebuild at most every ``_CACHE_DEBOUNCE_SEC``.

_CACHE_DEBOUNCE_SEC = 5.0

_REVERSE_CACHE: dict = {"seq": None, "ar": {}, "al": {}, "built_at": 0.0}
_ALBUM_LIST_CACHE: dict = {
    "seq": None,
    "seen": {},     # (aa, al) → sample-track dict
    "built_at": 0.0,
}


def _refresh_reverse_cache(store) -> None:
    seq = store._mutation_seq
    now = time.time()
    # Cache hit: same seq as last build.
    if _REVERSE_CACHE["seq"] == seq:
        return
    # Debounce: even when seq has moved, don't rebuild if we just did.
    # During an active scan, seq bumps every ~25 tracks; without this
    # check, every Subsonic poll would trigger a full rebuild.
    if (now - _REVERSE_CACHE["built_at"]) < _CACHE_DEBOUNCE_SEC and _REVERSE_CACHE["seq"] is not None:
        return
    ar_map: dict[str, str] = {}
    al_map: dict[str, tuple[str, str]] = {}
    # album_artist primary (preferred), then bare artist for the same hash
    # so artists without an album_artist tag still resolve.
    for entry in store.aggregate_album_artists():
        name = entry["album_artist"]
        if not name:
            continue
        ar_map.setdefault(_artist_id(name), name)
    for entry in store.aggregate_artists():
        name = entry["artist"]
        if not name:
            continue
        ar_map.setdefault(_artist_id(name), name)
    # Albums: walk each album-artist, then fallback artist.
    for aa_entry in store.aggregate_album_artists():
        aa = aa_entry["album_artist"]
        if not aa:
            continue
        for al_entry in store.aggregate_albums(album_artist=aa):
            al_map.setdefault(_album_id(aa, al_entry["album"]),
                              (aa, al_entry["album"]))
    for ar_entry in store.aggregate_artists():
        ar = ar_entry["artist"]
        if not ar:
            continue
        for al_entry in store.aggregate_albums(artist=ar):
            al_map.setdefault(_album_id(ar, al_entry["album"]),
                              (ar, al_entry["album"]))
    _REVERSE_CACHE["seq"] = seq
    _REVERSE_CACHE["ar"]  = ar_map
    _REVERSE_CACHE["al"]  = al_map
    _REVERSE_CACHE["built_at"] = now


def _refresh_album_list_cache(store) -> dict[tuple[str, str], dict]:
    """Return the ``(album_artist, album) → sample-track`` map for
    ``getAlbumList2``.

    The map itself is built + memoised by ``store.album_sample_index()`` (the
    same ``_mutation_seq``-keyed cache that backs ``aggregate_albums`` etc.),
    so it's recomputed only on real mutation and can never drift from the track
    table — replacing the old inline ``all_track_metas()`` re-walk.  This
    wrapper keeps the short debounce: a watcher-driven rescan bumps the seq
    every ~25 tracks, and the debounce caps the O(n) rebuild to at most once
    per ``_CACHE_DEBOUNCE_SEC`` even if a Subsonic client polls throughout the
    scan (the store's per-seq memo alone wouldn't rate-limit across seq bumps).
    """
    seq = store._mutation_seq
    now = time.time()
    if _ALBUM_LIST_CACHE["seq"] == seq:
        return _ALBUM_LIST_CACHE["seen"]
    if (now - _ALBUM_LIST_CACHE["built_at"]) < _CACHE_DEBOUNCE_SEC and _ALBUM_LIST_CACHE["seq"] is not None:
        return _ALBUM_LIST_CACHE["seen"]
    seen = store.album_sample_index()
    _ALBUM_LIST_CACHE["seq"] = seq
    _ALBUM_LIST_CACHE["seen"] = seen
    _ALBUM_LIST_CACHE["built_at"] = now
    return seen


# Per-type SORTED album list, memoised on the same seq as the (aa,al)->sample map.
# getAlbumList[2] used to re-materialise ``list(seen.items())`` and re-sort it
# (O(A log A) over 20-50k albums) ON THE EVENT LOOP on every poll; the
# deterministic sorts only change when the album set does, so cache one list per
# type.  Returns the cached list REFERENCE — callers must slice / comprehend it
# (never mutate it in place).
_ALBUM_SORT_CACHE: dict = {"seq": None, "lists": {}}


def _sorted_album_items(store, sort_type: str) -> list:
    seen = _refresh_album_list_cache(store)          # debounced (aa,al)->sample map
    seq = _ALBUM_LIST_CACHE["seq"]
    if _ALBUM_SORT_CACHE["seq"] != seq:
        _ALBUM_SORT_CACHE["seq"] = seq
        _ALBUM_SORT_CACHE["lists"] = {}              # invalidate every per-type list at once
    cached = _ALBUM_SORT_CACHE["lists"].get(sort_type)
    if cached is not None:
        return cached
    items = list(seen.items())
    if sort_type == "newest":
        items.sort(key=lambda a: a[1].get("added_at") or 0, reverse=True)
    elif sort_type == "alphabeticalByName":
        items.sort(key=lambda a: a[0][1].lower())
    elif sort_type == "alphabeticalByArtist":
        items.sort(key=lambda a: (a[0][0].lower(), a[0][1].lower()))
    elif sort_type == "byYear":
        items.sort(key=lambda a: _normalise_year(a[1].get("year")))   # ascending; reverse handled by the caller
    # "__unsorted__" (recent/frequent/highest/starred/byGenre) keeps seen.items() order
    _ALBUM_SORT_CACHE["lists"][sort_type] = items
    return items


def _decode_artist_id(raw: str, store) -> str | None:
    """Reverse ``ar:<hash>`` → artist name.  O(1) after first lookup
    in a stable library."""
    if not raw.startswith("ar:"):
        return None
    _refresh_reverse_cache(store)
    return _REVERSE_CACHE["ar"].get(raw)


def _decode_album_id(raw: str, store) -> tuple[str | None, str | None]:
    """Reverse ``al:<hash>`` → (album_artist, album).  O(1) cached."""
    if not raw.startswith("al:"):
        return (None, None)
    _refresh_reverse_cache(store)
    hit = _REVERSE_CACHE["al"].get(raw)
    return hit if hit else (None, None)


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


def _track_to_song(t: dict) -> dict:
    """Map a SoniqBoom track dict to a Subsonic ``Child`` (song) object."""
    aa = t.get("album_artist") or t.get("artist") or ""
    al = t.get("album") or ""
    genre_list = t.get("genre") or []
    genre = genre_list[0] if genre_list else ""
    raw_fmt = (t.get("format") or "").split("/", 1)[0]
    suffix = (raw_fmt.lower() or "mp3")
    content_type = _content_type(raw_fmt)
    out: dict = {
        "id":         t["id"],
        "parent":     _album_id(aa, al),
        "isDir":      False,
        "title":      t.get("title") or "",
        "album":      al,
        "artist":     t.get("artist") or aa or "",
        "albumArtist": aa,
        "track":      _safe_int(t.get("track_number")),
        "year":       _normalise_year(t.get("year")),
        "genre":      genre,
        "coverArt":   t["id"],
        "size":       _safe_int(t.get("file_size")),
        "contentType": content_type,
        "suffix":     suffix,
        "duration":   _safe_int(t.get("duration"), round_=True),
        "bitRate":    _safe_int((t.get("bitrate") or 0) / 1000),
        "path":       t.get("path") or "",
        "isVideo":    False,
        "type":       "music",
        "albumId":    _album_id(aa, al),
        "artistId":   _artist_id(aa),
        "discNumber": _safe_int(t.get("disc_number")),
        "created":    _iso(t.get("added_at")),
    }
    # ── OpenSubsonic optional fields ────────────────────────────────────────
    # Capability-aware clients (Symfonium, Amperfy, Feishin) read these to show
    # bit depth / sample rate, apply ReplayGain themselves, and render
    # multi-valued genres.  Only emit a field when the track actually carries
    # it, so a missing tag never advertises a wrong value.
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
    if genre_list:
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
    if rg:
        out["replayGain"] = rg
    # OpenSubsonic transcodedContentType / transcodedSuffix — advertise
    # the codec we'll actually DELIVER for sources the byte server
    # cannot serve natively (DSD, ALAC, AIFF, tracker, SID, MIDI, GME).
    # Without these fields, Amperfy / DSub / Symfonium read ``contentType``
    # to predict what bytes they'll receive; when the source contentType
    # says "audio/x-dsd" but the byte server delivers MP3/FLAC bytes, the
    # decoder fails — the symptom the user reported with Amperfy + DSF:
    # "streams something but no sound", and on pause/resume the player
    # immediately stops because its session is in error state.
    if raw_fmt.upper() in _ALWAYS_TRANSCODED:
        # Default transcode target is the server-configured fallback
        # (MP3 for OpenSubsonic compatibility); the client may still
        # ask for a specific ``format=`` via the stream URL.
        from soniqboom.config import settings as _s
        target_fmt = (getattr(_s, "transcode_format", None) or "mp3").lower()
        # Map target codec → MIME we'll send + filename suffix.
        _DELIVER_MIME = {
            "mp3":  "audio/mpeg",
            "flac": "audio/flac",
            "ogg":  "audio/ogg",
            "wav":  "audio/wav",
        }
        out["transcodedContentType"] = _DELIVER_MIME.get(target_fmt, "audio/mpeg")
        out["transcodedSuffix"]      = "mp3" if target_fmt == "mp3" else target_fmt
    return out


def _content_type(fmt: str) -> str:
    """Map a stored format label to a MIME type the Subsonic client expects.

    Defaulting to ``audio/mpeg`` was a footgun: any format the table
    didn't list (DSD, MOD, SID, MIDI, …) was advertised as MP3 to the
    client, which then refused to play the stream it actually received
    or — worse — played a few bytes of header as silence.  Surface a
    distinct type for DSD/lossless variants and a generic ``audio/x-N``
    fall-through so the contentType matches the source bytes, while
    callers that intend to transcode advertise ``transcodedContentType``
    + ``transcodedSuffix`` separately (see ``_song_payload``).
    """
    f = (fmt or "").split("/", 1)[0].upper()
    return {
        "MP3":   "audio/mpeg",
        "FLAC":  "audio/flac",
        "WAV":   "audio/wav",
        "AIFF":  "audio/aiff",
        "OGG":   "audio/ogg",
        "OPUS":  "audio/ogg",
        "AAC":   "audio/mp4",
        "ALAC":  "audio/mp4",
        "M4A":   "audio/mp4",
        # DSD container formats — scanner stores them all as "DSD"
        # currently; emit a DSD-aware MIME so the client doesn't think
        # the source is MP3 and proceed to play noise.
        "DSD":   "audio/x-dsd",
        "DSF":   "audio/x-dsd",
        "DFF":   "audio/x-dsd",
        "WSD":   "audio/x-dsd",
        # Tracker / SID / MIDI sources — no consumer-facing MIME for the
        # raw formats; report a generic audio/* and rely on the
        # transcoded-content-type advertisement to tell the client what
        # we'll actually deliver.
        "MOD":   "audio/x-mod",
        "S3M":   "audio/x-mod",
        "XM":    "audio/x-mod",
        "IT":    "audio/x-mod",
        "SID":   "audio/prs.sid",
        "MIDI":  "audio/midi",
        "MID":   "audio/midi",
    }.get(f, f"audio/x-{f.lower()}" if f else "application/octet-stream")


# Source formats the Subsonic byte server cannot deliver natively —
# the underlying bytes are always re-encoded before they hit the wire.
# Used to populate ``transcodedContentType`` / ``transcodedSuffix`` on
# the Child payload so the client (Amperfy, DSub, …) plays the codec
# we DELIVER, not the source codec we STORE.
_ALWAYS_TRANSCODED = {
    "DSD", "DSF", "DFF", "WSD",
    "ALAC",
    "AIFF",
    "MOD", "S3M", "XM", "IT", "MTM", "MED", "OCT", "669", "DBM",
    "ULT", "STM", "FAR", "AMF", "GDM", "IMF", "OKT", "SFX", "WOW", "DSM",
    "AHX", "HVL",
    "SID", "PSID",
    "MIDI", "MID", "KAR",
    "NSF", "NSFE", "SPC", "GBS", "VGM", "VGZ", "AY", "KSS", "SAP", "GYM", "HES",
}


def _normalise_year(y: Any) -> int:
    """Collapse YYYYMMDD-style ints to the year and coerce string years
    to int — Subsonic clients expect a plain integer."""
    if isinstance(y, str) and y.isdigit():
        y = int(y)
    if isinstance(y, int) and y > 9999:
        y = y // 10000
    return int(y) if isinstance(y, int) else 0


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        # A bad timestamp — non-finite (NaN/inf, e.g. a corrupt or tampered
        # persistence snapshot reintroducing one via default json.loads, which
        # accepts NaN/Infinity), non-numeric, or out of range for the platform
        # time_t — must not make time.gmtime raise and, via @_wrap, blank the
        # WHOLE listing.  Same "one bad record can't kill the response"
        # rationale as _safe_int; degrade to an empty string.
        return ""


# ── Auth ─────────────────────────────────────────────────────────────────────

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

    Token mode works only when the target user has opted in to a
    plaintext **Subsonic API password** (``user.subsonic_password``).
    That's the convention every Subsonic-compatible server uses
    (Navidrome, Airsonic, Funkwhale, Gonic) — it keeps the main
    browser-login password behind scrypt while letting third-party
    Subsonic clients (Amperfy, DSub, Symfonium, play:Sub …) use their
    spec-default token auth.
    """
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

    # 3. Subsonic password mode (?p=password or ?p=enc:hex).
    #    Try the main scrypt password first; if that fails, fall back to
    #    the user's Subsonic-API password (so a user who set only the
    #    Subsonic password can still authenticate via plain mode).
    if u and p is not None:
        plain = _decode_password(p)
        user = store.authenticate(u, plain)
        if user:
            return user
        # Fallback: per-user Subsonic password.  Look up the record
        # without password verification and constant-time compare the
        # plaintext.  ``find_by_username`` is a bare lookup, not auth.
        cand = store.get_by_username(u) if hasattr(store, "get_by_username") else None
        if cand and cand.subsonic_password and hmac.compare_digest(
            plain, cand.subsonic_password
        ):
            return cand
        return None

    # 4. Subsonic token mode (?s=salt&t=md5(p+salt)) — supported only
    #    when the user has set a Subsonic API password.  The token is
    #    md5(subsonic_password + salt); we recompute and constant-time
    #    compare against the client's ``t`` parameter.
    if u and s is not None and t is not None:
        cand = store.get_by_username(u) if hasattr(store, "get_by_username") else None
        if cand and cand.subsonic_password:
            expected = hashlib.md5(
                (cand.subsonic_password + s).encode("utf-8")
            ).hexdigest()
            if hmac.compare_digest(expected.lower(), t.lower()):
                return cand
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


def _require_user(
    request: Request,
    sb_session: str | None,
    u: str | None, p: str | None, s: str | None, t: str | None,
) -> User:
    """Resolve + check enabled; raise the appropriate Subsonic error."""
    user = _resolve_user(request, sb_session, u, p, s, t)
    if user is None:
        if u and s is not None and t is not None:
            # Token mode failed — either the user hasn't set a Subsonic
            # API password yet, or the token didn't verify.  Distinguish
            # in the message so users know what to do (the "configure
            # API password" step) instead of just seeing "wrong password".
            raise _SubsonicError(40, "Wrong username, Subsonic API password, "
                                       "or token — set or rotate the per-user "
                                       "Subsonic password in Settings → My Account.")
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
    Subsonic password credentials (or HTTP Basic), we ensure the request
    came from a real Subsonic client, not a drive-by browser tab."""
    # Same-origin requests can also be allowed (the Origin header must
    # match the request host); this lets in-browser dev still work.
    origin = request.headers.get("origin") or ""
    same_origin = origin and (
        origin == f"{request.url.scheme}://{request.url.netloc}"
    )
    if u or s or t or _has_basic_auth(request) or same_origin:
        return _require_user(request, sb_session, u, p, s, t)
    raise _SubsonicError(
        40,
        "This endpoint requires Subsonic password credentials "
        "(u=&p=) — cookie-only auth is rejected on mutation endpoints "
        "to prevent CSRF.",
    )


def _has_basic_auth(request: Request) -> bool:
    a = request.headers.get("authorization", "")
    return a.lower().startswith("basic ")


class _SubsonicError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


# ── Dependency wrapper that converts _SubsonicError → JSON envelope ─────────

def _wrap(handler):
    """Decorator: any handler raising ``_SubsonicError`` returns a proper
    Subsonic error envelope instead of an HTTP 4xx.

    ``functools.wraps`` is essential here — FastAPI inspects the wrapped
    function's signature to know which query params, cookies, and
    dependencies to inject.  Without it the wrapper looks like
    ``(*args, **kwargs)`` and FastAPI rejects every call with 422."""
    @functools.wraps(handler)
    async def _wrapped(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except _SubsonicError as e:
            # Honour the caller's requested format on the error too — every
            # handler takes ``f`` as a keyword (FastAPI injects by name), so a
            # JSON client (Symfonium/Feishin) gets a JSON error envelope it can
            # parse rather than XML it chokes on.
            return _err(e.code, e.message, fmt=kwargs.get("f", "xml"))
        except HTTPException:
            # Auth/redirect helpers raise these deliberately (e.g. 401 with a
            # WWW-Authenticate header); let Starlette render them unchanged.
            raise
        except Exception:  # noqa: BLE001 — last-resort envelope, not a swallow
            # Any other unexpected failure would otherwise surface as a raw
            # HTTP 500 (or, for a non-finite float reaching JSONResponse, a
            # 500 from ``allow_nan=False``) — which Subsonic clients treat as a
            # transport error, not a protocol error, and often show as "server
            # unreachable".  Convert it to a generic Subsonic error envelope
            # (code 0) so the client shows a real message; log the traceback so
            # the operator can still see it.
            log.exception("Unhandled error in Subsonic handler %s", handler.__name__)
            return _err(0, "An internal server error occurred.",
                        fmt=kwargs.get("f", "xml"))
    return _wrapped


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/ping")
@router.get("/ping.view")
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


@router.get("/getOpenSubsonicExtensions")
@router.get("/getOpenSubsonicExtensions.view")
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
    # OpenSubsonic's dedicated extension-discovery endpoint.  The same list also
    # rides on every response envelope, but a strict client may call this route
    # directly — without it, the SPA catch-all would answer with HTML.
    _require_user(request, sb_session, u, p, s, t)
    return _ok({"openSubsonicExtensions": _OPENSUBSONIC_EXTENSIONS}, fmt=f)


@router.get("/getLicense")
@router.get("/getLicense.view")
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
            "licenseExpires": "2099-12-31T00:00:00",
        },
    }, fmt=f)


@router.get("/getMusicFolders")
@router.get("/getMusicFolders.view")
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
    return _ok({
        "musicFolders": {
            "musicFolder": [{"id": _FOLDER_ID, "name": _FOLDER_NAME}],
        },
    }, fmt=f)


def _index_letter(name: str) -> str:
    """Bucket label for the indexed artist list — Subsonic spec uses
    A-Z plus '#' for everything else."""
    ch = (name or "").strip()[:1].upper()
    if not ch:
        return "#"
    if "A" <= ch <= "Z":
        return ch
    return "#"


@router.get("/getArtists")
@router.get("/getArtists.view")
@router.get("/getIndexes")
@router.get("/getIndexes.view")
@_wrap
async def get_artists(
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
    # Prefer album_artist (TPE2) when populated; fall back to plain artist.
    rows: list[dict] = store.aggregate_album_artists() or []
    if not rows:
        rows = [
            {"album_artist": r["artist"], "count": r["count"]}
            for r in store.aggregate_artists()
        ]
    # Group into A-Z buckets.  ``albumCount`` was previously O(albums) per
    # artist via ``aggregate_albums(album_artist=name)`` — on a 50k-artist
    # library that's 50k sorts per request.  Read the precomputed per-
    # album-artist counter directly: O(1) per artist (load-test #1 P1-4).
    buckets: dict[str, list[dict]] = {}
    album_counter_map = store._agg_albums_by_album_artist
    for r in rows:
        name = r.get("album_artist") or ""
        if not name:
            continue
        letter = _index_letter(name)
        album_count = len(album_counter_map.get(name.lower(), ()))
        buckets.setdefault(letter, []).append({
            "id":         _artist_id(name),
            "name":       name,
            "albumCount": album_count,
        })
    indexed = [
        {"name": letter, "artist": sorted(buckets[letter], key=lambda a: a["name"].lower())}
        for letter in sorted(buckets.keys())
    ]
    # getIndexes uses a slightly different envelope (indexes.index)
    if request.url.path.endswith("getIndexes") or request.url.path.endswith("getIndexes.view"):
        return _ok({
            "indexes": {
                "lastModified": int(time.time() * 1000),
                "ignoredArticles": "The El La Los Las Le Les",
                "index": indexed,
            },
        }, fmt=f)
    return _ok({
        "artists": {
            "ignoredArticles": "The El La Los Las Le Les",
            "index": indexed,
        },
    }, fmt=f)


@router.get("/getArtist")
@router.get("/getArtist.view")
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
    _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    name = _decode_artist_id(id, store)
    if not name:
        raise _SubsonicError(70, "Artist not found.")
    albums_raw = store.aggregate_albums(album_artist=name)
    if not albums_raw:
        albums_raw = store.aggregate_albums(artist=name)
    albums = []
    seen = _refresh_album_list_cache(store)   # memoised (album_artist|artist, album) -> newest sample
    for a in albums_raw:
        # Cover/year/genre come from one sample track.  Use the memoised index (a
        # dict lookup) instead of a per-album filter_tracks scan that walks the
        # global added-at index until it hits a member — O(library) for an old
        # album.  Fall back to the case-insensitive tag lookup ONLY on a
        # key-normalization miss (the index keys on raw-case album names).
        sample = seen.get((name, a["album"]))
        if sample is None:
            ts = (store.filter_tracks(album_artist=name, album=a["album"], limit=1)
                  or store.filter_tracks(artist=name, album=a["album"], limit=1))
            sample = ts[0] if ts else {}
        albums.append({
            "id":         _album_id(name, a["album"]),
            "name":       a["album"],
            "artist":     name,
            "artistId":   _artist_id(name),
            "songCount":  a["count"],
            "coverArt":   sample.get("id", ""),
            "year":       _normalise_year(sample.get("year")),
            "genre":      (sample.get("genre") or [""])[0],
        })
    return _ok({
        "artist": {
            "id":         _artist_id(name),
            "name":       name,
            "albumCount": len(albums),
            "album":      sorted(albums, key=lambda a: a.get("year") or 0),
        },
    }, fmt=f)


@router.get("/getAlbum")
@router.get("/getAlbum.view")
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
    _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    aa, al = _decode_album_id(id, store)
    if not aa or not al:
        raise _SubsonicError(70, "Album not found.")
    tracks = store.filter_tracks(album_artist=aa, album=al)
    if not tracks:
        tracks = store.filter_tracks(artist=aa, album=al)
    tracks.sort(key=lambda x: (x.get("disc_number") or 0, x.get("track_number") or 0))
    songs = [_track_to_song(t_) for t_ in tracks]
    # Sum the *sanitised* per-song durations, not the raw tags: a track that
    # decoded to a non-finite duration already degraded to 0 in _track_to_song,
    # and summing the raw value here would let one bad file's nan/inf propagate
    # into ``int(total_secs)`` (ValueError/OverflowError) and fail the WHOLE
    # album — the same "one bad file kills the listing" mode _track_to_song
    # guards against.  song["duration"] is a finite int, so no int() needed.
    total_secs = sum(s.get("duration") or 0 for s in songs)
    sample = tracks[0] if tracks else {}
    return _ok({
        "album": {
            "id":        id,
            "name":      al,
            "artist":    aa,
            "artistId":  _artist_id(aa),
            "songCount": len(songs),
            "duration":  total_secs,
            "coverArt":  sample.get("id", ""),
            "year":      _normalise_year(sample.get("year")),
            "genre":     (sample.get("genre") or [""])[0],
            "song":      songs,
        },
    }, fmt=f)


@router.get("/getSong")
@router.get("/getSong.view")
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
    _require_user(request, sb_session, u, p, s, t)
    track = get_store().get_track(id)
    if not track:
        raise _SubsonicError(70, "Song not found.")
    return _ok({"song": _track_to_song(track)}, fmt=f)


@router.get("/getAlbumList")
@router.get("/getAlbumList.view")
@router.get("/getAlbumList2")
@router.get("/getAlbumList2.view")
@_wrap
async def get_album_list(
    request: Request,
    type: str = Query("newest", description="newest|recent|frequent|highest|alphabeticalByName|alphabeticalByArtist|random|byYear|byGenre|starred"),
    size: int = Query(10, ge=1, le=500),
    offset: int = Query(0, ge=0),
    fromYear: int | None = Query(default=None),
    toYear:   int | None = Query(default=None),
    genre:    str | None = Query(default=None),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    # The (album_artist, album) → sample map is memoised on _mutation_seq (5s
    # debounce during a scan); the per-TYPE SORT on top of it is now memoised too
    # (_sorted_album_items), so the common unfiltered carousel poll is an O(size)
    # slice instead of an O(A) materialise + O(A log A) sort on the event loop.

    def _apply_filters(items: list) -> list:
        out = items
        if genre:
            out = [a for a in out
                   if genre.lower() in [g.lower() for g in (a[1].get("genre") or [])]]
        if fromYear is not None or toYear is not None:
            lo = fromYear or -9999
            hi = toYear or 9999
            out = [a for a in out
                   if lo <= _normalise_year(a[1].get("year")) <= hi]
        return out

    has_filter = bool(genre) or fromYear is not None or toYear is not None

    if type == "random":
        # Random can't reuse a cached order — materialise + shuffle per request.
        import random
        albums = _apply_filters(list(_refresh_album_list_cache(store).items()))
        random.shuffle(albums)
        sliced = albums[offset: offset + size]
    else:
        # ``recent`` / ``frequent`` / ``highest`` / ``starred`` / ``byGenre`` are
        # advisory — they ship the unsorted seen.items() order, exactly as before.
        base = type if type in ("newest", "alphabeticalByName",
                                 "alphabeticalByArtist", "byYear") else "__unsorted__"
        sorted_items = _sorted_album_items(store, base)        # cached, pre-sorted
        if has_filter:
            albums = _apply_filters(sorted_items)              # comprehension → new list; cache untouched
            # byYear descending only ever applies here (it needs both years set).
            if base == "byYear" and fromYear is not None and toYear is not None and toYear < fromYear:
                albums.reverse()
            sliced = albums[offset: offset + size]
        else:
            sliced = sorted_items[offset: offset + size]       # O(size); no materialise, no sort
    out = []
    # Per-album-artist counter is precomputed in the store — read it once
    # per row instead of calling aggregate_albums() (which sorts) twice per
    # row (load-test #1 P1-3).
    aa_counter = store._agg_albums_by_album_artist
    for (aa, al), sample in sliced:
        # The album sub-counters are keyed on ``album.lower()`` (see
        # store._index_track) but ``al`` here is the raw-case album name from
        # album_sample_index() — lowercase it to match, else mixed/upper-case
        # titles miss the counter and report songCount: 0.
        al_key = (al or "").lower()
        song_count = aa_counter.get((aa or "").lower(), {}).get(al_key, 0)
        if not song_count:
            # Fallback for tracks whose album_artist is empty — fall back
            # to the bare artist counter.
            song_count = store._agg_albums_by_artist.get((aa or "").lower(), {}).get(al_key, 0)
        out.append({
            "id":        _album_id(aa, al),
            "name":      al,
            "artist":    aa,
            "artistId":  _artist_id(aa),
            "songCount": song_count,
            "coverArt":  sample.get("id", ""),
            "year":      _normalise_year(sample.get("year")),
            "genre":     (sample.get("genre") or [""])[0],
            "created":   _iso(sample.get("added_at")),
        })

    is_v2 = request.url.path.endswith("getAlbumList2") or request.url.path.endswith("getAlbumList2.view")
    key = "albumList2" if is_v2 else "albumList"
    return _ok({key: {"album": out}}, fmt=f)


@router.get("/search3")
@router.get("/search3.view")
@router.get("/search2")
@router.get("/search2.view")
@_wrap
async def search3(
    request: Request,
    query: str = Query("", alias="query"),
    artistCount: int = Query(20, ge=0, le=500),
    albumCount:  int = Query(20, ge=0, le=500),
    songCount:   int = Query(20, ge=0, le=500),
    artistOffset: int = Query(0, ge=0),
    albumOffset:  int = Query(0, ge=0),
    songOffset:   int = Query(0, ge=0),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    store = get_store()
    q = (query or "").strip().lower()
    # Songs: leverage existing filter_tracks query path
    tracks = store.filter_tracks(query=q, limit=songCount + songOffset)[songOffset:]
    songs = [_track_to_song(t_) for t_ in tracks[:songCount]]
    # Artists / albums: do a substring match against aggregations
    artists_all = [r["album_artist"] for r in store.aggregate_album_artists()]
    if not artists_all:
        artists_all = [r["artist"] for r in store.aggregate_artists()]
    artists_match = [a for a in artists_all if q in a.lower()][artistOffset:artistOffset + artistCount]
    artists_out = [{
        "id":         _artist_id(a),
        "name":       a,
        "albumCount": len(store.aggregate_albums(album_artist=a)) or len(store.aggregate_albums(artist=a)),
    } for a in artists_match]
    # Album matches: scan the memoised (album_artist|artist, album) -> sample map
    # (O(albums)) instead of materialising + walking all 270k track metas, and read
    # each match's sample straight from it instead of a per-pair filter_tracks scan.
    # The map keys derive identically to the old walk (raw album_artist-or-artist /
    # album), so no normalization fallback is needed here.
    seen = _refresh_album_list_cache(store)
    matches = [(aa, al) for (aa, al) in seen.keys() if al and q in al.lower()]
    matches.sort(key=lambda x: x[1].lower())
    albums_out = []
    for aa, al in matches[albumOffset:albumOffset + albumCount]:
        sample = seen.get((aa, al), {})
        albums_out.append({
            "id":       _album_id(aa, al),
            "name":     al,
            "artist":   aa,
            "artistId": _artist_id(aa),
            "coverArt": sample.get("id", ""),
            "year":     _normalise_year(sample.get("year")),
        })

    is_v3 = "search3" in request.url.path
    key = "searchResult3" if is_v3 else "searchResult2"
    return _ok({
        key: {
            "artist": artists_out,
            "album":  albums_out,
            "song":   songs,
        },
    }, fmt=f)


# Cached snapshot of ``list(store._tracks.keys())`` for ``getRandomSongs``.
# Rebuilding the list each call is a 170K-entry copy + full O(N) shuffle —
# on a busy library, ``random.shuffle(all_track_metas())`` was the single
# largest CPU consumer for Subsonic clients on shuffle play.  We sample
# without materialising metadata until *after* the selection.
_RANDOM_KEYS_CACHE: dict = {"seq": None, "keys": []}


def _random_track_keys(store) -> list[str]:
    seq = store._mutation_seq
    if _RANDOM_KEYS_CACHE["seq"] != seq:
        _RANDOM_KEYS_CACHE["keys"] = list(store._tracks.keys())
        _RANDOM_KEYS_CACHE["seq"] = seq
    return _RANDOM_KEYS_CACHE["keys"]


@router.get("/getRandomSongs")
@router.get("/getRandomSongs.view")
@_wrap
async def get_random_songs(
    request: Request,
    size: int = Query(10, ge=1, le=500),
    fromYear: int | None = Query(default=None),
    toYear:   int | None = Query(default=None),
    genre:    str | None = Query(default=None),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    import random
    store = get_store()
    # Filtered path — resolve eligibility via the maintained _tag_genre ∩
    # _sorted_year indexes (filter_track_ids), then random.sample over just the
    # candidate IDs and materialise only the picks.  Was a full all_track_metas()
    # walk + filter over 270k on the event loop.
    if genre or fromYear is not None or toYear is not None:
        ids = store.filter_track_ids(genre=genre, year_min=fromYear, year_max=toYear)
        if len(ids) > size:
            pick_ids = random.sample(ids, size)
        else:
            random.shuffle(ids)
            pick_ids = ids
        picks = [m for m in (store.get_track(i) for i in pick_ids) if m]
        songs = [_track_to_song(t_) for t_ in picks]
        return _ok({"randomSongs": {"song": songs}}, fmt=f)

    # Unfiltered: random.sample over the cached key list, materialising
    # metadata only for the chosen IDs (~10 dict copies vs 170K).
    keys = _random_track_keys(store)
    if not keys:
        return _ok({"randomSongs": {"song": []}}, fmt=f)
    take = min(size, len(keys))
    picked_ids = random.sample(keys, take)
    metas = store.get_tracks_batch(picked_ids)
    songs = [_track_to_song(t_) for t_ in metas if t_]
    return _ok({"randomSongs": {"song": songs}}, fmt=f)


# ── Streaming proxy ──────────────────────────────────────────────────────────

@router.get("/stream")
@router.get("/stream.view")
@router.get("/download")
@router.get("/download.view")
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
    the internal stream handler directly to reuse its range / transcode /
    rendered-format logic.  No HTTP redirect, no credential round-trip,
    no extension-inference dance.
    """
    _require_user(request, sb_session, u, p, s, t)

    # Late import to avoid an import cycle (stream.py doesn't import
    # subsonic.py, so a top-of-module import here would be safe — the
    # function-local form just future-proofs against accidental cycles
    # if a route ever moves between modules).
    from soniqboom.api.stream import stream_track

    return await stream_track(
        track_id=id,
        request=request,
        seek=0.0,
        subsong=0,
        file_path=None,
        target_format=(format or None),
        max_bitrate_kbps=int(maxBitRate or 0),
        target_sample_rate=int(sampleRate or 0),
        force_transcode=False,
        sb_session=sb_session,
        u=u, p=p, s=s, t=t,
    )


# ── OpenSubsonic Transcoding extension ──────────────────────────────────────
# Spec: https://opensubsonic.netlify.app/docs/extensions/transcoding/
#
# Clients POST their capability profile (codecs they decode natively, max
# bitrate, max sample rate) to ``getTranscodeDecision``.  We return either:
#
#   { "transcoded": false, "streamUrl": "/rest/stream?id=..." }
#         direct play — client can decode the source as-is
#
#   { "transcoded": true,  "token": "<jwt>", "format": "flac" / "mp3" / ... }
#         transcode required — client streams via /rest/getTranscodeStream?token=
#
# The JWT carries (track_id, codec, bitrate_kbps, file_mtime, exp).  When the
# file is touched on disk (rescan, metadata edit), its mtime changes and the
# JWT becomes stale — we return 410 Gone so the client requests a fresh
# decision instead of streaming bytes that don't match the JWT's metadata.
#
# We sign with HMAC-SHA256 keyed off the server secret.  Tokens are valid
# for 24 h by default — long enough that clients can cache the decision
# across a session, short enough that a stolen token has bounded lifetime.

_TOKEN_TTL_SECONDS = 24 * 60 * 60


def _server_secret() -> bytes:
    """Server-local secret for signing transcode tokens.  Reuses the same
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
    sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
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


# ── Transcode-token replay protection ────────────────────────────────────────
# A signed transcode token is bearer credential for re-using a transcode
# decision for ``_TOKEN_TTL_SECONDS``.  If a token leaks (proxy log, copied
# URL, network capture), the holder can replay it from any IP until the
# 24h TTL elapses.  Protect against bulk replay by keeping a per-user
# bounded set of jti's seen on ``getTranscodeStream`` — if the same jti is
# presented from two different clients in quick succession the second one
# is rejected.  This is a soft guard: legitimate clients reuse a token,
# but they reuse it from the SAME session, so the first call from the
# legitimate client wins and an attacker replay from a different session
# context hits the soft barrier.
_JTI_LRU_PER_USER: dict[str, dict[str, float]] = {}
_JTI_LRU_MAX_PER_USER = 1024


def _jti_seen(user_id: str, jti: str, remote_ip: str | None = None) -> bool:
    """Record a token's jti for the user; return True ONLY if the same
    jti has previously been seen from a DIFFERENT remote IP within the
    LRU window.

    Why per-IP and not per-call: Subsonic clients reuse the same stream
    URL across HTTP Range requests, pause/resume cycles, and seek-driven
    re-requests.  Treating the second Range request as a replay broke
    every Subsonic client (R2 finding) — they all looked like replays
    to the original per-call gate.  Per-IP keeps the replay threat
    model (token leaked to an attacker on a different network) without
    blocking the legitimate "same client re-uses URL" case.
    """
    if not user_id or not jti:
        return False
    now = time.time()
    bucket = _JTI_LRU_PER_USER.setdefault(user_id, {})
    # Drop entries past the token TTL so the dict stays bounded for users
    # streaming for many hours.
    if len(bucket) > _JTI_LRU_MAX_PER_USER:
        cutoff = now - _TOKEN_TTL_SECONDS
        stale = [k for k, v in bucket.items() if v[1] < cutoff]
        for k in stale:
            bucket.pop(k, None)
        if len(bucket) > _JTI_LRU_MAX_PER_USER:
            oldest = sorted(bucket.items(), key=lambda kv: kv[1][1])[: len(bucket) // 2]
            for k, _ in oldest:
                bucket.pop(k, None)
    entry = bucket.get(jti)
    if entry is None:
        # First time we've seen this jti for this user.  Record + allow.
        bucket[jti] = (remote_ip or "", now)
        return False
    seen_ip, _ts = entry
    # Same caller re-using the URL: refresh timestamp, allow.
    if not seen_ip or seen_ip == (remote_ip or ""):
        bucket[jti] = (seen_ip or (remote_ip or ""), now)
        return False
    # Different IP saw the same token — that's the replay we care about.
    return True


# Format → quality tier mapping for the decision heuristic.  Codec names
# here MUST be post-``_normalise_codec`` canonical forms (it collapses
# vorbis → ogg, m4a → aac before lookup).
_DECODER_QUALITY = {
    "flac": 100, "alac": 100, "wav": 100, "ape": 100, "wv": 100,
    "opus": 70, "ogg": 60, "aac": 50, "mp3": 40,
}

# Source codecs we never re-encode if the client says it can decode them.
# Lossless → lossless transcode (FLAC ↔ ALAC) is wasted CPU.
_LOSSLESS_FAMILY = {"flac", "alac", "ape", "wv", "wav"}


def _normalise_codec(name: str) -> str:
    """Map mutagen / ffprobe / user-input codec names to a canonical set
    so a client claiming ``opus`` and a server tagging ``Opus`` agree."""
    n = (name or "").lower().strip()
    return {"vorbis": "ogg", "ogg vorbis": "ogg", "m4a": "aac",
            "alac/aac": "alac", "alac": "alac", "wavpack": "wv",
            "musepack": "mpc"}.get(n, n)


@router.get("/getTranscodeDecision")
@router.get("/getTranscodeDecision.view")
@_wrap
async def get_transcode_decision(
    request: Request,
    id: str = Query(..., description="Track ID to negotiate"),
    clientCodecs: str = Query(
        "mp3,aac,ogg,flac,opus",
        max_length=512,
        description="Comma-separated codecs the client can decode natively",
    ),
    maxBitRate: int = Query(
        default=0, ge=0, le=2_500_000,
        description="Client's max-bitrate cap in kbps; 0 means no limit",
    ),
    maxSampleRate: int = Query(
        default=0, ge=0, le=384_000,
        description="Client's max sample rate in Hz; 0 means no limit",
    ),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
):
    """Negotiate the optimal transcode for ``id`` against the client's profile.

    Cheap, idempotent, cacheable on the client.  The signed token returned
    here is the only thing ``getTranscodeStream`` accepts, so a client
    that's offline can replay an unexpired token and the server will still
    honour it as long as the file's mtime hasn't shifted.
    """
    _require_user(request, sb_session, u, p, s, t)

    from pathlib import Path as _P
    from soniqboom.core.data import get_track as _gt
    track = await _gt(id)
    if track is None:
        return _err(70, "Track not found")

    # Map the client's capability profile.
    accept = {_normalise_codec(c) for c in clientCodecs.split(",") if c.strip()}
    source_codec = _normalise_codec(track.format)
    transcode_fmt = settings.transcode_format  # e.g. "flac"

    # Decide.
    decision: dict[str, Any] = {"track_id": id}
    if source_codec in accept and source_codec in _LOSSLESS_FAMILY:
        # Client speaks our lossless source → direct play, no transcode.
        decision["transcoded"] = False
        decision["streamUrl"] = f"/rest/stream?id={id}"
        decision["sourceCodec"] = source_codec
    elif source_codec in accept and source_codec not in _LOSSLESS_FAMILY:
        # Native lossy that the client accepts (mp3/aac/ogg/opus).  Direct
        # play unless the client capped maxBitRate below what the file
        # actually has — then we transcode down to that cap.
        src_br = (getattr(track, "bitrate", 0) or 0) // 1000
        if maxBitRate and src_br and src_br > maxBitRate:
            decision["transcoded"] = True
            decision["targetCodec"] = source_codec
            decision["targetBitRate"] = maxBitRate
        else:
            decision["transcoded"] = False
            decision["streamUrl"] = f"/rest/stream?id={id}"
            decision["sourceCodec"] = source_codec
    else:
        # Client can't decode source → transcode to whichever target we
        # know the client accepts, preferring lossless then high-quality
        # lossy.  Falls back to server-configured ``transcode_format``.
        ordered = sorted(accept, key=lambda c: _DECODER_QUALITY.get(c, 0), reverse=True)
        target = next((c for c in ordered if c in _DECODER_QUALITY), transcode_fmt)
        decision["transcoded"] = True
        decision["targetCodec"] = target
        if maxBitRate:
            decision["targetBitRate"] = maxBitRate
        if maxSampleRate:
            decision["targetSampleRate"] = maxSampleRate

    # Direct-play decisions don't need a token — clients hit /rest/stream
    # with the existing auth.  Only mint when the client genuinely needs to
    # call /rest/getTranscodeStream (saves an HMAC computation per request
    # and removes the "stray token in a direct-play response" footgun where
    # a buggy client switches to the wrong endpoint).
    if decision.get("transcoded"):
        mtime: float = 0.0
        size: int = 0
        if not track.path.startswith(("smb://", "ftp://", "http://", "https://")):
            try:
                st = await asyncio.to_thread(_P(track.path).stat)
                mtime = float(st.st_mtime)
                size  = int(st.st_size)
            except OSError:
                pass
        # Float mtime + size together close the "rename-then-replace at
        # identical mtime" silent-stale window — a future contributor will
        # struggle to forge both keys.
        # ``jti`` is a per-issue random nonce — paired with the verify-time
        # LRU set below, it lets us detect token replay across clients.
        import secrets as _secrets
        claims = {
            "tid":  id,
            "tc":   decision.get("targetCodec") or source_codec,
            "tbr":  decision.get("targetBitRate") or 0,
            "tsr":  decision.get("targetSampleRate") or 0,
            "mt":   f"{mtime:.6f}",
            "sz":   size,
            "iat":  int(time.time()),
            "exp":  int(time.time()) + _TOKEN_TTL_SECONDS,
            "jti":  _secrets.token_urlsafe(12),
        }
        decision["token"]     = _sign_token(claims)
        decision["expiresIn"] = _TOKEN_TTL_SECONDS
    return _ok({"transcodeDecision": decision})


@router.get("/getTranscodeStream")
@router.get("/getTranscodeStream.view")
@_wrap
async def get_transcode_stream(
    request: Request,
    token: str = Query(..., description="Token from getTranscodeDecision"),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
):
    """Stream the track described by a previously-issued transcode token.

    Auth still applies — the token is a transcode *decision*, not a
    bypass of Subsonic auth.  410 Gone when the file's mtime has moved
    since the token was issued; the client should re-call getTranscodeDecision.
    """
    user = _require_user(request, sb_session, u, p, s, t)
    claims = _verify_token(token)
    if claims is None:
        # Plain-English message — clients like DSub/Substreamer surface
        # server messages verbatim to end users.
        return _err(70, "This stream link expired. Tap play again.")

    # Replay protection: a given ``jti`` is honoured per-IP.  The same
    # client re-using the URL (Range requests, pause/resume, browser
    # refresh) keeps working; a token replayed from a DIFFERENT IP is
    # rejected.  Earlier per-call rejection broke every Subsonic client
    # (DSub/Symfonium/Substreamer all reuse the stream URL).
    jti = str(claims.get("jti") or "")
    remote_ip = request.client.host if request.client else None
    if jti and _jti_seen(user.id, jti, remote_ip):
        return _err(
            70,
            "This stream link was already used from another device.",
        )

    from pathlib import Path as _P
    from soniqboom.core.data import get_track as _gt
    track = await _gt(claims["tid"])
    if track is None:
        return _err(70, "Track not found")

    # Freshness check — file changed under us, force the client to renegotiate.
    # Compare both mtime (float, sub-second precision) AND size — defeats the
    # "rename-and-replace preserves mtime" silent-stale window.
    if claims.get("mt") and not track.path.startswith(
        ("smb://", "ftp://", "http://", "https://"),
    ):
        try:
            st = await asyncio.to_thread(_P(track.path).stat)
            claimed_mt = float(claims["mt"])
            claimed_sz = int(claims.get("sz") or 0)
            if (abs(float(st.st_mtime) - claimed_mt) > 0.001 or
                    (claimed_sz and int(st.st_size) != claimed_sz)):
                return _err(30, "This song changed on the server. Tap play again to start over.")
        except OSError:
            # Stat failure mid-stream — let the downstream /api/stream
            # surface the real error (404, permission denied).
            pass

    # Serve inline via the internal stream handler — same architectural
    # reason as the ``/rest/stream.view`` route above (iOS AVPlayer
    # occasionally drops query params on 307s and infers content-type
    # from URL extensions).  We thread the negotiated transcode params
    # (codec / bitrate / sample-rate) from the token claims into the
    # call, so the stream endpoint serves bytes matching the decision
    # the client already cached.
    from soniqboom.api.stream import stream_track
    return await stream_track(
        track_id=claims["tid"],
        request=request,
        seek=0.0,
        subsong=0,
        file_path=None,
        target_format=(str(claims["tc"]) if claims.get("tc") else None),
        max_bitrate_kbps=int(claims.get("tbr") or 0),
        target_sample_rate=int(claims.get("tsr") or 0),
        force_transcode=False,
        sb_session=sb_session,
        u=u, p=p, s=s, t=t,
    )


@router.get("/getCoverArt")
@router.get("/getCoverArt.view")
@_wrap
async def get_cover_art(
    request: Request,
    id: str = Query(...),
    size: int | None = Query(default=None),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
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
    """
    _require_user(request, sb_session, u, p, s, t)
    # Map Subsonic "size in px" → SoniqBoom buckets: <=300 → sm, <=600 → lg.
    bucket = "sm"
    if size and size > 300:
        bucket = "lg"
    if size and size > 600:
        bucket = "full"
    # Album / artist IDs are synthetic — resolve them to a sample track,
    # then fetch that track's art.
    resolved_id = id
    if id.startswith("al:") or id.startswith("ar:"):
        store = get_store()
        sample_id = None
        if id.startswith("al:"):
            aa, al = _decode_album_id(id, store)
            if aa and al:
                ts = (store.filter_tracks(album_artist=aa, album=al, limit=1)
                      or store.filter_tracks(artist=aa, album=al, limit=1))
                if ts: sample_id = ts[0]["id"]
        else:
            name = _decode_artist_id(id, store)
            if name:
                ts = (store.filter_tracks(album_artist=name, limit=1)
                      or store.filter_tracks(artist=name, limit=1))
                if ts: sample_id = ts[0]["id"]
        if not sample_id:
            return Response(status_code=404)
        resolved_id = sample_id

    # Late import so subsonic.py stays loadable even if art.py has a
    # transient init error (we'd rather degrade art than crash routing).
    from soniqboom.api.art import (
        _resolve_full_art,
        _generate_and_cache_thumbs,
        _SIZE_MAP,
    )
    from soniqboom.core import art_cache

    if bucket in _SIZE_MAP:
        thumb = await art_cache.get_art(resolved_id, bucket)
        if thumb:
            return Response(content=thumb, media_type="image/jpeg")
        full_data, _mime = await _resolve_full_art(resolved_id)
        if full_data:
            thumbs = await _generate_and_cache_thumbs(resolved_id, full_data)
            return Response(content=thumbs[bucket], media_type="image/jpeg")
        return Response(status_code=404)

    full_data, mime = await _resolve_full_art(resolved_id)
    if full_data:
        asyncio.create_task(_generate_and_cache_thumbs(resolved_id, full_data))
        return Response(content=full_data, media_type=mime or "image/jpeg")
    return Response(status_code=404)


# ── Scrobble ────────────────────────────────────────────────────────────────

@router.get("/scrobble")
@router.get("/scrobble.view")
@_wrap
async def scrobble(
    request: Request,
    id: str = Query(...),
    submission: bool = Query(default=True),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    # Mutating endpoint — refuse cookie-only auth to prevent CSRF via
    # an attacker-origin <img src=…>.
    user = _require_user_no_cookie(request, sb_session, u, p, s, t)
    from soniqboom.core.store import get_store as _gs
    track = _gs().get_track(id)
    if submission:
        # Mirror the /api/tracks/{id}/played path — play-stat update + history
        # append — inline to avoid an HTTP round-trip back to the same process.
        if track:
            from soniqboom.core.data import record_play
            await record_play(id)
            # Forward to external scrobblers (last.fm / ListenBrainz) —
            # noop until the user enables them under Settings → My Account.
            try:
                from soniqboom.core.scrobble import submit_play
                await submit_play(user, track)
            except Exception:
                pass
    else:
        # submission=false is the "now playing" signal (OpenSubsonic clients
        # send it at track start).  Push it to the external now-playing widgets
        # if the user has linked last.fm / ListenBrainz; otherwise a cheap noop.
        if track:
            try:
                from soniqboom.core.scrobble import submit_now_playing
                await submit_now_playing(user, track)
            except Exception:
                pass
    return _ok(fmt=f)


# ── Playlists ───────────────────────────────────────────────────────────────

@router.get("/getPlaylists")
@router.get("/getPlaylists.view")
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
        from soniqboom.core.users import get_user_store
        owner_name = user.username if owner_id == user.id else (
            (get_user_store().get(owner_id).username if owner_id and get_user_store().get(owner_id) else "shared")
        )
        pls.append({
            "id":         pl["id"],
            "name":       pl["name"],
            "owner":      owner_name,
            "public":     owner_id is None,
            "songCount":  len(pl.get("track_ids") or []),
            "duration":   0,
            "created":    _iso(pl.get("created_at")),
            "changed":    _iso(pl.get("updated_at")),
        })
    pls.sort(key=lambda x: x.get("changed") or "", reverse=True)
    return _ok({"playlists": {"playlist": pls}}, fmt=f)


@router.get("/getPlaylist")
@router.get("/getPlaylist.view")
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
    if pl.get("query"):
        # Smart playlist — evaluate the saved search live (same engine as the
        # native API) so Subsonic clients see the computed tracks too.
        from soniqboom.api.search import run_search
        _results = await run_search(pl["query"], limit=500)
        entry_tracks = [r.model_dump() if hasattr(r, "model_dump") else r for r in _results]
    else:
        # Playlist entries are bare ids OR {id, subsong} objects — resolve to the
        # base track id (Subsonic clients play the default tune; subsong is a
        # SoniqBoom-native dimension the protocol has no field for).
        _ids = [(e.get("id") if isinstance(e, dict) else e)
                for e in (pl.get("track_ids") or [])]
        entry_tracks = store.get_tracks_batch(_ids)
    entries = [_track_to_song(t_) for t_ in entry_tracks if t_]
    return _ok({
        "playlist": {
            "id":        pl["id"],
            "name":      pl["name"],
            "owner":     user.username if owner_id == user.id else "shared",
            "public":    owner_id is None,
            "songCount": len(entries),
            "duration":  sum(e.get("duration") or 0 for e in entries),
            "created":   _iso(pl.get("created_at")),
            "changed":   _iso(pl.get("updated_at")),
            "entry":     entries,
        },
    }, fmt=f)


@router.get("/createPlaylist")
@router.get("/createPlaylist.view")
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
        await _update(playlistId, name=name, track_ids=songId)
        pl = store._playlists.get(playlistId)
    else:
        pl = await _create(name or "New playlist", track_ids=songId,
                           owner_user_id=user.id)
    if not pl:
        raise _SubsonicError(70, "Playlist not found.")
    return await get_playlist(
        request, id=pl["id"], sb_session=sb_session,
        u=u, p=p, s=s, t=t, f=f,
    )


@router.get("/updatePlaylist")
@router.get("/updatePlaylist.view")
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
    # Remove first, by descending index so earlier indices stay valid.
    for idx in sorted(set(songIndexToRemove), reverse=True):
        if 0 <= idx < len(ids):
            ids.pop(idx)
    ids.extend(songIdToAdd)
    from soniqboom.core.data import update_playlist as _update
    await _update(playlistId, name=name, track_ids=ids)
    return _ok(fmt=f)


@router.get("/deletePlaylist")
@router.get("/deletePlaylist.view")
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

@router.get("/getUser")
@router.get("/getUser.view")
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
    if target != me.username and me.role != "admin":
        raise _SubsonicError(50, "Not authorised to view this user.")
    target_user = get_user_store().get_by_username(target) or me
    return _ok({
        "user": {
            "username":          target_user.username,
            "email":             "",
            "scrobblingEnabled": bool(target_user.lastfm_session_key or target_user.listenbrainz_token),
            "adminRole":         target_user.role == "admin",
            "settingsRole":      target_user.role == "admin",
            "downloadRole":      target_user.role in ("admin", "edit"),
            "uploadRole":        target_user.role == "admin",
            "playlistRole":      target_user.role in ("admin", "edit", "readonly"),
            "coverArtRole":      target_user.role == "admin",
            "commentRole":       False,
            "podcastRole":       False,
            "streamRole":        True,
            "jukeboxRole":       target_user.role in ("admin", "edit"),
            "shareRole":         False,
        },
    }, fmt=f)


@router.get("/getStarred")
@router.get("/getStarred.view")
@router.get("/getStarred2")
@router.get("/getStarred2.view")
@_wrap
async def get_starred(
    request: Request,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    f: str = Query(default="xml"),
):
    _require_user(request, sb_session, u, p, s, t)
    # No per-user starred state yet — return empty payload so clients
    # don't error out on the call.
    is_v2 = "starred2" in request.url.path.lower()
    key = "starred2" if is_v2 else "starred"
    return _ok({key: {"artist": [], "album": [], "song": []}}, fmt=f)


@router.get("/getGenres")
@router.get("/getGenres.view")
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
    rows = get_store().aggregate_genres()
    return _ok({
        "genres": {
            "genre": [
                {"value": r["genre"], "songCount": r["count"], "albumCount": 0}
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


@router.get("/getLyrics")
@router.get("/getLyrics.view")
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
        # O(N) scan over the whole library — run it off the event loop so a
        # legacy client hitting this rare endpoint can't stall other requests
        # on a large collection.
        def _scan() -> str | None:
            for cand in get_store().all_tracks():
                if ti and (cand.get("title") or "").strip().lower() != ti:
                    continue
                if a and (cand.get("artist") or cand.get("album_artist") or "").strip().lower() != a:
                    continue
                return cand["id"]
            return None
        import asyncio as _asyncio
        tid = await _asyncio.to_thread(_scan)
    lyr = await _lyrics_for_track_id(tid) if tid else None
    if not lyr:
        # Spec: always return a <lyrics> element (empty body) so clients
        # don't treat "no lyrics" as a protocol error.
        return _ok({"lyrics": {"artist": artist, "title": title, "_text": ""}}, fmt=f)
    return _ok({"lyrics": {"artist": lyr["artist"], "title": lyr["title"],
                           "_text": lyr["text"]}}, fmt=f)


@router.get("/getLyricsBySongId")
@router.get("/getLyricsBySongId.view")
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


# ── Similar songs (OpenSubsonic sonicSimilarity extension) ────────────────────

async def _similar_song_tracks(track_id: str, count: int) -> list[dict]:
    """Seed the audio-envelope similarity engine and return the matched track
    dicts (same engine the web 'More like this' uses)."""
    import asyncio as _asyncio
    from soniqboom.core.data import get_all_ratings
    from soniqboom.core.similar import find_similar
    store = get_store()
    seed = store.get_track(track_id)
    if not seed:
        raise _SubsonicError(70, "Song not found.")
    ratings = await get_all_ratings()
    res = await _asyncio.to_thread(
        find_similar, seed, store.all_tracks(), store.waveforms_view(),
        ratings=ratings or {}, k=max(1, min(count, 100)),
    )
    return [r["track"] for r in res if r.get("track")]


@router.get("/getSimilarSongs")
@router.get("/getSimilarSongs.view")
@router.get("/getSimilarSongs2")
@router.get("/getSimilarSongs2.view")
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
    _require_user(request, sb_session, u, p, s, t)
    tracks = await _similar_song_tracks(id, count)
    songs = [_track_to_song(tr) for tr in tracks]
    is_v2 = "similarsongs2" in request.url.path.lower()
    key = "similarSongs2" if is_v2 else "similarSongs"
    return _ok({key: {"song": songs}}, fmt=f)


# ── Jukebox (Subsonic jukeboxControl) ─────────────────────────────────────────

@router.get("/jukeboxControl")
@router.get("/jukeboxControl.view")
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

    if act == "set":
        jb.set_queue(id or [])
    elif act == "add":
        jb.add(id or [])
    elif act == "clear":
        jb.clear()
    elif act == "remove":
        if idx is None:
            raise _SubsonicError(10, "jukeboxControl remove requires index.")
        jb.remove(idx)
    elif act == "shuffle":
        jb.shuffle()
    elif act == "skip":
        if idx is None:
            raise _SubsonicError(10, "jukeboxControl skip requires index.")
        jb.skip(idx, offs or 0.0)
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
    if act == "get":
        # Full playlist view — resolve queued ids to Child entries.
        store = get_store()
        entries: list[dict] = []
        for tid in jb.queue_ids():
            tr = store.get_track(tid)
            if tr:
                entries.append(_track_to_song(tr))
        return _ok({"jukeboxPlaylist": {
            "currentIndex": st["currentIndex"],
            "playing":      st["playing"],
            "gain":         st["gain"],
            "position":     st["position"],
            "entry":        entries,
        }}, fmt=f)
    # Every other action returns the compact status object.
    return _ok({"jukeboxStatus": st}, fmt=f)
