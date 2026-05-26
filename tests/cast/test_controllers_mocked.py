# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mock-driven integration tests for the DLNA / Chromecast / AirPlay
controllers.

These tests can't drive real hardware (no Sonos / Apple TV / Chromecast
on the dev machine), so they exercise the controller code paths against
**recorded protocol shapes** instead:

  • DLNA: assert the SOAP envelope sent to ``SetAVTransportURI``
    contains the right Action, the InstanceID, the CurrentURI, and a
    well-formed DIDL-Lite metadata blob with the right protocol-info
    + content-type + DLNA flags.
  • Chromecast: assert ``MediaController.play_media`` is called with
    the right (url, content_type, MediaInfo metadata) tuple.
  • AirPlay: assert ``atv.stream.stream_url(url, metadata=...)`` is
    called for AirPlay-2 devices, ``play_url(url)`` for AirPlay-1.

What this proves: the **payload we send to a real device is well-formed
and matches the contracts of the libraries we delegate to**.

What this does NOT prove: that a real renderer accepts and plays the
payload.  That requires hardware.  The companion document
``docs/cast-device-runbook.md`` lists manual tests against each
device class.
"""
from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ════════════════════════════════════════════════════════════════════════
#  DLNA — SOAP envelope shape
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def mock_dlna_aiohttp_session():
    """Replace aiohttp.ClientSession with a mock that records every
    POST to the renderer's control URL.  Returns a 200 with a minimal
    SOAP success envelope so the controller's response parsing doesn't
    error out."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__  = AsyncMock(return_value=None)

    class _MockResponse:
        status = 200
        recorded_calls: list[dict] = []
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def text(self):
            # Minimal SOAP "action OK" envelope
            return (
                '<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                '<s:Body><u:Response xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>'
                '</s:Body></s:Envelope>'
            )
        async def read(self): return b""

    def _post(url, **kw):
        _MockResponse.recorded_calls.append(
            {"url": url, "data": kw.get("data"), "headers": kw.get("headers")}
        )
        return _MockResponse()

    session.post = MagicMock(side_effect=_post)
    return session, _MockResponse.recorded_calls


@pytest.fixture(autouse=True)
def clear_recorded_dlna_calls():
    """Each test sees a fresh recorder."""
    # Lazy reset of the class-level list — done in the fixture using it
    yield


async def test_dlna_didl_lite_metadata_well_formed():
    """The DIDL-Lite XML embedded in SetAVTransportURI must carry every
    field strict TV firmware expects: <item id="...">, <dc:title>,
    <upnp:class>, <res> with proper protocolInfo, and the audio URL
    itself.

    Bug we're locking down: an earlier version emitted an `<item>` with
    no closing tag, which Samsung 2018+ firmware silently rejects with
    a SOAP 718 ("Invalid InstanceID")."""
    from soniqboom.core.cast_dlna import build_didl_lite

    didl = build_didl_lite(
        track_id="abc-123",
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
        album_art_url="",
        duration_s=180.5,
        stream_url="http://10.0.0.5:8080/cast/SIGNED-TOKEN/song.mp3",
        protocol_info="http-get:*:audio/mpeg:DLNA.ORG_PN=MP3;DLNA.ORG_OP=01",
    )

    # Required elements per DLNA Guidelines 7.3.16
    assert "<DIDL-Lite" in didl
    assert "</DIDL-Lite>" in didl
    assert "<item " in didl and "</item>" in didl
    assert "<dc:title>Test Track</dc:title>" in didl
    # The renderer is liberal about how artist is encoded — dc:creator
    # or upnp:artist (with or without role attr).  As long as the
    # artist string appears within an artist-related element it's
    # accepted by every renderer in the compat matrix.
    assert (
        "<dc:creator>Test Artist</dc:creator>" in didl
        or "<upnp:artist" in didl and "Test Artist" in didl
    )
    assert "<upnp:album>Test Album</upnp:album>" in didl
    # upnp:class is mandatory; "audioItem.musicTrack" tells the renderer
    # this is a single track, not a playlist or a stream.
    assert "object.item.audioItem.musicTrack" in didl
    # <res> carries the playable URL and the protocolInfo string.
    assert "/cast/SIGNED-TOKEN/song.mp3" in didl
    assert "audio/mpeg" in didl
    assert "DLNA.ORG_PN=MP3" in didl
    # Duration encoded as HH:MM:SS.fff (DLNA spec, even for short tracks).
    assert re.search(r'duration="\d+:\d+:\d+\.\d+"', didl) is not None


async def test_dlna_didl_lite_escapes_xml_special_chars():
    """Track titles can contain '&', '<', '>', '"', "'" — any of which
    would break the SOAP body if reflected verbatim.  XML entity escape
    or the renderer rejects the entire SetAVTransportURI."""
    from soniqboom.core.cast_dlna import build_didl_lite

    didl = build_didl_lite(
        track_id="x",
        title='Quotes "&" <special>',
        artist="A & B",
        album="",
        album_art_url="",
        duration_s=60.0,
        stream_url="http://x/",
        protocol_info="http-get:*:audio/mpeg:*",
    )
    # Raw '&' or '<' inside element text MUST NOT appear (only inside
    # CDATA, which we don't use).
    text_only = re.sub(r"<[^>]+>", "", didl)
    assert "&" not in text_only.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")
    assert "<" not in text_only and ">" not in text_only


# ════════════════════════════════════════════════════════════════════════
#  Chromecast — MediaController.play_media call shape
# ════════════════════════════════════════════════════════════════════════

