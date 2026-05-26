# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DLNA / UPnP-AV media-renderer controller for SoniqBoom.

What is DLNA?
=============

DLNA (Digital Living Network Alliance) is the consumer-friendly
trade-name for a profile of UPnP-AV (Universal Plug and Play - Audio /
Video). In practice it's the protocol your TV, AV receiver, Sonos (in
"line-in" / DLNA mode), Yamaha MusicCast unit, and most "network
speakers" understand for one-way pushed playback: the controller tells
the renderer "fetch this HTTP URL and play it", the renderer streams
it directly from our byte-server.

Three relevant UPnP services live on a media renderer:

* ``AVTransport`` --- Play / Pause / Stop / Seek / GetPositionInfo.
  All control verbs take an ``InstanceID`` (always ``0`` for renderers
  that expose a single playback slot, which is every consumer device
  ever shipped).
* ``ConnectionManager`` --- ``GetProtocolInfo`` returns the Sink
  protocolInfo CSV that tells us which codecs / containers the
  renderer claims to decode. This is how we negotiate without
  blindly transcoding.
* ``RenderingControl`` --- volume / mute. We deliberately don't
  touch this from the music server (volume is a user-facing concern
  that lives with the remote-control surface, not the streamer).

Each service has a "control URL" relative to the description.xml
root; SOAP requests are POSTed to that URL with a SOAPAction header
naming the service+action. The library handles all of that for us.

Why async-upnp-client?
======================

There are three Python UPnP libraries of any maturity:

