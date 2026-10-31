# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""UPnP-AV / DLNA HTTP endpoints — device description + ContentDirectory.

When SoniqBoom's :mod:`soniqboom.core.dlna_server` SSDP responder tells
a controller "I'm here, my device description is at ``/dlna/device.xml``",
the controller follows that URL and walks the resulting XML to learn
which services we expose.  This module serves all of those HTTP
endpoints:

  GET  /dlna/device.xml     - root device description (UPnP DDD)
  GET  /dlna/cds.xml        - ContentDirectory:1 service description
  GET  /dlna/cm.xml         - ConnectionManager:1 service description
  POST /dlna/cds/control    - ContentDirectory:1 SOAP control (Browse + Search)
  POST /dlna/cm/control     - ConnectionManager:1 SOAP control (minimal)
  SUB  /dlna/cds/event      - GENA eventing (SUBSCRIBE/UNSUBSCRIBE; NOTIFY on
                              library change, carrying SystemUpdateID)
  GET  /dlna/art/<id>.jpg   - a track's cover for control points (550 px JPEG)

The router mounts at the app root (NOT under /api/) so the URLs
exactly match what the SSDP LOCATION header advertises, and so the
session-cookie auth middleware leaves them anonymous.  DLNA traffic
is intrinsically LAN-only and has no concept of authenticated sessions
— controllers expect the device description to be reachable without
credentials.

Browse hierarchy:

    0                                   (root container)
    └── music                           (audio container)
        ├── all                         (every track in flat list)
        ├── albums                      (one container per album)
        │   └── al:<b64(album)>         (tracks of one album)
        ├── artists                     (one container per album_artist)
        │   └── ar:<b64(album_artist)>  (that artist's albums)
        │       └── aa:<b64(artist)>:<b64(album)>  (tracks of one album)
        ├── genres                      (one container per genre)
        │   └── ge:<b64(genre)>         (tracks of one genre)
        ├── folders                     (one container per library folder)
        │   └── fo:<root hash>[:<b64(rel path)>]  (subfolders, then tracks)
        └── playlists                   (the playlists shared with every account)
            └── pl:<id>                 (its entries: items pi:<id>:<index>)

    ObjectID name tokens are URL-safe base64 of the album / artist /
    genre NAME or folder path (reversible, so no server-side hash→name
    map is needed).

Each track returns a DIDL-Lite ``<item>`` whose ``<res>`` URLs point
at the existing ``/cast/{token}/<filename>.<ext>`` anonymous byte
server — same tokens the Cast / DLNA / AirPlay sender uses for its
outgoing direction.  Browse-time URLs use the user-less anonymous
token form (no ``uid`` claim, ``origin="dlna"``) and a shorter
15-minute TTL so the exposure window is bounded.  Covers come from
``/dlna/art/<track id>.jpg`` (``upnp:albumArtURI``).
"""
from __future__ import annotations

import asyncio
import html
import logging
import urllib.parse
from typing import Any
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, Response as FResponse

log = logging.getLogger(__name__)


def _dlna_store():
    """The library as DLNA control points see it: catalogue tunes
    (``core/catalogue.py``) are hidden unless the catalogue's "Show to
    Subsonic apps" switch is on (``core/catalogue_view.py``)."""
    from soniqboom.core.catalogue_view import subsonic_store
    from soniqboom.core.store import get_store
    return subsonic_store(get_store())


# Mounted at app root in main.py
router = APIRouter(tags=["dlna"])


# ── Cached state ──────────────────────────────────────────────────────────
# The SSDP layer (dlna_server) constructs the URLs from base_url +
# friendly_name + UDN.  We fetch that singleton on each request rather
# than caching a snapshot — the SSDP loop can be restarted with a new
# binding (e.g. after the LAN IP changes) and HTTP responses should
# reflect the current values immediately.

def _instance():
    from soniqboom.core import dlna_server
    inst = dlna_server.get_instance()
    if inst is None:
        raise HTTPException(503, "DLNA Media Server is not running.")
    return inst


# ── XML helpers ───────────────────────────────────────────────────────────

def _xml_response(xml: str, *, status: int = 200) -> FResponse:
    """Standard UPnP XML response.  Content-Type matters — controllers
    bail with cryptic errors when it's ``text/plain`` or has the wrong
    charset."""
    return FResponse(
        content=xml,
        status_code=status,
        media_type="text/xml; charset=\"utf-8\"",
        headers={
            "Server":          "UPnP/1.0 SoniqBoom-DLNA/1.0",
            "Content-Language": "en",
        },
    )


def _xescape(s: str) -> str:
    """XML-escape for element text + attribute values."""
    return html.escape(s or "", quote=True)


# ── /dlna/device.xml ──────────────────────────────────────────────────────

@router.get("/dlna/device.xml", include_in_schema=False)
async def device_description():
    """Root device description — the entry point every DLNA controller
    follows from the SSDP LOCATION URL.  Lists the friendly name +
    manufacturer info + the two services we host (ContentDirectory,
    ConnectionManager) with their control / event URLs.
    """
    inst = _instance()
    # No external deps — hand-built so we control exact byte layout
    # (some controllers parse this with strict regex matchers).
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{_xescape(inst.friendly_name)}</friendlyName>
    <manufacturer>SoniqBoom</manufacturer>
    <manufacturerURL>https://github.com/scyris/soniqboom</manufacturerURL>
    <modelDescription>SoniqBoom Music Library</modelDescription>
    <modelName>SoniqBoom</modelName>
    <modelNumber>1.0</modelNumber>
    <modelURL>https://github.com/scyris/soniqboom</modelURL>
    <serialNumber>1</serialNumber>
    <UDN>{_xescape(inst.udn)}</UDN>
    <dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">DMS-1.50</dlna:X_DLNADOC>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/dlna/cds.xml</SCPDURL>
        <controlURL>/dlna/cds/control</controlURL>
        <eventSubURL>/dlna/cds/event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/dlna/cm.xml</SCPDURL>
        <controlURL>/dlna/cm/control</controlURL>
        <eventSubURL>/dlna/cm/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
"""
    return _xml_response(xml)


# ── /dlna/cds.xml (service description) ────────────────────────────────────

@router.get("/dlna/cds.xml", include_in_schema=False)
async def cds_description():
    """ContentDirectory:1 service description.

    Declares the action set we support.  We implement only the two
    actions every real-world controller actually calls — Browse and
    GetSearchCapabilities (the latter returns empty, meaning search
    isn't supported).  Skipping Search keeps the implementation
    bounded; controllers fall back to Browse + client-side filter.
    """
    xml = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetSearchCapabilities</name>
      <argumentList>
        <argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetSortCapabilities</name>
      <argumentList>
        <argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetSystemUpdateID</name>
      <argumentList>
        <argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>Browse</name>
      <argumentList>
        <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>Search</name>
      <argumentList>
        <argument><name>ContainerID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>SearchCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SearchCriteria</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType><allowedValueList><allowedValue>BrowseMetadata</allowedValue><allowedValue>BrowseDirectChildren</allowedValue></allowedValueList></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SearchCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>ContainerUpdateIDs</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""
    return _xml_response(xml)


# ── /dlna/cm.xml (ConnectionManager:1) ─────────────────────────────────────

@router.get("/dlna/cm.xml", include_in_schema=False)
async def connmgr_description():
    """Minimal ConnectionManager:1.  We don't implement
    PrepareForConnection — controllers that don't try fail soft;
    those that do (some legacy DLNA gear) get a 401-shaped SOAP fault."""
    xml = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name>
      <argumentList>
        <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
        <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action><name>GetCurrentConnectionIDs</name>
      <argumentList>
        <argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""
    return _xml_response(xml)


# ── SOAP envelope helpers ──────────────────────────────────────────────────

_SOAP_NS    = "http://schemas.xmlsoap.org/soap/envelope/"
_UPNP_CDS_NS = "urn:schemas-upnp-org:service:ContentDirectory:1"
_UPNP_CM_NS  = "urn:schemas-upnp-org:service:ConnectionManager:1"


def _parse_soap_body(raw: bytes) -> tuple[str, dict[str, str]]:
    """Return (action_name, {arg: value}) for an incoming SOAP request.

    Defensive: returns ('', {}) on any parse failure so the caller
    returns a SOAP fault rather than 500.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return "", {}
    # Find <Envelope><Body><{action}>
    body = None
    for child in root:
        if child.tag.endswith("}Body") or child.tag == "Body":
            body = child
            break
    if body is None or len(body) == 0:
        return "", {}
    action_elem = body[0]
    # Tag form: {namespace}Action — strip the namespace
    action = action_elem.tag.split("}", 1)[-1] if "}" in action_elem.tag else action_elem.tag
    args: dict[str, str] = {}
    for arg in action_elem:
        name = arg.tag.split("}", 1)[-1] if "}" in arg.tag else arg.tag
        args[name] = arg.text or ""
    return action, args


def _soap_response(service_ns: str, action: str, args: dict[str, str]) -> FResponse:
    """Wrap action result args in a SOAP envelope + correct headers."""
    args_xml = "".join(
        f"<{name}>{_xescape(value)}</{name}>" for name, value in args.items()
    )
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        f'<u:{action}Response xmlns:u="{service_ns}">'
        f'{args_xml}'
        f'</u:{action}Response>'
        '</s:Body>'
        '</s:Envelope>'
    )
    return FResponse(
        content=body,
        media_type='text/xml; charset="utf-8"',
        headers={
            "Server":           "UPnP/1.0 SoniqBoom-DLNA/1.0",
            "EXT":              "",
            "Content-Language": "en",
        },
    )


def _soap_fault(code: int, desc: str, status: int = 500) -> FResponse:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        '<s:Fault>'
        '<faultcode>s:Client</faultcode>'
        '<faultstring>UPnPError</faultstring>'
        '<detail>'
        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f'<errorCode>{code}</errorCode>'
        f'<errorDescription>{_xescape(desc)}</errorDescription>'
        '</UPnPError>'
        '</detail>'
        '</s:Fault>'
        '</s:Body>'
        '</s:Envelope>'
    )
    return FResponse(
        content=body, status_code=status,
        media_type='text/xml; charset="utf-8"',
    )


