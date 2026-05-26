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
  POST /dlna/cds/control    - ContentDirectory:1 SOAP control (Browse)
  POST /dlna/cm/control     - ConnectionManager:1 SOAP control (minimal)
  GET  /dlna/cds/event      - eventing endpoint stub (no GENA subscriptions)

The router mounts at the app root (NOT under /api/) so the URLs
exactly match what the SSDP LOCATION header advertises, and so the
session-cookie auth middleware leaves them anonymous.  DLNA traffic
is intrinsically LAN-only and has no concept of authenticated sessions
— controllers expect the device description to be reachable without
credentials.

Browse hierarchy:

    0                           (root container)
    └── music                   (audio container)
        ├── all                 (every track in flat list)
        ├── albums              (one container per album)
        │   └── al:<sha1>       (tracks of one album)
        └── artists             (one container per album_artist)
            └── ar:<sha1>       (tracks of one artist)

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
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType><allowedValueList><allowedValue>BrowseMetadata</allowedValue><allowedValue>BrowseDirectChildren</allowedValue></allowedValueList></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
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


def _track_to_didl_item(track: dict, base_url: str) -> str:
    """Render a single track row as a DIDL-Lite ``<item>``.

    The ``<res>`` URL points at the existing anonymous cast byte server
    with a fresh signed token.  Tokens are user-less (no ``uid`` claim)
    so DLNA controllers — which never authenticate to us — get exactly
    the access scope the operator agreed to when they enabled the
    Media Server.
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
        f'<item id="{_xescape(track_id)}" parentID="music" restricted="1">'
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
        # We don't support Search; controllers fall back to client-side filter.
        return _soap_response(_UPNP_CDS_NS, action, {"SearchCaps": ""})

    if action == "GetSortCapabilities":
        # Also empty — we return tracks in library/insertion order.
        return _soap_response(_UPNP_CDS_NS, action, {"SortCaps": ""})

    if action == "GetSystemUpdateID":
        # Bumps when the library changes; we just use a monotonic counter
        # derived from store.track_count() so a rescan flips it visibly.
        try:
            from soniqboom.core.store import get_store
            updid = get_store().track_count()
        except Exception:
            updid = 0
        return _soap_response(_UPNP_CDS_NS, action, {"Id": str(updid)})

    if action == "Browse":
        return await _do_browse(args, request)

    return _soap_fault(401, f"Invalid Action: {action!r}")


async def _do_browse(args: dict[str, str], request: Request) -> FResponse:
    """Implement the Browse action.

    Object-ID hierarchy:
      "0"       → root (single child: music)
      "music"   → music container (all / albums / artists)
      "music/all"     → flat list of every track
      "music/albums"  → list of album containers
      "music/artists" → list of artist containers
      "al:<sha1>"     → tracks of one album
      "ar:<sha1>"     → tracks of one artist

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
            "UpdateID":       "0",
        }
    # Unknown object → empty result
    return {
        "Result":         '<DIDL-Lite ' + _DIDL_NS_BOILERPLATE + '></DIDL-Lite>',
        "NumberReturned": "0",
        "TotalMatches":   "0",
        "UpdateID":       "0",
    }


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
        # Music → All / Albums / Artists
        children = [
            ("music/all",     "All Tracks", get_store().track_count()),
            ("music/albums",  "Albums",     0),  # child count unknown w/o scan
            ("music/artists", "Artists",    0),
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
            return [_track_to_didl_item(t, base_url) for t in tracks]
        rendered = await asyncio.to_thread(_render_rows)
        for row in rendered:
            didl_parts.append(row)
            returned += 1
    elif obj_id == "music/albums" or obj_id == "music/artists":
        # TODO(future): build album / artist container index.  For v1
        # we expose only the flat list under "music/all" — DLNA
        # controllers can still walk it.
        total = 0
    elif obj_id.startswith("al:") or obj_id.startswith("ar:"):
        # Same TODO — not implemented in v1; returns empty so the
        # controller shows "no items" instead of erroring out.
        total = 0
    else:
        # Unknown obj_id — empty
        total = 0

    didl_parts.append("</DIDL-Lite>")
    result = "".join(didl_parts)

    return {
        "Result":         result,
        "NumberReturned": str(returned),
        "TotalMatches":   str(total),
        "UpdateID":       "0",
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


# ── GENA event subscription stubs ──────────────────────────────────────────
# Many DLNA controllers send a SUBSCRIBE request to the event URLs at
# startup.  We don't actually emit events (no library-change push), but
# returning a successful subscription response avoids "device half-broken"
# warnings on strict controllers.

@router.api_route(
    "/dlna/cds/event", methods=["SUBSCRIBE", "UNSUBSCRIBE"], include_in_schema=False,
)
async def cds_event(request: Request):
    return _event_response()


@router.api_route(
    "/dlna/cm/event", methods=["SUBSCRIBE", "UNSUBSCRIBE"], include_in_schema=False,
)
async def cm_event(request: Request):
    return _event_response()


def _event_response() -> PlainTextResponse:
    """Minimal GENA SUBSCRIBE response — accepts the subscription
    but never sends NOTIFYs.  Sufficient for controllers that bail
    when the device 405s on SUBSCRIBE."""
    return PlainTextResponse(
        content="",
        status_code=200,
        headers={
            "Server":  "UPnP/1.0 SoniqBoom-DLNA/1.0",
            "SID":     "uuid:00000000-0000-0000-0000-000000000000",
            "TIMEOUT": "Second-1800",
        },
    )