1. ``async-upnp-client`` --- pure-asyncio, used by Home Assistant
   in production for years. Tracks the spec including the
   irritating little quirks (LastChange event parsing, vendor
   namespace handling, the "DIDL-Lite is sent as escaped XML
   inside a SOAP string parameter" landmine).
2. ``upnpclient`` --- sync only, abandoned, no DIDL helpers.
3. Roll-our-own SOAP --- tempting and tractable (it's just XML
   over HTTP) but the long tail of vendor weirdness (Samsung
   rejecting requests without specific UA strings, Yamaha
   demanding the SOAPAction header double-quoted, LG returning
   non-UTF-8 in track titles) is where weeks vanish. The library
   has already eaten that pain.

We pin async-upnp-client as an *optional* dependency: SoniqBoom
runs fine without DLNA support, the picker just hides DLNA targets.
At import time we capture the ImportError and surface a friendly
``RuntimeError`` from any controller method if a user does try to
hit a DLNA target without the library installed.

Threading / concurrency model
=============================

A renderer is a single-state resource: you can't ``Play`` and
``Seek`` it simultaneously and expect predictable results, and
several budget renderers literally crash when handed overlapping
SOAP requests. We therefore serialise every SOAP exchange behind
a per-controller ``asyncio.Lock``. Each call is additionally
wrapped in ``asyncio.wait_for(..., 10.0)`` so that a renderer
that's been pulled off the LAN doesn't pin a coroutine forever.

The 10-second timeout is empirical: every renderer we've tested
responds within 2s under normal conditions; a value of 10s gives
slow ARM-based gear (older Yamaha receivers, the cheap Sony
"Bluetooth speaker" line) headroom without making a hard hang
feel infinite to the user.
"""
from __future__ import annotations

import asyncio
import logging
from xml.sax.saxutils import escape as xml_escape

from soniqboom.core.cast_codecs import DEFAULT_CAPS, parse_sink_protocol_info

log = logging.getLogger(__name__)


# ── Optional dependency import ────────────────────────────────────────────
#
# async-upnp-client is an OPTIONAL dependency.  If it's missing we
# still want this module to import cleanly so the cast_targets
# discovery layer can carry on advertising the non-DLNA backends.
# Every public method below checks ``_UPNP_AVAILABLE`` and raises a
# friendly RuntimeError if the user tries to actually use DLNA
# control without the library present.
try:
    from async_upnp_client.aiohttp import AiohttpRequester
    from async_upnp_client.client_factory import UpnpFactory

    _UPNP_AVAILABLE = True
except ImportError:  # pragma: no cover -- exercised only when dep is absent
    AiohttpRequester = None  # type: ignore[assignment,misc]
    UpnpFactory = None  # type: ignore[assignment,misc]
    _UPNP_AVAILABLE = False


# ── Constants ─────────────────────────────────────────────────────────────

# Every consumer renderer we've ever tested exposes exactly one
# playback "instance" addressed as InstanceID=0.  The UPnP-AV spec
# permits multi-instance renderers (think: a home-theatre amp with
# Zone-1 / Zone-2 outputs) but none of them implement it via DLNA
# in practice -- zone control is always vendor-private.  Hard-coding
# 0 is the universally-compatible choice.
_INSTANCE_ID = "0"

# Per-call SOAP timeout.  See module docstring for the rationale.
_SOAP_TIMEOUT_S = 10.0

# Standard AVTransport service-type URI strings.  We accept either
# v1 or v2 because cheaper gear shipped with v1 and never updated.
_AVTRANSPORT_TYPES = (
    "urn:schemas-upnp-org:service:AVTransport:1",
    "urn:schemas-upnp-org:service:AVTransport:2",
)
_CONNECTIONMGR_TYPES = (
    "urn:schemas-upnp-org:service:ConnectionManager:1",
    "urn:schemas-upnp-org:service:ConnectionManager:2",
)


# ── DIDL-Lite metadata builder ────────────────────────────────────────────
#
# DIDL-Lite is the XML "menu card" that travels alongside the stream
# URL in SetAVTransportURI's CurrentURIMetaData parameter.  It tells
# the renderer the title, artist, album, cover art URL, duration,
# and a <res> element whose ``protocolInfo`` attribute is the same
# DLNA contentFeatures string we emit on the byte-stream HTTP
# response.  Many renderers refuse to play a stream when the
# metadata is empty (Sonos in particular displays the URL as the
# track name and the artwork stays blank); supplying a complete
# DIDL-Lite document is the difference between "works" and "works
# AND looks good on the renderer's screen".
#
# Reference: UPnP ContentDirectory v1/2 spec, Appendix B (DIDL-Lite
# schema).  Root namespace, dc:, and upnp: namespace URIs are
# normative -- changing them breaks parsing on strict renderers.

_DIDL_NS_ROOT = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
_DIDL_NS_DC = "http://purl.org/dc/elements/1.1/"
_DIDL_NS_UPNP = "urn:schemas-upnp-org:metadata-1-0/upnp/"


def _seconds_to_hms(seconds: float) -> str:
    """Format seconds as ``H:MM:SS.fff`` for DIDL-Lite ``res@duration``.

    DIDL-Lite uses an unpadded leading-hours field with fractional
    seconds to millisecond precision; this is *different* from the
    AVTransport.Seek HH:MM:SS form (which pads hours to 2 digits and
    omits fractional seconds on many renderers).
    """
    if seconds is None or seconds <= 0:
        return "0:00:00.000"
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _seconds_to_hms_seek(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS.mmm`` for AVTransport.Seek REL_TIME.

    The Seek action uses a stricter zero-padded format than DIDL-Lite
    durations.  Most renderers also accept the no-fractional form
    ``HH:MM:SS`` but the spec permits milliseconds and Sonos honours
    them, so we include them for sub-second-accurate scrubbing.
    """
    if seconds is None or seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _hms_to_seconds(hms: str) -> float:
    """Parse ``HH:MM:SS[.fff]`` (or ``H:MM:SS``) back to float seconds.

    Tolerant of:

    * Leading whitespace.
    * ``NOT_IMPLEMENTED`` (returned by some renderers when no track
      is loaded) --> 0.0.
    * ``00:00:00`` --> 0.0 (per spec contract).
    * Missing fractional component.

    Returns 0.0 on any parse failure -- callers treat 0.0 as
    "unknown" already.
    """
    if not hms:
        return 0.0
    s = hms.strip()
    if not s or s.upper() == "NOT_IMPLEMENTED":
        return 0.0
    try:
        parts = s.split(":")
        if len(parts) != 3:
            return 0.0
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600.0 + minutes * 60.0 + seconds
    except (ValueError, TypeError):
        return 0.0


def build_didl_lite(
    *,
    track_id: str,
    title: str,
    artist: str = "",
    album: str = "",
    album_art_url: str = "",
    duration_s: float = 0.0,
    stream_url: str,
    protocol_info: str,
) -> str:
    """Build a complete DIDL-Lite XML document for ``CurrentURIMetaData``.

    All user-supplied text fields are XML-escaped via
    :func:`xml.sax.saxutils.escape` -- a track title containing an
    ampersand would otherwise produce malformed XML and the renderer
    would silently refuse to play.

    The ``<upnp:class>`` value ``object.item.audioItem.musicTrack``
    is the canonical class for a single audio track; using a vaguer
    class (``object.item.audioItem``) loses the artist/album fields
    on stricter renderers like older Samsung TVs.
    """
    safe_id = xml_escape(track_id or "0")
    safe_title = xml_escape(title or "")
    safe_artist = xml_escape(artist or "")
    safe_album = xml_escape(album or "")
    safe_art = xml_escape(album_art_url or "")
    safe_url = xml_escape(stream_url or "")
    safe_proto = xml_escape(protocol_info or "")

    duration_attr = _seconds_to_hms(duration_s)

    # Optional sub-elements -- only emitted when populated.  Empty
    # <dc:creator/> tags are legal but some renderers display the
    # placeholder text "Unknown" / blank box; omitting them entirely
    # gives the renderer freedom to suppress the field.
    bits: list[str] = []
    bits.append(
        f'<DIDL-Lite xmlns="{_DIDL_NS_ROOT}" '
        f'xmlns:dc="{_DIDL_NS_DC}" '
        f'xmlns:upnp="{_DIDL_NS_UPNP}">'
    )
    bits.append(
        f'<item id="{safe_id}" parentID="0" restricted="1">'
    )
    bits.append(f"<dc:title>{safe_title}</dc:title>")
    if safe_artist:
        # Both forms emitted -- dc:creator is the DIDL-Lite-standard
        # field; upnp:artist is the structured form many renderers
        # actually display.  Including both costs nothing.
        bits.append(f"<dc:creator>{safe_artist}</dc:creator>")
        bits.append(f'<upnp:artist role="AlbumArtist">{safe_artist}</upnp:artist>')
    if safe_album:
        bits.append(f"<upnp:album>{safe_album}</upnp:album>")
    if safe_art:
        # The dlna:profileID attribute is technically required by
        # spec but every renderer we've tested accepts a bare URI.
        bits.append(f"<upnp:albumArtURI>{safe_art}</upnp:albumArtURI>")
    bits.append("<upnp:class>object.item.audioItem.musicTrack</upnp:class>")
    bits.append(
        f'<res protocolInfo="{safe_proto}" duration="{duration_attr}">'
        f"{safe_url}</res>"
    )
    bits.append("</item>")
    bits.append("</DIDL-Lite>")
    return "".join(bits)


# ── Controller class ──────────────────────────────────────────────────────


class DLNAController:
    """Drive a single DLNA media renderer via UPnP-AV SOAP actions.

    Lifecycle:

    1. ``ctrl = DLNAController(description_url=..., target_id=...)``
    2. ``await ctrl.connect()``      -- fetches description.xml,
       locates AVTransport + ConnectionManager.  Idempotent: calling
       again is a no-op.
    3. ``await ctrl.capabilities()`` -- probe codec support.
    4. ``await ctrl.play(stream_url=..., didl_metadata=...)``
       then ``pause`` / ``resume`` / ``seek`` / ``position`` /
       ``stop`` as the user / pipeline drives.
    5. ``await ctrl.disconnect()``   -- release the aiohttp session.

    Every SOAP-issuing method serialises behind ``self._lock`` so
    multiple concurrent calls (e.g. the UI polls ``position`` while
    a track-change is mid-flight) don't corrupt the renderer's
    state machine.
    """

    def __init__(self, *, description_url: str, target_id: str):
        if not description_url:
            raise ValueError("description_url is required")
        self.description_url: str = description_url
        self.target_id: str = target_id

        # Per-controller lock around SOAP calls -- see class docstring.
        self._lock: asyncio.Lock = asyncio.Lock()

        # Populated by connect().  None until then; checked by every
        # SOAP method via _require_connected().
        self._device = None  # async_upnp_client UpnpDevice
        self._avtransport = None  # AVTransport service handle
        self._connmgr = None  # ConnectionManager service handle
        self._requester = None  # AiohttpRequester (owns the session)
        self._connected: bool = False

    # ── helpers ───────────────────────────────────────────────────────

    def _check_available(self) -> None:
        """Raise RuntimeError if async-upnp-client isn't installed."""
        if not _UPNP_AVAILABLE:
            raise RuntimeError("async-upnp-client not installed")

    def _require_connected(self) -> None:
        if not self._connected or self._avtransport is None:
            raise RuntimeError(
                "DLNAController not connected; call await .connect() first"
            )

    @staticmethod
    def _find_service(device, type_uris: tuple[str, ...]):
        """Locate a service on a device by trying spec versions in order.

        Renderers advertise either v1 or v2; we prefer whichever is
        present and fall back gracefully.  Returns ``None`` if the
        device doesn't expose the service at all -- which is legal
        per spec (a renderer without ConnectionManager simply can't
        be probed for codec support, and we fall back to defaults).
        """
        for uri in type_uris:
            try:
                if device.has_service(uri):
                    return device.service(uri)
            except Exception:
                continue
        return None

    async def _invoke(self, service, action_name: str, **kwargs):
        """Call a UPnP action under the lock + timeout, log the result.

        Returns whatever the library returns (a dict of out-params)
        on success.  On SOAP error or timeout we log at WARNING and
        re-raise so the caller can decide how to surface it.
        """
        action = service.action(action_name)
        async with self._lock:
            try:
                result = await asyncio.wait_for(
                    action.async_call(**kwargs),
                    timeout=_SOAP_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "DLNA %s.%s timed out after %ss (renderer %s)",
                    service.service_type,
                    action_name,
                    _SOAP_TIMEOUT_S,
                    self.target_id,
                )
                raise
            except Exception as exc:
                # async-upnp-client raises UpnpError subclasses; we log
                # the action + the exception text and re-raise so the
                # caller's higher-level handler decides whether to
                # retry, fail loud, or fall back.
                log.warning(
                    "DLNA %s.%s failed on %s: %s",
                    service.service_type,
                    action_name,
                    self.target_id,
                    exc,
                )
                raise
            log.debug(
                "DLNA %s.%s -> ok (renderer %s)",
                service.service_type,
                action_name,
                self.target_id,
            )
            return result

    # ── public API ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Fetch description.xml and locate the required services.

        Idempotent -- repeated calls return immediately once the
        device is bound.  The aiohttp session inside the requester
        is created lazily on first SOAP call and lives until
        :meth:`disconnect`.
        """
        if self._connected:
            return
        self._check_available()

        # AiohttpRequester wraps an aiohttp.ClientSession; the
        # default keepalive behaviour is fine for our usage (one
        # renderer, low call rate).
        self._requester = AiohttpRequester(timeout=int(_SOAP_TIMEOUT_S))
        factory = UpnpFactory(self._requester)
        # async_create_device performs the HTTP GET of the
        # description.xml and parses out the service list.
        self._device = await asyncio.wait_for(
            factory.async_create_device(self.description_url),
            timeout=_SOAP_TIMEOUT_S,
        )

        self._avtransport = self._find_service(self._device, _AVTRANSPORT_TYPES)
        if self._avtransport is None:
            # No AVTransport means it isn't a media renderer we can
            # control.  Surface this clearly rather than failing
            # later inside .play() with a cryptic AttributeError.
            raise RuntimeError(
                f"Device at {self.description_url} exposes no AVTransport service"
            )
        self._connmgr = self._find_service(self._device, _CONNECTIONMGR_TYPES)

        self._connected = True
        log.info(
            "Connected to DLNA renderer %s (%s) at %s",
            getattr(self._device, "friendly_name", self.target_id),
            self.target_id,
            self.description_url,
        )

    async def capabilities(self) -> set[str]:
        """Probe the renderer's supported codecs via GetProtocolInfo.

        Returns a set like ``{"mp3", "flac", "wav"}``.  Falls back to
        ``DEFAULT_CAPS['dlna']`` when:

        * The device doesn't expose ConnectionManager.
        * GetProtocolInfo errors out (Samsung 2014-era firmware
          sometimes returns SOAP 501).
        * The Sink CSV parses to an empty set (the renderer is
          mis-reporting -- defaulting is safer than refusing to
          play anything).
        """
        self._check_available()
        self._require_connected()

        if self._connmgr is None:
            log.debug(
                "Renderer %s has no ConnectionManager; using default caps",
                self.target_id,
            )
            return set(DEFAULT_CAPS["dlna"])

        try:
            result = await self._invoke(self._connmgr, "GetProtocolInfo")
        except Exception:
            # _invoke already logged at WARNING; treat as soft failure.
            return set(DEFAULT_CAPS["dlna"])

        sink = ""
        if isinstance(result, dict):
            sink = str(result.get("Sink") or "")
        caps = parse_sink_protocol_info(sink)
        if not caps:
            log.debug(
                "Renderer %s returned no recognisable Sink codecs; using defaults",
                self.target_id,
            )
            return set(DEFAULT_CAPS["dlna"])
        return caps

    async def play(self, *, stream_url: str, didl_metadata: str = "") -> None:
        """Set the URI then start playback.

        Two SOAP calls, strictly sequential: many renderers won't
        accept a Play before SetAVTransportURI has fully returned,
        and a few (Yamaha RX-V series) crash when the second call
        arrives during the first's response phase.  The shared
        ``_lock`` already enforces this serialisation across
        concurrent callers; here we just issue them in order.

        ``didl_metadata`` may be the empty string -- most renderers
        accept that and synthesise a placeholder.  Pass the output
        of :func:`build_didl_lite` for full per-track display.
        """
        self._check_available()
        self._require_connected()
        if not stream_url:
            raise ValueError("stream_url is required")

        await self._invoke(
            self._avtransport,
            "SetAVTransportURI",
            InstanceID=int(_INSTANCE_ID),
            CurrentURI=stream_url,
            CurrentURIMetaData=didl_metadata or "",
        )
        # Speed='1' = play at 1x.  The spec permits other rates but
        # only a handful of high-end renderers actually implement
        # them; '1' is the universal "go".
        await self._invoke(
            self._avtransport,
            "Play",
            InstanceID=int(_INSTANCE_ID),
            Speed="1",
        )

    async def pause(self) -> None:
        """Pause playback.  No-op on renderers that don't expose Pause
        (we let the SOAP error propagate -- the caller decides whether
        to surface it)."""
        self._check_available()
        self._require_connected()
        await self._invoke(
            self._avtransport,
            "Pause",
            InstanceID=int(_INSTANCE_ID),
        )

    async def resume(self) -> None:
        """Resume from a paused state by re-issuing Play at speed 1."""
        self._check_available()
        self._require_connected()
        await self._invoke(
            self._avtransport,
            "Play",
            InstanceID=int(_INSTANCE_ID),
            Speed="1",
        )

    async def stop(self) -> None:
        """Stop playback and clear the renderer's transport URI."""
        self._check_available()
        self._require_connected()
        await self._invoke(
            self._avtransport,
            "Stop",
            InstanceID=int(_INSTANCE_ID),
        )

    async def seek(self, *, seconds: float) -> None:
        """Seek to ``seconds`` from the start of the current track.

        Uses ``Unit='REL_TIME'`` with an ``HH:MM:SS.mmm`` target.
        The other defined unit, ``ABS_TIME``, is meant for live /
        broadcast content and most music renderers don't implement
        it -- REL_TIME is the safe choice for file playback.

        Negative ``seconds`` are clamped to 0 because the spec
        forbids negative REL_TIME values; passing -1 to "go to
        start" is a common caller bug that we silently absorb.
        """
        self._check_available()
        self._require_connected()
        target = _seconds_to_hms_seek(max(0.0, float(seconds)))
        await self._invoke(
            self._avtransport,
            "Seek",
            InstanceID=int(_INSTANCE_ID),
            Unit="REL_TIME",
            Target=target,
        )

    async def position(self) -> dict:
        """Return current playback position.

        Output dict keys:

        * ``track_uri``  -- the URI currently loaded, or "".
        * ``duration_s`` -- total track length in seconds (float).
        * ``position_s`` -- current playhead in seconds (float).
        * ``rel_time``   -- raw HH:MM:SS string from the renderer
                            (useful for debugging when the renderer
                            is creative with its time formats).

        Returns ``{}`` on any error -- callers treat that as "I
        don't know, try again later" rather than a hard failure.
        """
        self._check_available()
        self._require_connected()
        try:
            result = await self._invoke(
                self._avtransport,
                "GetPositionInfo",
                InstanceID=int(_INSTANCE_ID),
            )
        except Exception:
            return {}

        if not isinstance(result, dict):
            return {}

        track_uri = str(result.get("TrackURI") or "")
        # TrackDuration / RelTime are 'HH:MM:SS' or 'NOT_IMPLEMENTED'.
        duration_raw = str(result.get("TrackDuration") or "")
        rel_time = str(result.get("RelTime") or "")
        return {
            "track_uri": track_uri,
            "duration_s": _hms_to_seconds(duration_raw),
            "position_s": _hms_to_seconds(rel_time),
            "rel_time": rel_time,
        }

    async def disconnect(self) -> None:
        """Close the aiohttp session and forget the device.

        Idempotent.  Safe to call from finally-blocks regardless of
        whether :meth:`connect` ever succeeded.
        """
        if self._requester is not None:
            # AiohttpRequester owns its session; closing it returns
            # the connector to the pool and stops any keepalive
            # background tasks.  The method is named ``async_close``
            # in current async-upnp-client; some older versions used
            # ``close`` -- accommodate both.
            close = getattr(self._requester, "async_close", None) or getattr(
                self._requester, "close", None
            )
            if close is not None:
                try:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    # Disconnect must never raise -- log and move on.
                    log.debug(
                        "Ignoring error while closing DLNA requester for %s",
                        self.target_id,
                        exc_info=True,
                    )
        self._requester = None
        self._device = None
        self._avtransport = None
        self._connmgr = None
        self._connected = False