# ── DIDL-Lite construction ─────────────────────────────────────────────────

_DIDL_NS_BOILERPLATE = (
    'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
    'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/"'
)

# A file the stream endpoint serves as it is (``stream.NATIVE``: MP3, FLAC,
# WAV, Ogg Vorbis, Opus) is offered as it is.  Anything else — every rendered
# format (SID, MIDI, trackers, AdLib, Atari ST, PSF, console and Amiga
# formats), MP4/ALAC and the rarer containers — is offered as these, in this
# order: lossless first, MP3 for the renderers without FLAC.  A control point
# plays the first its renderer takes.
_TRANSCODE_RES = (("flac", None), ("mp3", 320))

# Browse / Search links live a day: a control point queues a whole album or
# playlist with the links it browsed, and plays and seeks in them for hours.
# (Anyone on the network may browse for fresh ones — the Media Server is
# anonymous — so a short life protects nothing.)
_LINK_TTL_S = 24 * 3600


def _native_ext(track: dict) -> str:
    """The file's extension (an archive member's own) when the stream
    endpoint serves it as it is, else ``""``."""
    from soniqboom.api.stream import NATIVE
    name = (track.get("path") or "").rpartition("::")[2]
    ext = ("." + name.rpartition(".")[2].lower()) if "." in name else ""
    return ext if ext in NATIVE else ""


def _features(codec: str, *, seekable: bool) -> str:
    """The protocolInfo's DLNA field for a link in ``codec`` — the
    ``contentFeatures`` its answer carries.  WAV here is the RIFF file, not
    DLNA's LPCM profile (raw ``audio/L16``), so it names no profile."""
    from soniqboom.core.cast_codecs import content_features
    f = content_features(codec, seekable=seekable)
    if codec == "wav":
        f = ";".join(p for p in f.split(";") if not p.startswith("DLNA.ORG_PN="))
    return f or "*"


def _res(track: dict, base_url: str, *, codec: "str | None", ext: str, mime: str,
         bitrate: "int | None", subsong: "int | None", dur_attr: str) -> str:
    """One ``<res>``: a fresh anonymous link to the cast byte server for the
    track — in ``codec`` (None: the file as it is, ``ext``)."""
    from soniqboom.core import cast_tokens
    token = cast_tokens.issue_token(
        track_id=track.get("id") or "",
        codec=codec,
        bitrate_kbps=bitrate,
        subsong=subsong,
        ttl_seconds=_LINK_TTL_S,
        origin="dlna",
    )
    filename = cast_tokens.safe_filename(track, ext)
    # URL-encode the filename so spaces and any other URL-unsafe chars
    # become %20 etc.  ``safe_filename`` already stripped CR/LF and
    # non-ASCII; this layer covers spaces + parens + the handful of
    # ASCII chars that are technically URL-unsafe.  Without this, strict
    # DLNA renderers (LG WebOS, older Samsung) reject the URL outright
    # because they treat the space as an HTTP header delimiter.
    safe_path = urllib.parse.quote(filename, safe="._-()")
    url = f"{base_url.rstrip('/')}/cast/{token}/{safe_path}"
    # A transcode's first play is encoded while it runs: no length, no seeking.
    features = _features(codec or ext.lstrip("."), seekable=codec is None)
    return (f'<res protocolInfo="http-get:*:{mime}:{features}"{dur_attr}>'
            f'{_xescape(url)}</res>')


def _art_uri(track: dict, base_url: str) -> str:
    """``upnp:albumArtURI`` of a track that has a cover we know of — an
    embedded one the scan found, or one already cached (its own, or its
    folder's picture) — else empty: a listing never offers a link that
    would answer 404, and never makes the server fetch an online-collection
    tune's cover."""
    from soniqboom.core import art_cache, catalogue
    from soniqboom.core.store import get_store
    tid = track.get("id") or ""
    if not tid or get_store().is_art_absent(tid):
        return ""
    known = bool(track.get("cover_art"))
    if not known and not catalogue.is_catalogue_path(track.get("path") or ""):
        from soniqboom.api.art import _folder_art_cache_key
        dh = track.get("dir_hash")
        known = (art_cache.art_exists(tid, "lg") or art_cache.art_exists(tid, "full")
                 or bool(dh and art_cache.art_exists(_folder_art_cache_key(dh), "full")))
    if not known:
        return ""
    url = f"{base_url.rstrip('/')}/dlna/art/{urllib.parse.quote(tid, safe='')}.jpg"
    return f'<upnp:albumArtURI dlna:profileID="JPEG_MED">{_xescape(url)}</upnp:albumArtURI>'


