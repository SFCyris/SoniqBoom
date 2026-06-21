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
        └── artists                     (one container per album_artist)
            └── ar:<b64(album_artist)>  (that artist's albums)
                └── aa:<b64(artist)>:<b64(album)>  (tracks of one album)

    ObjectID name tokens are URL-safe base64 of the album / artist NAME
    (reversible, so no server-side hash→name map is needed).

Each track returns a DIDL-Lite ``<item>`` whose ``<res>`` URL points
at the existing ``/cast/{token}/<filename>.<ext>`` anonymous byte
server — same tokens the Cast / DLNA / AirPlay sender uses for its
outgoing direction.  Browse-time URLs use the user-less anonymous
token form (no ``uid`` claim) and a shorter 15-minute TTL so the
exposure window is bounded.
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


def _track_to_didl_item(track: dict, base_url: str, parent_id: str = "music") -> str:
    """Render a single track row as a DIDL-Lite ``<item>``.

    The ``<res>`` URL points at the existing anonymous cast byte server
    with a fresh signed token.  Tokens are user-less (no ``uid`` claim)
    so DLNA controllers — which never authenticate to us — get exactly
    the access scope the operator agreed to when they enabled the
    Media Server.

    ``parent_id`` is the ObjectID of the container this item is being
    returned under (``music/all``, ``al:<b64>``, ``aa:<b64>:<b64>`` …).
    ContentDirectory:1 §2.7.1 requires an item's ``parentID`` to match
    the container that lists it; strict controllers use it to anchor the
    item back into the tree, so a constant ``"music"`` breaks descent.
    """
    from soniqboom.core import cast_tokens
    from soniqboom.core.cast_codecs import CODECS

    track_id = track.get("id") or ""
    title    = track.get("title")  or "Unknown Track"
    artist   = (track.get("album_artist") or track.get("artist") or "Unknown Artist")
    album    = track.get("album")  or "Unknown Album"
    duration = float(track.get("duration") or 0)
    src_fmt  = (track.get("format") or "").lower()
    # Map library format → codec spec (defaults to mp3 mime if unknown)
    codec_key = src_fmt if src_fmt in CODECS else "mp3"
    spec = CODECS.get(codec_key, CODECS["mp3"])

    # Mint anonymous, short-TTL token.  15 min = enough for one
    # full track + Range re-fetches; not enough for a stolen-URL
    # attacker to play a whole album.
    token = cast_tokens.issue_token(
        track_id=track_id,
        codec=None,           # native delivery — let the renderer pick
        ttl_seconds=15 * 60,
    )
    filename = cast_tokens.safe_filename(track, spec.url_ext)
    # URL-encode the filename so spaces and any other URL-unsafe chars
    # become %20 etc.  ``safe_filename`` already stripped CR/LF and
    # non-ASCII; this layer covers spaces + parens + the handful of
    # ASCII chars that are technically URL-unsafe.  Without this, strict
    # DLNA renderers (LG WebOS, older Samsung) reject the URL outright
    # because they treat the space as an HTTP header delimiter.
    safe_path = urllib.parse.quote(filename, safe="._-()")
    url = f"{base_url.rstrip('/')}/cast/{token}/{safe_path}"

    # Duration: HH:MM:SS.fff per DLNA spec
    h  = int(duration // 3600)
    m  = int((duration % 3600) // 60)
    s  = duration - (h * 3600 + m * 60)
    dur_str = f"{h:d}:{m:02d}:{s:06.3f}"

    return (
        f'<item id="{_xescape(track_id)}" parentID="{_xescape(parent_id)}" restricted="1">'
        f'<dc:title>{_xescape(title)}</dc:title>'
        f'<dc:creator>{_xescape(artist)}</dc:creator>'
        f'<upnp:artist>{_xescape(artist)}</upnp:artist>'
        f'<upnp:album>{_xescape(album)}</upnp:album>'
        f'<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:{spec.content_type}:*" '
        f'duration="{dur_str}">{_xescape(url)}</res>'
        f'</item>'
    )


def _container(id_: str, parent: str, title: str, child_count: int) -> str:
    return (
        f'<container id="{_xescape(id_)}" parentID="{_xescape(parent)}" '
        f'childCount="{child_count}" restricted="1">'
        f'<dc:title>{_xescape(title)}</dc:title>'
        f'<upnp:class>object.container</upnp:class>'
        f'</container>'
    )


# ── ContentDirectory Browse action ─────────────────────────────────────────

@router.post("/dlna/cds/control", include_in_schema=False)
async def cds_control(request: Request):
    """ContentDirectory:1 SOAPAction dispatcher.

    Only Browse + Get*Capabilities + GetSystemUpdateID are implemented;
    every other action returns a 401 ``Invalid Action`` SOAP fault."""
    raw = await request.body()
    action, args = _parse_soap_body(raw)

    if action == "GetSearchCapabilities":
        # Fields the Search action can match on.
        return _soap_response(_UPNP_CDS_NS, action,
                              {"SearchCaps": "dc:title,upnp:artist,upnp:album,dc:creator"})

    if action == "GetSortCapabilities":
        # Also empty — we return tracks in library/insertion order.
        return _soap_response(_UPNP_CDS_NS, action, {"SortCaps": ""})

    if action == "GetSystemUpdateID":
        # Monotonic counter bumped by notify_library_changed() on every
        # library change (the same value carried in the GENA SystemUpdateID
        # event), so a controller can tell its cached tree is stale.
        return _soap_response(_UPNP_CDS_NS, action, {"Id": str(_system_update_id)})

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
    from soniqboom.core.store import get_store
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
      "music"   → music container (all / albums / artists)
      "music/all"          → flat list of every track
      "music/albums"       → list of album containers
      "music/artists"      → list of artist containers
      "al:<b64>"           → tracks of one album (by album name)
      "ar:<b64>"           → that album-artist's albums (aa: containers)
      "aa:<b64>:<b64>"     → tracks of one (album_artist, album)

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


async def _browse_metadata(obj_id: str, base_url: str) -> dict[str, str]:
    """Return the single object's DIDL-Lite (BrowseMetadata mode)."""
    if obj_id in ("0", "music", "music/all", "music/albums", "music/artists"):
        title = {
            "0":              "SoniqBoom",
            "music":          "Music",
            "music/all":      "All Tracks",
            "music/albums":   "Albums",
            "music/artists":  "Artists",
        }[obj_id]
        # Just the container as a single item
        result = (
            '<DIDL-Lite ' + _DIDL_NS_BOILERPLATE + '>'
            + _container(obj_id, "-1" if obj_id == "0" else "0", title, 0)
            + '</DIDL-Lite>'
        )
        return {
            "Result":         result,
            "NumberReturned": "1",
            "TotalMatches":   "1",
            "UpdateID":       str(_system_update_id),
        }
    # Album / artist / artist-album container metadata — some controllers fetch
    # this before BrowseDirectChildren; return it as the container itself.
    if obj_id.startswith(("al:", "ar:", "aa:")):
        if obj_id.startswith("aa:"):
            try:
                _, b_artist, b_album = obj_id.split(":", 2)
            except ValueError:
                b_artist = b_album = ""
            title = _dec_name(b_album) or "[Unknown Album]"
            parent = "ar:" + b_artist
        elif obj_id.startswith("al:"):
            title = _dec_name(obj_id[3:]) or "[Unknown Album]"
            parent = "music/albums"
        else:  # ar:
            title = _dec_name(obj_id[3:]) or "[No Album Artist]"
            parent = "music/artists"
        result = (
            '<DIDL-Lite ' + _DIDL_NS_BOILERPLATE + '>'
            + _container(obj_id, parent, title, 0)
            + '</DIDL-Lite>'
        )
        return {
            "Result":         result,
            "NumberReturned": "1",
            "TotalMatches":   "1",
            "UpdateID":       str(_system_update_id),
        }

    # Unknown object → empty result
    return {
        "Result":         '<DIDL-Lite ' + _DIDL_NS_BOILERPLATE + '></DIDL-Lite>',
        "NumberReturned": "0",
        "TotalMatches":   "0",
        "UpdateID":       str(_system_update_id),
    }


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


async def _browse_children(
    obj_id: str, start: int, count: int, base_url: str,
) -> dict[str, str]:
    """Return paginated children of ``obj_id`` as DIDL-Lite."""
    from soniqboom.core.store import get_store

    didl_parts: list[str] = ["<DIDL-Lite " + _DIDL_NS_BOILERPLATE + ">"]
    total = 0
    returned = 0

    if obj_id == "0":
        # Root → single Music container
        didl_parts.append(_container(
            "music", "0", "Music",
            get_store().track_count(),
        ))
        total = 1
        returned = 1 if start == 0 else 0
        if returned == 0:
            didl_parts = ["<DIDL-Lite " + _DIDL_NS_BOILERPLATE + ">"]
    elif obj_id == "music":
        # Music → All / Albums / Artists.  Real child counts so a controller
        # doesn't render Albums/Artists as "0 items" and refuse to descend.
        store = get_store()
        n_albums  = len(await asyncio.to_thread(store.aggregate_albums))
        n_artists = len(await asyncio.to_thread(store.aggregate_album_artists))
        children = [
            ("music/all",     "All Tracks", store.track_count()),
            ("music/albums",  "Albums",     n_albums),
            ("music/artists", "Artists",    n_artists),
        ]
        total = len(children)
        for i, (cid, ctitle, ccount) in enumerate(children):
            if i < start: continue
            if returned >= count: break
            didl_parts.append(_container(cid, "music", ctitle, ccount))
            returned += 1
    elif obj_id == "music/all":
        # Flat list of every track — paginated.  store.filter_tracks
        # is synchronous and on a 270 K-track library walks the in-memory
        # sorted index; that's a 30-200 ms scan on the dev box and
        # blocks the asyncio loop for every other request landing
        # during the Browse window.  Push it to the default executor
        # so concurrent /cast/{token}/ streams don't stall.
        store = get_store()
        total = await asyncio.to_thread(store.track_count)

        def _fetch_page() -> list[dict]:
            try:
                return store.filter_tracks(limit=count, offset=start)
            except TypeError:
                all_tracks = store.filter_tracks(limit=start + count)
                return all_tracks[start:start + count]
        tracks = await asyncio.to_thread(_fetch_page)

        # DIDL-Lite rendering for each track is also CPU-bound at large
        # ``count`` (string concat + html.escape + token mint per row).
        # 500 rows is ~30 ms; off-loop it as well so concurrent requests
        # aren't held while we render Browse.
        def _render_rows() -> list[str]:
            return [_track_to_didl_item(t, base_url, "music/all") for t in tracks]
        rendered = await asyncio.to_thread(_render_rows)
        for row in rendered:
            didl_parts.append(row)
            returned += 1
    elif obj_id == "music/albums":
        # One container per album → ``al:<base64(album)>``.
        store = get_store()
        rows = await asyncio.to_thread(store.aggregate_albums)
        total = len(rows)
        for d in rows[start:start + count]:
            name = d.get("album") or ""
            didl_parts.append(_container(
                "al:" + _enc_name(name), "music/albums",
                name or "[Unknown Album]", int(d.get("count", 0) or 0)))
            returned += 1
    elif obj_id == "music/artists":
        # One container per album-artist → ``ar:<base64(album_artist)>``.
        store = get_store()
        rows = await asyncio.to_thread(store.aggregate_album_artists)
        total = len(rows)
        for d in rows[start:start + count]:
            name = d.get("album_artist") or ""
            didl_parts.append(_container(
                "ar:" + _enc_name(name), "music/artists",
                name or "[No Album Artist]", int(d.get("count", 0) or 0)))
            returned += 1
    elif obj_id.startswith("ar:"):
        # Artist → that album-artist's ALBUMS (containers ``aa:<artist>:<album>``).
        store = get_store()
        artist = _dec_name(obj_id[3:])
        rows = await asyncio.to_thread(store.aggregate_albums, None, artist)
        total = len(rows)
        for d in rows[start:start + count]:
            album = d.get("album") or ""
            didl_parts.append(_container(
                "aa:" + _enc_name(artist) + ":" + _enc_name(album), obj_id,
                album or "[Unknown Album]", int(d.get("count", 0) or 0)))
            returned += 1
    elif obj_id.startswith("aa:"):
        # Artist + album → tracks (disambiguated, so same-named albums by
        # different artists don't bleed together).
        store = get_store()
        try:
            _, b_artist, b_album = obj_id.split(":", 2)
        except ValueError:
            b_artist = b_album = ""
        artist, album = _dec_name(b_artist), _dec_name(b_album)
        agg = await asyncio.to_thread(store.aggregate_albums, None, artist)
        total = next((int(d.get("count", 0) or 0)
                      for d in agg if (d.get("album") or "") == album), 0)
        for row in await _list_track_items(
                store, {"album_artist": artist, "album": album},
                start, count, base_url, obj_id):
            didl_parts.append(row)
            returned += 1
        if total == 0:
            total = start + returned
    elif obj_id.startswith("al:"):
        # Album (top level) → tracks, by album name only.
        store = get_store()
        album = _dec_name(obj_id[3:])
        agg = await asyncio.to_thread(store.aggregate_albums)
        total = next((int(d.get("count", 0) or 0)
                      for d in agg if (d.get("album") or "") == album), 0)
        for row in await _list_track_items(
                store, {"album": album}, start, count, base_url, obj_id):
            didl_parts.append(row)
            returned += 1
        if total == 0:
            total = start + returned
    else:
        # Unknown obj_id — empty
        total = 0

    didl_parts.append("</DIDL-Lite>")
    result = "".join(didl_parts)

    return {
        "Result":         result,
        "NumberReturned": str(returned),
        "TotalMatches":   str(total),
        "UpdateID":       str(_system_update_id),
    }


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