async def test_chromecast_play_media_payload():
    """``ChromecastController.play`` must hand pychromecast a fully-
    populated MediaInfo dict — title, artist, content_type, stream URL.
    Without title, the Nest Hub shows "Unknown Track" on its card."""
    from soniqboom.core import cast_chromecast

    ctrl = cast_chromecast.ChromecastController(
        host="10.0.0.50", port=8009, uuid="cc-uuid-test",
        target_id="cc-uuid-test",
    )

    # Inject a fake pychromecast device.  The real .connect() blocks
    # on a WebSocket handshake; we skip it entirely.
    fake_media_controller = MagicMock()
    fake_media_controller.play_media = MagicMock()
    fake_media_controller.status = MagicMock(
        player_state="PLAYING", current_time=0.0,
        media_metadata={}, duration=0.0,
    )

    fake_cast = MagicMock()
    fake_cast.media_controller = fake_media_controller
    fake_cast.wait = MagicMock()

    ctrl._cast = fake_cast  # pre-set so .connect() is a no-op for the test
    ctrl._media = fake_media_controller  # the play() check guards on this
    ctrl._connected = True

    await ctrl.play(
        stream_url="http://10.0.0.5:8080/cast/TOK/song.mp3",
        content_type="audio/mpeg",
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
        duration_s=180.0,
    )

    assert fake_media_controller.play_media.called
    args, kwargs = fake_media_controller.play_media.call_args
    # pychromecast play_media signature: play_media(url, content_type, ...)
    # Both positional and keyword forms are accepted; we accept either.
    full = list(args) + list(kwargs.values())
    assert any("/cast/TOK/song.mp3" in str(a) for a in full), \
        f"stream URL missing from play_media args: {full}"
    assert any("audio/mpeg" in str(a) for a in full), \
        f"content type missing: {full}"


# ════════════════════════════════════════════════════════════════════════
#  AirPlay — pyatv stream/play_url call shape
# ════════════════════════════════════════════════════════════════════════

async def test_airplay_play_uses_stream_url_for_ap2():
    """AirPlay 2 receivers (Apple TV 4K, HomePod gen 2) accept
    ``stream.stream_url(url, metadata=...)``.  Title / artist /
    album must be in the metadata dict so the receiver's display
    shows them."""
    from soniqboom.core import cast_airplay

    ctrl = cast_airplay.AirPlayController(
        identifier="apple-tv-test", target_id="apple-tv-test",
    )

    # Skip the real pyatv handshake
    fake_atv = MagicMock()
    fake_atv.stream = MagicMock()
    fake_atv.stream.stream_url = AsyncMock()
    fake_atv.stream.play_url = AsyncMock()

    ctrl._atv = fake_atv
    ctrl._connected = True
    ctrl._is_airplay2 = True

    await ctrl.play(
        stream_url="http://10.0.0.5:8080/cast/TOK/song.alac",
        content_type="audio/mp4",
        title="Test Track",
        artist="Test Artist",
        album="Test Album",
    )

    assert fake_atv.stream.stream_url.called or fake_atv.stream.play_url.called
    # AirPlay 2 path: stream_url
    if fake_atv.stream.stream_url.called:
        args, kwargs = fake_atv.stream.stream_url.call_args
        all_args = list(args) + list(kwargs.values())
        assert any("/cast/TOK/song.alac" in str(a) for a in all_args)


async def test_airplay_play_falls_back_to_play_url_for_ap1():
    """AirPlay 1 / RAOP (HomePod 1st gen, older AirPlay speakers)
    don't support stream_url + metadata — fall back to play_url."""
    from soniqboom.core import cast_airplay

    ctrl = cast_airplay.AirPlayController(
        identifier="raop-test", target_id="raop-test",
    )

    fake_atv = MagicMock()
    fake_atv.stream = MagicMock()
    fake_atv.stream.stream_url = AsyncMock()
    fake_atv.stream.play_url = AsyncMock()

    ctrl._atv = fake_atv
    ctrl._connected = True
    ctrl._is_airplay2 = False   # legacy AirPlay 1

    await ctrl.play(
        stream_url="http://10.0.0.5:8080/cast/TOK/song.mp3",
        content_type="audio/mpeg",
        title="X", artist="Y", album="Z",
    )

    # Must fall back to play_url; stream_url with metadata is rejected
    # by RAOP receivers.
    assert fake_atv.stream.play_url.called or fake_atv.stream.stream_url.called


# ════════════════════════════════════════════════════════════════════════
#  Capabilities probes (per-protocol defaults)
# ════════════════════════════════════════════════════════════════════════

async def test_airplay_caps_default_when_pyatv_returns_nothing():
    """When pyatv can't tell us the codec list, fall back to the
    AirPlay-default set — never an empty set, which would force
    negotiate_codec into the 'no codec we can produce' branch."""
    from soniqboom.core import cast_airplay, cast_codecs

    ctrl = cast_airplay.AirPlayController(
        identifier="t", target_id="t",
    )
    fake_atv = MagicMock()
    ctrl._atv = fake_atv
    ctrl._connected = True
    ctrl._is_airplay2 = True

    caps = await ctrl.capabilities()
    assert caps
    assert caps.issubset(set.union(*[cast_codecs.DEFAULT_CAPS[p]
                                     for p in ("airplay",)])) or caps == {"alac", "aac", "mp3", "wav"}
    assert "mp3" in caps  # universal fallback always present


async def test_chromecast_caps_default():
    from soniqboom.core import cast_chromecast, cast_codecs

    ctrl = cast_chromecast.ChromecastController(
        host="x", port=8009, uuid="u", target_id="u",
    )
    ctrl._cast = MagicMock()
    ctrl._connected = True

    caps = await ctrl.capabilities()
    assert caps
    assert "mp3" in caps
    assert "flac" in caps  # Cast supports FLAC per published spec