def _track_to_didl_item(track: dict, base_url: str, parent_id: str = "music",
                        *, item_id: "str | None" = None,
                        subsong: "int | None" = None) -> str:
    """Render a single track row as a DIDL-Lite ``<item>``.

    Each ``<res>`` URL points at the existing anonymous cast byte server
    with a fresh signed token.  Tokens are user-less (no ``uid`` claim)
    so DLNA controllers — which never authenticate to us — get exactly
    the access scope the operator agreed to when they enabled the
    Media Server.  A file the stream endpoint serves as it is is offered
    as it is; any other as FLAC and MP3 (``_TRANSCODE_RES``), which the
    stream endpoint encodes while a render still runs, so it starts at once.

    ``parent_id`` is the ObjectID of the container this item is being
    returned under (``music/all``, ``al:<b64>``, ``aa:<b64>:<b64>`` …).
    ContentDirectory:1 §2.7.1 requires an item's ``parentID`` to match
    the container that lists it; strict controllers use it to anchor the
    item back into the tree, so a constant ``"music"`` breaks descent.
    ``item_id`` (default: the track id) and ``subsong`` (the tune, a wire
    index; None: the file's default) are a playlist entry's — titled and
    timed as that tune, numbered as the web player numbers it.
    """
    from soniqboom.api.stream import NATIVE
    from soniqboom.core import subsonic_index as _sx
    from soniqboom.core.cast_codecs import CODECS

    title    = track.get("title")  or "Unknown Track"
    artist   = (track.get("album_artist") or track.get("artist") or "Unknown Artist")
    album    = track.get("album")  or "Unknown Album"
    duration = float(track.get("duration") or 0)
    if subsong is not None:
        title = f"{title} (tune {_sx.wire_tune(track, subsong) + 1})"
        duration = _sx.wire_length(track, subsong)

    # Duration: H:MM:SS.fff per DLNA spec; left out when it isn't known.
    dur_attr = ""
    if duration > 0:
        h  = int(duration // 3600)
        m  = int((duration % 3600) // 60)
        s  = duration - (h * 3600 + m * 60)
        dur_attr = f' duration="{h:d}:{m:02d}:{s:06.3f}"'

    ext = _native_ext(track)
    if ext:
        res = _res(track, base_url, codec=None, ext=ext.lstrip("."),
                   mime=NATIVE[ext].split(";")[0], bitrate=None,
                   subsong=subsong, dur_attr=dur_attr)
    else:
        res = "".join(_res(track, base_url, codec=c, ext=CODECS[c].url_ext,
                           mime=CODECS[c].content_type, bitrate=br,
                           subsong=subsong, dur_attr=dur_attr)
                      for c, br in _TRANSCODE_RES)

    return (
        f'<item id="{_xescape(item_id or track.get("id") or "")}" '
        f'parentID="{_xescape(parent_id)}" restricted="1">'
        f'<dc:title>{_xescape(title)}</dc:title>'
        f'<dc:creator>{_xescape(artist)}</dc:creator>'
        f'<upnp:artist>{_xescape(artist)}</upnp:artist>'
        f'<upnp:album>{_xescape(album)}</upnp:album>'
        f'{_art_uri(track, base_url)}'
        f'<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'{res}'
        f'</item>'
    )


def _container(id_: str, parent: str, title: str, child_count: int,
               cls: str = "object.container", art: str = "") -> str:
    """A DIDL-Lite ``<container>`` (``art``: an ``upnp:albumArtURI`` element)."""
    return (
        f'<container id="{_xescape(id_)}" parentID="{_xescape(parent)}" '
        f'childCount="{child_count}" restricted="1">'
        f'<dc:title>{_xescape(title)}</dc:title>'
        f'{art}'
        f'<upnp:class>{cls}</upnp:class>'
        f'</container>'
    )


_ALBUM_CLS    = "object.container.album.musicAlbum"
_ARTIST_CLS   = "object.container.person.musicArtist"
_GENRE_CLS    = "object.container.genre.musicGenre"
_FOLDER_CLS   = "object.container.storageFolder"
_PLAYLIST_CLS = "object.container.playlistContainer"


def _album_art(store, rep_id: "str | None", base_url: str) -> str:
    """An album container's cover: its representative track's."""
    t = store.get_track(rep_id) if rep_id else None
    return _art_uri(t, base_url) if t else ""


@router.api_route("/dlna/art/{track_id}.jpg", methods=["GET", "HEAD"], include_in_schema=False)
async def dlna_art(track_id: str):
    """A track's cover (550 px JPEG) for DLNA control points, which never
    sign in — offered only for what the Media Server lists (``_dlna_store``)
    and only while the server isn't locked.  An online-collection tune's
    cover is served only once it is cached (never fetched for this)."""
    from soniqboom.api.art import cover_thumb
    from soniqboom.core import catalogue
    from soniqboom.core.users import accounts_locked
    if accounts_locked():
        raise HTTPException(503, "The server is locked.")
    track = _dlna_store().get_track(track_id)
    if not track:
        raise HTTPException(404, "No such track.")
    data = await cover_thumb(
        track_id, "lg", resolve=not catalogue.is_catalogue_path(track.get("path") or ""))
    if not data:
        raise HTTPException(404, "No cover.")
    return FResponse(content=data, media_type="image/jpeg",
                     headers={"Cache-Control": "public, max-age=3600",
                              "transferMode.dlna.org": "Interactive"})


# ── ContentDirectory Browse action ─────────────────────────────────────────

@router.post("/dlna/cds/control", include_in_schema=False)
async def cds_control(request: Request):
    """ContentDirectory:1 SOAPAction dispatcher.

    Browse, Search, Get*Capabilities and GetSystemUpdateID are implemented;
    every other action returns a 401 ``Invalid Action`` SOAP fault."""
    raw = await request.body()
    action, args = _parse_soap_body(raw)

    if action == "GetSearchCapabilities":
        # Fields the Search action can match on.
        return _soap_response(_UPNP_CDS_NS, action,
                              {"SearchCaps": "dc:title,upnp:artist,upnp:album,dc:creator"})

    if action == "GetSortCapabilities":
        # Empty: SortCriteria is not applied (each container keeps its own
        # order — albums by disc and track, the rest as the store lists them).
        return _soap_response(_UPNP_CDS_NS, action, {"SortCaps": ""})

    if action == "GetSystemUpdateID":
        # Monotonic counter bumped by notify_library_changed() on every
        # library change (the same value carried in the GENA SystemUpdateID
        # event), so a controller can tell its cached tree is stale.
        return _soap_response(_UPNP_CDS_NS, action, {"Id": str(_system_update_id)})

    if action in ("Browse", "Search"):
        # Recovery (users.json unreadable): the server is locked for
        # everyone — the library isn't listed to anonymous controllers either.
        from soniqboom.core.users import accounts_locked
        if accounts_locked():
            return _soap_fault(501, "The server is locked: its user accounts "
                                    "could not be loaded.")

    if action == "Browse":
        return await _do_browse(args, request)

    if action == "Search":
        return await _do_search(args, request)

    return _soap_fault(401, f"Invalid Action: {action!r}")


def _parse_search_criteria(crit: str):
    """Map a UPnP SearchCriteria string to ``filter_tracks`` kwargs.

    Returns the kwargs dict, or ``None`` for "match everything" (``*`` or a
    criteria that only constrains upnp:class).  Pragmatic — real controllers
    send simple ``<field> contains "term"`` clauses; we pull the term out per
    known field and otherwise fall back to a full-text query.
    """
    import re
    c = (crit or "").strip()
    if not c or c == "*":
        return None
    # Exact-match clauses (operator ``=``) → precise field filter.
    filt: dict[str, str] = {}
    for field, key in (("artist", "artist"), ("author", "artist"),
                       ("album", "album"), ("creator", "artist")):
        m = re.search(r'(?:upnp|dc):%s\s*=\s*["\']([^"\']+)["\']' % field, c, re.I)
        if m and key not in filt:
            filt[key] = m.group(1)
    if filt:
        return filt
    # ``contains`` (and anything else) → full-text query, which does substring
    # matching across title/artist/album.  Mapping ``contains`` to an exact
    # field filter would miss partial terms (e.g. artist contains "Beethoven").
    # Use the first quoted term that isn't a UPnP class path.
    for m in re.finditer(r'["\']([^"\']+)["\']', c):
        term = m.group(1)
        if not term.lower().startswith("object."):
            return {"query": term}
    return None


async def _do_search(args: dict[str, str], request: Request) -> FResponse:
    """ContentDirectory:1 Search — returns matching track items as DIDL-Lite."""
    get_store = _dlna_store
    try:
        start = max(0, int(args.get("StartingIndex", "0") or 0))
        req_cnt = int(args.get("RequestedCount", "0") or 0)
    except ValueError:
        return _soap_fault(402, "StartingIndex / RequestedCount must be integers")
    if req_cnt <= 0 or req_cnt > 500:
        req_cnt = 500
    base_url = str(request.base_url).rstrip("/")
    container = args.get("ContainerID", "music") or "music"
    store = get_store()

    filt = _parse_search_criteria(args.get("SearchCriteria", ""))
    if filt is None:
        # Match everything — same as browsing music/all.
        total = await asyncio.to_thread(store.track_count)
        rows = await _list_track_items(store, {}, start, req_cnt, base_url, container)
    else:
        # Search result sets are small in practice; fetch a bounded match set,
        # then page in memory (cap guards a 1-char query on a huge library).
        def _all() -> list[dict]:
            return store.filter_tracks(limit=5000, **filt)
        matches = await asyncio.to_thread(_all)
        total = len(matches)
        page = matches[start:start + req_cnt]
        rows = await asyncio.to_thread(
            lambda: [_track_to_didl_item(t, base_url, container) for t in page])

    result = ("<DIDL-Lite " + _DIDL_NS_BOILERPLATE + ">"
              + "".join(rows) + "</DIDL-Lite>")
    return _soap_response(_UPNP_CDS_NS, "Search", {
        "Result":         result,
        "NumberReturned": str(len(rows)),
        "TotalMatches":   str(total),
        "UpdateID":       str(_system_update_id),
    })


async def _do_browse(args: dict[str, str], request: Request) -> FResponse:
    """Implement the Browse action.

    Object-ID hierarchy:
      "0"       → root (single child: music)
      "music"   → music container (all / albums / artists / genres /
                  folders / playlists)
      "music/all"          → flat list of every track
      "music/albums"       → list of album containers
      "music/artists"      → list of artist containers
      "music/genres"       → list of genre containers
      "music/folders"      → the library folders (fo: containers)
      "music/playlists"    → the shared playlists (pl: containers)
      "al:<b64>"           → tracks of one album (by album name)
      "ar:<b64>"           → that album-artist's albums (aa: containers)
      "aa:<b64>:<b64>"     → tracks of one (album_artist, album)
      "ge:<b64>"           → tracks of one genre
      "fo:<hash>[:<b64>]"  → a folder's subfolders, then its tracks
      "pl:<id>"            → a shared playlist's entries (pi:<id>:<index>)

    BrowseFlag = "BrowseMetadata" returns just the object itself;
    "BrowseDirectChildren" returns its children paginated by
    StartingIndex + RequestedCount.
    """
    obj_id   = args.get("ObjectID", "0") or "0"
    flag     = args.get("BrowseFlag", "BrowseDirectChildren") or "BrowseDirectChildren"
    try:
        start    = max(0, int(args.get("StartingIndex", "0") or 0))
        req_cnt  = int(args.get("RequestedCount", "0") or 0)
    except ValueError:
        return _soap_fault(402, "StartingIndex / RequestedCount must be integers")

    # Default RequestedCount=0 means "all" per spec, but unbounded
    # browse on a 270 K library is a foot-gun for cheap DLNA stacks.
    # Cap at 500 — controllers paginate via subsequent calls.
    if req_cnt <= 0 or req_cnt > 500:
        req_cnt = 500

    # Build base_url for the <res> children.
    base_url = str(request.base_url).rstrip("/")
    if base_url.startswith("http://testserver"):
        # FastAPI TestClient — keep as-is.
        pass

    if flag == "BrowseMetadata":
        return _soap_response(_UPNP_CDS_NS, "Browse",
                              await _browse_metadata(obj_id, base_url))

    # BrowseDirectChildren
    return _soap_response(_UPNP_CDS_NS, "Browse",
                          await _browse_children(obj_id, start, req_cnt, base_url))


# Fixed containers: ObjectID → (parentID, title).
_FIXED = {
    "0":               ("-1",    "SoniqBoom"),
    "music":           ("0",     "Music"),
    "music/all":       ("music", "All Tracks"),
    "music/albums":    ("music", "Albums"),
    "music/artists":   ("music", "Artists"),
    "music/genres":    ("music", "Genres"),
    "music/folders":   ("music", "Folders"),
    "music/playlists": ("music", "Playlists"),
}
_MUSIC_CHILDREN = ("music/all", "music/albums", "music/artists",
                   "music/genres", "music/folders", "music/playlists")


def _didl(parts: "list[str]", returned: int, total: int) -> dict[str, str]:
    """A Browse / Search answer of the DIDL-Lite fragments ``parts``."""
    return {
        "Result":         "<DIDL-Lite " + _DIDL_NS_BOILERPLATE + ">" + "".join(parts)
                          + "</DIDL-Lite>",
        "NumberReturned": str(returned),
        "TotalMatches":   str(total),
        "UpdateID":       str(_system_update_id),
    }


def _name_filter(field: str, name: str) -> dict:
    """``filter_tracks`` arguments for one tag value — for an empty one the
    tracks without that tag (the "[Unknown …]" bucket), never the library."""
    return {field: name} if name else {"untagged": field}


def _split_aa(obj_id: str) -> "tuple[str, str]":
    """``(album artist, album)`` of an ``aa:`` ObjectID."""
    try:
        _, b_artist, b_album = obj_id.split(":", 2)
    except ValueError:
        b_artist = b_album = ""
    return _dec_name(b_artist), _dec_name(b_album)


def _agg_count(rows: "list[dict]", key: str, name: str, *, fold: bool = False) -> int:
    """The ``count`` of the aggregate row named ``name`` (0: none)."""
    want = name.lower() if fold else name
    for d in rows:
        got = d.get(key) or ""
        if (got.lower() if fold else got) == want:
            return int(d.get("count", 0) or 0)
    return 0


def _child_count(store, obj_id: str) -> int:
    """How many children Browse lists for ``obj_id`` (blocking).  A folder's
    subfolders count once each, an album artist's albums once each."""
    if obj_id == "0":
        return 1
    if obj_id == "music":
        return len(_MUSIC_CHILDREN)
    if obj_id == "music/all":
        return store.track_count()
    if obj_id == "music/albums":
        return len(store.aggregate_albums())
    if obj_id == "music/artists":
        return len(store.aggregate_album_artists())
    if obj_id == "music/genres":
        return len(store.aggregate_genres())
    if obj_id == "music/folders":
        return len(_folder_roots(store))
    if obj_id == "music/playlists":
        return len(_shared_playlists())
    if obj_id.startswith("al:"):
        return _agg_count(store.aggregate_albums(), "album", _dec_name(obj_id[3:]))
    if obj_id.startswith("aa:"):
        artist, album = _split_aa(obj_id)
        return _agg_count(store.aggregate_albums(None, artist), "album", album)
    if obj_id.startswith("ar:"):
        return len(store.aggregate_albums(None, _dec_name(obj_id[3:])))
    if obj_id.startswith("ge:"):
        return _agg_count(store.aggregate_genres(), "genre", _dec_name(obj_id[3:]), fold=True)
    if obj_id.startswith("fo:"):
        hit = _folder_of(store, obj_id)
        if hit is None:
            return 0
        listing = _folder_listing(store, *hit)
        return len(listing.subs) + len(listing.files)
    if obj_id.startswith("pl:"):
        pl = _shared_playlist(obj_id[3:])
        return len(_visible_entries(store, pl)) if pl is not None else 0
    return 0


async def _browse_metadata(obj_id: str, base_url: str) -> dict[str, str]:
    """Return the single object's DIDL-Lite (BrowseMetadata mode): a
    container (some controllers fetch this before BrowseDirectChildren) or
    a track item.  Unknown objects → an empty result."""
    store = _dlna_store()
    if obj_id in _FIXED:
        parent, title = _FIXED[obj_id]
        n = await asyncio.to_thread(_child_count, store, obj_id)
        return _didl([_container(obj_id, parent, title, n)], 1, 1)
    if obj_id.startswith(("al:", "ar:", "aa:", "ge:")):
        if obj_id.startswith("aa:"):
            album = _split_aa(obj_id)[1]
            title, parent, cls = (album or "[Unknown Album]",
                                  "ar:" + obj_id.split(":", 2)[1], _ALBUM_CLS)
        elif obj_id.startswith("al:"):
            title, parent, cls = (_dec_name(obj_id[3:]) or "[Unknown Album]",
                                  "music/albums", _ALBUM_CLS)
        elif obj_id.startswith("ge:"):
            title, parent, cls = (_dec_name(obj_id[3:]) or "[No Genre]",
                                  "music/genres", _GENRE_CLS)
        else:  # ar:
            title, parent, cls = (_dec_name(obj_id[3:]) or "[No Album Artist]",
                                  "music/artists", _ARTIST_CLS)
        n = await asyncio.to_thread(_child_count, store, obj_id)
        return _didl([_container(obj_id, parent, title, n, cls)], 1, 1)
    if obj_id.startswith("fo:"):
        def _folder_meta() -> "str | None":
            hit = _folder_of(store, obj_id)
            if hit is None:
                return None
            root, rel = hit
            parent = ("music/folders" if not rel else
                      _folder_id(root["hash"], rel.rpartition("/")[0]))
            title = rel.rpartition("/")[2] if rel else root["title"]
            return _container(obj_id, parent, title, _child_count(store, obj_id), _FOLDER_CLS)
        part = await asyncio.to_thread(_folder_meta)
        if part is not None:
            return _didl([part], 1, 1)
    elif obj_id.startswith("pl:"):
        pl = _shared_playlist(obj_id[3:])
        if pl is not None:
            n = await asyncio.to_thread(_child_count, store, obj_id)
            return _didl([_container(obj_id, "music/playlists", pl.get("name") or "Playlist",
                                     n, _PLAYLIST_CLS)], 1, 1)
    elif obj_id.startswith("pi:"):
        entry = _playlist_entry(store, obj_id)
        if entry is not None:
            pl_id, track, sub = entry
            item = await asyncio.to_thread(
                _track_to_didl_item, track, base_url, "pl:" + pl_id,
                item_id=obj_id, subsong=sub)
            return _didl([item], 1, 1)
    else:
        track = store.get_track(obj_id)
        if track:
            item = await asyncio.to_thread(_track_to_didl_item, track, base_url, "music/all")
            return _didl([item], 1, 1)
    return _didl([], 0, 0)


# ── Folders ────────────────────────────────────────────────────────────────
# The library's folders as the index knows them (the web folder tree's
# in-memory path caches, ``api/fstree`` — never a disk or network listing):
#   fo:<root path hash>                  a library folder (scan root)
#   fo:<root path hash>:<b64(rel path)>  a folder inside it
# A folder lists its subfolders, then its tracks (an archive's tunes in the
# folder that holds the archive, as the web player lists them).

def _folder_roots(store) -> "list[dict]":
    """The library folders the Media Server shows (hidden online
    collections left out), with the titles controllers display."""
    from soniqboom.config import settings
    aliases = getattr(settings, "folder_aliases", None) or {}
    shares = getattr(settings, "network_shares", None) or {}
    out = []
    for d in store.list_scan_dirs():
        path = d.get("path") or ""
        h = d.get("path_hash")
        if not path or not h:
            continue
        share = shares.get(d.get("network_share_id") or "") or {}
        name = path.rstrip("/").rpartition("/")[2] or path
        title = (d.get("label") or aliases.get(path) or share.get("alias") or name)
        out.append({"hash": h, "path": path, "title": title})
    out.sort(key=lambda r: r["title"].lower())
    return out


def _folder_id(root_hash: str, rel: str) -> str:
    return f"fo:{root_hash}:{_enc_name(rel)}" if rel else f"fo:{root_hash}"


def _folder_of(store, obj_id: str) -> "tuple[dict, str] | None":
    """``(root, rel)`` of a folder ObjectID, or None (not a shown root)."""
    h, _, b_rel = obj_id[3:].partition(":")
    roots = _folder_roots(store)
    for gone in [k for k in _ROOT_IDS if k not in {r["hash"] for r in roots}]:
        _ROOT_IDS.pop(gone, None)
    root = next((r for r in roots if r["hash"] == h), None)
    if root is None:
        return None
    rel = _dec_name(b_rel).strip("/") if b_rel else ""
    return root, rel


# Track ids of a library folder's shaped path cache, in its path order —
# keyed on the cache's own list object (kept referenced here, so its
# identity is stable): rebuilt only when the folder tree rebuilt it.
_ROOT_IDS: dict[str, tuple] = {}


def _root_rows(root: dict) -> "tuple[list[str], list[str]]":
    """``(paths, ids)`` of a library folder's tracks in path order (blocking)."""
    from soniqboom.api import fstree
    from soniqboom.core import catalogue
    from soniqboom.core.store import get_store
    store = get_store()                      # the caches are the folder tree's own
    if catalogue.is_catalogue_root(root["path"]):
        return fstree._catalogue_sorted(store, root["hash"])
    paths, dicts = fstree._get_or_build_scan_root_sorted(store, root["hash"])
    memo = _ROOT_IDS.get(root["hash"])
    if memo is None or memo[0] is not dicts:
        memo = _ROOT_IDS[root["hash"]] = (dicts, [d.get("id") for d in dicts])
    return paths, memo[1]


class _Listing:
    """A folder's ``subs`` (``(name, lo, hi)``: its path rows ``paths[lo:hi]``)
    and ``files`` (the ids of the tracks directly in it that the store shows)."""
    __slots__ = ("paths", "prefix", "subs", "files")

    def __init__(self, paths, prefix, subs, files) -> None:
        self.paths, self.prefix, self.subs, self.files = paths, prefix, subs, files

    def direct_count(self, lo: int, hi: int, name: str) -> int:
        """How many entries (subfolders + tracks) subfolder ``name`` lists."""
        from bisect import bisect_left
        paths, prefix = self.paths, f"{self.prefix}{name}/"
        plen, n, i = len(prefix), 0, lo
        while i < hi:
            head = paths[i][plen:].split("::", 1)[0]
            slash = head.find("/")
            if slash > 0:
                nxt = bisect_left(paths, f"{prefix}{head[:slash]}/\U0010FFFF", i, hi)
                i = nxt if nxt > i else i + 1
            else:
                i += 1
            n += 1
        return n


def _folder_listing(store, root: dict, rel: str) -> _Listing:
    """A folder's subfolders and tracks (blocking).  Bisect-skips each
    subfolder's subtree, so a big tree costs O(subfolders · log n) plus its
    own tracks."""
    from bisect import bisect_left
    from soniqboom.core.filesource import is_remote_path
    paths, ids = _root_rows(root)
    base = (f"{root['path']}:/" if is_remote_path(root["path"])
            else root["path"].rstrip("/") + "/")
    prefix = base + (rel + "/" if rel else "")
    plen, n = len(prefix), len(paths)
    i = bisect_left(paths, prefix)
    subs: list[tuple[str, int, int]] = []
    files: list[str] = []
    while i < n and paths[i].startswith(prefix):
        head = paths[i][plen:].split("::", 1)[0]
        slash = head.find("/")
        if slash > 0:
            child = head[:slash]
            # Past the whole subtree (U+10FFFF sorts after any name; ``i + 1``
            # guarantees progress) — see fstree._local_children_from_store.
            nxt = bisect_left(paths, f"{prefix}{child}/\U0010FFFF", i)
            nxt = nxt if nxt > i else i + 1
            subs.append((child, i, nxt))
            i = nxt
        else:
            files.append(ids[i])
            i += 1
    subs.sort(key=lambda s: s[0].lower())
    # Only tracks the store still has and shows (the path cache may hold
    # one removed since), so paging and the totals agree.
    files = [f for f, t in zip(files, store.get_tracks_batch(files)) if t]
    return _Listing(paths, prefix, subs, files)


# ── Playlists ──────────────────────────────────────────────────────────────
# Only the playlists their owners share with every account
# (``store.playlist_offered`` — the same rule the Import list follows): the
# Media Server answers anyone on the network, without signing in.
#   pl:<playlist id>          a shared playlist
#   pi:<playlist id>:<index>  one entry of it (a track, or one tune of it)

def _shared_playlists() -> "list[dict]":
    from soniqboom.core.store import get_store
    store = get_store()
    pls = [p for p in store.list_playlists() if store.playlist_offered(p)]
    pls.sort(key=lambda p: ((p.get("name") or "").lower(), p.get("id") or ""))
    return pls


def _shared_playlist(pl_id: str) -> "dict | None":
    from soniqboom.core.store import get_store
    store = get_store()
    pl = store.get_playlist(pl_id) if pl_id else None
    return pl if pl is not None and store.playlist_offered(pl) else None


def _visible_entries(store, pl: dict) -> "list[tuple[int, dict, int | None]]":
    """``(index, track, tune)`` of a playlist's entries whose track the store
    still has and shows — what Browse lists and counts."""
    from soniqboom.api.playlist import _entry_id, _entry_sub
    entries = pl.get("track_ids") or []
    tracks = store.get_tracks_batch([_entry_id(e) or "" for e in entries])
    return [(i, t, _entry_sub(e)) for i, (e, t) in enumerate(zip(entries, tracks)) if t]


def _playlist_entry(store, obj_id: str) -> "tuple[str, dict, int | None] | None":
    """``(playlist id, track, tune)`` of a ``pi:`` ObjectID, or None."""
    from soniqboom.api.playlist import _entry_id, _entry_sub
    pl_id, _, idx = obj_id[3:].rpartition(":")
    pl = _shared_playlist(pl_id)
    if pl is None or not idx.isdigit():
        return None
    entries = pl.get("track_ids") or []
    i = int(idx)
    if i >= len(entries):
        return None
    track = store.get_track(_entry_id(entries[i]) or "")
    return (pl_id, track, _entry_sub(entries[i])) if track else None


def _enc_name(name: str) -> str:
    """URL-safe base64 of an album/artist NAME → an opaque, REVERSIBLE ObjectID
    token (no server-side hash→name map needed, unlike the old sha1 plan)."""
    import base64
    return base64.urlsafe_b64encode((name or "").encode("utf-8")).decode("ascii")


def _dec_name(token: str) -> str:
    """Reverse of _enc_name; returns '' on a malformed token."""
    import base64
    try:
        pad = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")
    except Exception:                       # noqa: BLE001 — garbage token → empty
        return ""


async def _list_track_items(store, filt: dict, start: int, count: int, base_url: str,
                            parent_id: str = "music") -> list[str]:
    """Paginated DIDL-Lite ``<item>`` rows for tracks matching ``filt``."""
    def _fetch() -> list[dict]:
        try:
            return store.filter_tracks(limit=count, offset=start, **filt)
        except TypeError:                   # older store: no ``offset`` kwarg
            allt = store.filter_tracks(limit=start + count, **filt)
            return allt[start:start + count]
    tracks = await asyncio.to_thread(_fetch)

    def _render() -> list[str]:
        return [_track_to_didl_item(t, base_url, parent_id) for t in tracks]
    return await asyncio.to_thread(_render)


# An album longer than this is listed in the store's order, unsorted.
_ALBUM_SORT_MAX = 5000


def _num(v) -> int:
    """A disc / track number as an int ("3/12" → 3); 0 when there is none."""
    try:
        return int(float(str(v).split("/", 1)[0]))
    except (TypeError, ValueError, OverflowError):
        return 0


async def _album_items(store, filt: dict, start: int, count: int, base_url: str,
                       parent_id: str) -> "tuple[int, list[str]]":
    """``(total, items)`` of an album in disc and track order (the order
    Subsonic lists it in), paginated."""
    def _album() -> "tuple[int, list[str]] | None":
        rows = store.filter_tracks(limit=_ALBUM_SORT_MAX + 1, **filt)
        if len(rows) > _ALBUM_SORT_MAX:
            return None
        rows.sort(key=lambda t: (_num(t.get("disc_number")), _num(t.get("track_number")),
                                 (t.get("title") or "").lower()))
        return len(rows), [_track_to_didl_item(t, base_url, parent_id)
                           for t in rows[start:start + count]]
    got = await asyncio.to_thread(_album)
    if got is not None:
        return got
    total = await asyncio.to_thread(_child_count, store, parent_id)
    return total, await _list_track_items(store, filt, start, count, base_url, parent_id)


async def _browse_children(
    obj_id: str, start: int, count: int, base_url: str,
) -> dict[str, str]:
    """Return paginated children of ``obj_id`` as DIDL-Lite."""
    store = _dlna_store()
    parts: list[str] = []
    total = 0

    def _page(rows: list) -> list:
        return rows[start:start + count]

    if obj_id == "0":
        # Root → single Music container
        total = 1
        if start == 0:
            parts.append(_container("music", "0", "Music", len(_MUSIC_CHILDREN)))
    elif obj_id == "music":
        # Music → All / Albums / Artists / Genres / Folders / Playlists.  Real
        # child counts so a controller doesn't render one as "0 items" and
        # refuse to descend.
        children = await asyncio.to_thread(
            lambda: [(cid, _child_count(store, cid)) for cid in _MUSIC_CHILDREN])
        total = len(children)
        for cid, ccount in _page(children):
            parts.append(_container(cid, "music", _FIXED[cid][1], ccount))
    elif obj_id == "music/all":
        # Flat list of every track — paginated.  store.filter_tracks
        # is synchronous and on a 270 K-track library walks the in-memory
        # sorted index; that's a 30-200 ms scan on the dev box and
        # blocks the asyncio loop for every other request landing
        # during the Browse window.  Push it (and the row rendering — a
        # token mint per row) to the default executor so concurrent
        # /cast/{token}/ streams don't stall.
        total = await asyncio.to_thread(store.track_count)
        parts += await _list_track_items(store, {}, start, count, base_url, "music/all")
    elif obj_id == "music/albums":
        # One container per album → ``al:<base64(album)>``.
        def _albums() -> tuple[int, list[str]]:
            rows = store.aggregate_albums()
            return len(rows), [
                _container("al:" + _enc_name(d.get("album") or ""), "music/albums",
                           d.get("album") or "[Unknown Album]", int(d.get("count", 0) or 0),
                           _ALBUM_CLS, _album_art(store, d.get("track_id"), base_url))
                for d in _page(rows)]
        total, rows = await asyncio.to_thread(_albums)
        parts += rows
    elif obj_id == "music/artists":
        # One container per album-artist → ``ar:<base64(album_artist)>``;
        # its children are its albums.
        def _artists() -> tuple[int, list[str]]:
            rows = store.aggregate_album_artists()
            out = []
            for d in _page(rows):
                name = d.get("album_artist") or ""
                cid = "ar:" + _enc_name(name)
                out.append(_container(cid, "music/artists", name or "[No Album Artist]",
                                      _child_count(store, cid), _ARTIST_CLS))
            return len(rows), out
        total, rows = await asyncio.to_thread(_artists)
        parts += rows
    elif obj_id == "music/genres":
        # One container per genre → ``ge:<base64(genre)>``.
        rows = await asyncio.to_thread(store.aggregate_genres)
        total = len(rows)
        for d in _page(rows):
            name = d.get("genre") or ""
            parts.append(_container("ge:" + _enc_name(name), "music/genres",
                                    name or "[No Genre]", int(d.get("count", 0) or 0),
                                    _GENRE_CLS))
    elif obj_id == "music/folders":
        # One container per library folder → ``fo:<root hash>``.
        def _roots() -> tuple[int, list[str]]:
            roots = _folder_roots(store)
            return len(roots), [
                _container(_folder_id(r["hash"], ""), "music/folders", r["title"],
                           _child_count(store, _folder_id(r["hash"], "")), _FOLDER_CLS)
                for r in _page(roots)]
        total, rows = await asyncio.to_thread(_roots)
        parts += rows
    elif obj_id == "music/playlists":
        def _playlists() -> tuple[int, list[str]]:
            pls = _shared_playlists()
            return len(pls), [
                _container("pl:" + (p.get("id") or ""), "music/playlists",
                           p.get("name") or "Playlist", len(_visible_entries(store, p)),
                           _PLAYLIST_CLS)
                for p in _page(pls)]
        total, rows = await asyncio.to_thread(_playlists)
        parts += rows
    elif obj_id.startswith("ar:"):
        # Artist → that album-artist's ALBUMS (containers ``aa:<artist>:<album>``).
        artist = _dec_name(obj_id[3:])

        def _artist_albums() -> tuple[int, list[str]]:
            rows = store.aggregate_albums(None, artist)
            return len(rows), [
                _container("aa:" + _enc_name(artist) + ":" + _enc_name(d.get("album") or ""),
                           obj_id, d.get("album") or "[Unknown Album]",
                           int(d.get("count", 0) or 0), _ALBUM_CLS,
                           _album_art(store, d.get("track_id"), base_url))
                for d in _page(rows)]
        total, rows = await asyncio.to_thread(_artist_albums)
        parts += rows
    elif obj_id.startswith("aa:"):
        # Artist + album → tracks (disambiguated, so same-named albums by
        # different artists don't bleed together), in disc and track order.
        artist, album = _split_aa(obj_id)
        total, rows = await _album_items(
            store, {**_name_filter("album_artist", artist), **_name_filter("album", album)},
            start, count, base_url, obj_id)
        parts += rows
    elif obj_id.startswith("al:"):
        # Album (top level) → tracks, by album name only, in disc and track order.
        total, rows = await _album_items(
            store, _name_filter("album", _dec_name(obj_id[3:])), start, count, base_url, obj_id)
        parts += rows
    elif obj_id.startswith("ge:"):
        # Genre → its tracks.
        genre = _dec_name(obj_id[3:])
        total = await asyncio.to_thread(_child_count, store, obj_id)
        parts += await _list_track_items(store, _name_filter("genre", genre),
                                         start, count, base_url, obj_id)
        total = total or start + len(parts)
    elif obj_id.startswith("fo:"):
        # Folder → its subfolders, then its tracks.
        def _folder() -> tuple[int, list[str]]:
            hit = _folder_of(store, obj_id)
            if hit is None:
                return 0, []
            root, rel = hit
            listing = _folder_listing(store, root, rel)
            subs, files = listing.subs, listing.files
            out = [_container(_folder_id(root["hash"], f"{rel}/{name}" if rel else name),
                              obj_id, name, listing.direct_count(lo, hi, name), _FOLDER_CLS)
                   for name, lo, hi in subs[start:start + count]]
            ids = files[max(0, start - len(subs)):max(0, start + count - len(subs))]
            out += [_track_to_didl_item(t, base_url, obj_id)
                    for t in store.get_tracks_batch(ids) if t]
            return len(subs) + len(files), out
        total, rows = await asyncio.to_thread(_folder)
        parts += rows
    elif obj_id.startswith("pl:"):
        # Shared playlist → its entries in order (``pi:<id>:<index>``).
        pl = _shared_playlist(obj_id[3:])
        if pl is not None:
            def _entries() -> tuple[int, list[str]]:
                shown = _visible_entries(store, pl)
                return len(shown), [
                    _track_to_didl_item(t, base_url, obj_id, item_id=f"pi:{obj_id[3:]}:{i}",
                                        subsong=sub)
                    for i, t, sub in _page(shown)]
            total, rows = await asyncio.to_thread(_entries)
            parts += rows
    # (Unknown obj_id: empty)

    return _didl(parts, len(parts), total)


# ── ConnectionManager control + event stubs ────────────────────────────────

@router.post("/dlna/cm/control", include_in_schema=False)
async def cm_control(request: Request):
    """Minimal ConnectionManager SOAP handler.  GetProtocolInfo returns
    the codec set our /cast/{token}/ byte-server can deliver."""
    raw = await request.body()
    action, _args = _parse_soap_body(raw)

    if action == "GetProtocolInfo":
        # Codec set our byte-server can deliver.  Each entry is the
        # 4-field DLNA Source ProtocolInfo:
        #
        #   <protocol>:<network>:<contentFormat>:<additionalInfo>
        #
        # The 4th field (additionalInfo) carries the contentFeatures
        # advertisement — DLNA.ORG_PN, DLNA.ORG_OP, DLNA.ORG_FLAGS
        # in the same form we emit on the per-stream response header.
        # Strict renderers (Samsung SmartTV / late Sony Bravia) reject
        # entries that end in ``:*`` because they treat it as "no DLNA
        # features advertised" and bail out of the picker before even
        # GETting the device.xml a second time.  See DLNA Networked
        # Device Guidelines 7.4.34 + ConnectionManager:1 spec §2.2.4.
        from soniqboom.core.cast_codecs import content_features
        codec_entries = [
            ("audio/mpeg",  "mp3"),
            ("audio/flac",  "flac"),
            ("audio/wav",   "wav"),
            ("audio/ogg",   "ogg"),
            ("audio/mp4",   "aac"),
            ("audio/aac",   "aac"),
        ]
        source_entries: list[str] = []
        for mime, codec in codec_entries:
            features = content_features(codec) or "*"
            source_entries.append(f"http-get:*:{mime}:{features}")
        source = ",".join(source_entries)
        return _soap_response(_UPNP_CM_NS, action, {
            "Source": source,
            "Sink":   "",
        })

    if action == "GetCurrentConnectionIDs":
        return _soap_response(_UPNP_CM_NS, action, {"ConnectionIDs": ""})

    return _soap_fault(401, f"Invalid Action: {action!r}")


# ── GENA eventing (ContentDirectory) ───────────────────────────────────────
# Real subscriptions: on SUBSCRIBE we register the controller's callback and
# send the initial event; when the library changes we bump SystemUpdateID and
# NOTIFY every live subscriber so it re-browses.  ConnectionManager eventing
# stays a stub — its evented state never changes.

import threading as _threading
import time as _gtime
import uuid as _guuid

_SUB_LOCK = asyncio.Lock()
_cds_subs: dict[str, dict] = {}        # sid -> {callback, host, expires, seq}
_system_update_id: int = 1
_MAX_SUBS = 64                          # cap registry growth (cheap DoS guard)

# ``_system_update_id`` is mutated both from the event loop (on_library_changed)
# and, on the no-loop fallback, from a library-scan worker thread.  ``+= 1`` is
# read-modify-write and not atomic across threads, so guard it with a plain
# threading.Lock (held only for the increment — never across an await).
_UPDATE_ID_LOCK = _threading.Lock()


def _bump_update_id() -> int:
    """Atomically increment the SystemUpdateID and return the new value."""
    global _system_update_id
    with _UPDATE_ID_LOCK:
        _system_update_id += 1
        return _system_update_id


def _parse_timeout(raw: str | None) -> int:
    """``Second-1800`` / ``infinite`` → seconds, clamped to [60, 3600]."""
    import re
    if not raw:
        return 1800
    raw = raw.strip().lower()
    if "infinite" in raw:
        return 3600
    m = re.match(r"second-(\d+)", raw)
    return max(60, min(3600, int(m.group(1)) if m else 1800))


def _parse_callback(raw: str | None) -> str | None:
    """``CALLBACK: <http://host/path> [<...>]`` → first URL."""
    import re
    if not raw:
        return None
    m = re.search(r"<([^>]+)>", raw)
    return m.group(1) if m else None


def _propertyset() -> str:
    return (
        '<?xml version="1.0"?>'
        '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
        f'<e:property><SystemUpdateID>{_system_update_id}</SystemUpdateID></e:property>'
        '<e:property><ContainerUpdateIDs></ContainerUpdateIDs></e:property>'
        '</e:propertyset>'
    )


async def _send_gena_notify(sid: str, sub: dict) -> bool:
    """POST a GENA NOTIFY to a subscriber's callback.  False on failure."""
    import httpx
    # Allocate this NOTIFY's SEQ *synchronously* — read and bump with no await
    # in between, so the event loop can't interleave two concurrent notifies to
    # the same subscriber onto the same sequence number.  GENA (UPnP Device
    # Architecture §4.1.4) requires SEQ to start at 0 and increment by exactly
    # one per event; reading SEQ before the await and bumping after would let
    # two overlapping sends both ship SEQ=0 and leave a gap.
    seq = sub["seq"]
    sub["seq"] = seq + 1
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "NT":  "upnp:event",
        "NTS": "upnp:propchange",
        "SID": sid,
        "SEQ": str(seq),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.request(
                "NOTIFY", sub["callback"], content=_propertyset(), headers=headers)
        return r.status_code < 400
    except Exception:                       # noqa: BLE001 — controller offline
        return False


async def _send_initial_notify(sid: str, sub: dict) -> None:
    """Initial GENA event, fired just after a SUBSCRIBE.  A short delay lets the
    SUBSCRIBE 200 (carrying the SID) reach the controller before the first
    NOTIFY, so the controller has the subscription registered when it lands."""
    await asyncio.sleep(0.1)
    await _send_gena_notify(sid, sub)


def _prune_subs(now: float) -> None:
    for k in [k for k, v in _cds_subs.items() if v["expires"] <= now]:
        _cds_subs.pop(k, None)


async def on_library_changed() -> None:
    """Bump SystemUpdateID and NOTIFY all live CDS subscribers."""
    _bump_update_id()
    now = _gtime.monotonic()
    async with _SUB_LOCK:
        _prune_subs(now)
        subs = list(_cds_subs.items())
    for sid, sub in subs:
        await _send_gena_notify(sid, sub)


def notify_library_changed() -> None:
    """Sync entry point (called from the library-change path).  Schedules the
    async NOTIFY when a loop is running; otherwise just bumps the counter."""
    try:
        asyncio.get_running_loop().create_task(on_library_changed())
    except RuntimeError:                    # no running loop (worker thread)
        _bump_update_id()


@router.api_route(
    "/dlna/cds/event", methods=["SUBSCRIBE", "UNSUBSCRIBE"], include_in_schema=False,
)
async def cds_event(request: Request):
    sid_hdr = request.headers.get("SID")
    if request.method == "UNSUBSCRIBE":
        if sid_hdr:
            async with _SUB_LOCK:
                _cds_subs.pop(sid_hdr, None)
        return PlainTextResponse("", status_code=200)

    # SUBSCRIBE
    now = _gtime.monotonic()
    timeout_s = _parse_timeout(request.headers.get("TIMEOUT"))
    callback = _parse_callback(request.headers.get("CALLBACK"))

    if sid_hdr and not callback:            # renewal
        async with _SUB_LOCK:
            sub = _cds_subs.get(sid_hdr)
            if sub is None:
                return PlainTextResponse("", status_code=412)
            sub["expires"] = now + timeout_s
        return PlainTextResponse("", status_code=200, headers={
            "SID": sid_hdr, "TIMEOUT": f"Second-{timeout_s}",
            "Server": "UPnP/1.0 SoniqBoom-DLNA/1.0"})

    if not callback:
        return PlainTextResponse("", status_code=412)

    # SSRF guard: a subscriber may only register a callback on its OWN address,
    # so we can't be coerced into NOTIFYing an arbitrary internal host.
    from urllib.parse import urlparse
    cb_host = urlparse(callback).hostname or ""
    client_host = request.client.host if request.client else ""
    if cb_host != client_host:
        return PlainTextResponse("Callback host must match subscriber",
                                 status_code=412)

    sid = "uuid:" + str(_guuid.uuid4())
    sub = {"callback": callback, "host": cb_host, "expires": now + timeout_s, "seq": 0}
    async with _SUB_LOCK:
        _prune_subs(now)
        if len(_cds_subs) >= _MAX_SUBS:
            # Registry full of live subscriptions — refuse rather than grow
            # unbounded.  503 tells the controller to retry later.
            return PlainTextResponse("Too many subscriptions", status_code=503)
        _cds_subs[sid] = sub
    # GENA requires the initial event right after SUBSCRIBE (seq 0); fire it
    # slightly deferred so the SUBSCRIBE 200 reaches the controller first.
    asyncio.create_task(_send_initial_notify(sid, sub))
    return PlainTextResponse("", status_code=200, headers={
        "SID": sid, "TIMEOUT": f"Second-{timeout_s}",
        "Server": "UPnP/1.0 SoniqBoom-DLNA/1.0"})


@router.api_route(
    "/dlna/cm/event", methods=["SUBSCRIBE", "UNSUBSCRIBE"], include_in_schema=False,
)
async def cm_event(request: Request):
    # ConnectionManager evented state never changes — accept and no-op.
    return PlainTextResponse("", status_code=200, headers={
        "Server":  "UPnP/1.0 SoniqBoom-DLNA/1.0",
        "SID":     "uuid:00000000-0000-0000-0000-0000000000cm",
        "TIMEOUT": "Second-1800",
    })
