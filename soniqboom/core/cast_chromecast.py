# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Google Cast (Chromecast) media controller.

Wraps ``pychromecast`` — the canonical Python implementation of the
Cast v2 protocol — behind an async, lock-guarded facade so SoniqBoom's
FastAPI request handlers can drive a Cast device without blocking the
event loop.

Cast protocol primer (just enough to read this file):

* A Cast device exposes a TLS socket on port 8009.  The host opens a
  long-lived connection and exchanges protobuf-wrapped JSON over it.
* The receiver runs *applications* (Default Media Receiver, YouTube,
  Spotify, ...).  We always target the Default Media Receiver
  (app id ``CC1AD845``) — it ships the ``urn:x-cast:com.google.cast.media``
  channel, which handles ``LOAD`` / ``PLAY`` / ``PAUSE`` / ``QUEUE_*``.
* The receiver speaks an extensive media protocol — including a
  multi-track *queue* for gapless playback.  pychromecast's
  ``MediaController.play_media`` covers single-track LOAD; the queue
  ops are not first-class API yet, so we send raw messages on the
  media channel.

Why async wrappers around a sync library:

* ``pychromecast`` blocks the calling thread on every send/recv — even
  ``play_media`` waits for the receiver's status broadcast before
  returning.  Running that on the FastAPI event loop would stall every
  other request for hundreds of ms.
* We funnel all calls through ``asyncio.to_thread`` and serialise them
  on a per-controller ``asyncio.Lock`` — the underlying ``Chromecast``
  object is not thread-safe, so two concurrent ``play_media`` calls
  against the same device would corrupt its socket state.
* Every blocking call is also wrapped in ``asyncio.wait_for`` with a
  10 s budget.  Cast devices that go AWOL (WiFi dropout, power-cycle)
  otherwise hang the await forever.

Capabilities are static per Google's published Cast media spec — there
is no runtime probe — so ``capabilities()`` returns a hardcoded set
matched to ``cast_codecs.DEFAULT_CAPS['cast']``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


# ── Optional dependency import ────────────────────────────────────────────
#
# pychromecast is optional — installs only when the user wants Cast
# output.  All public methods raise RuntimeError if it's missing, so
# import-time failure is silent (the rest of SoniqBoom still works).

try:
    import pychromecast  # type: ignore[import-not-found]
    from pychromecast.controllers.media import (  # type: ignore[import-not-found]
        MediaController,
    )
    _PYCHROMECAST_AVAILABLE = True
except ImportError:
    pychromecast = None  # type: ignore[assignment]
    MediaController = None  # type: ignore[assignment,misc]
    _PYCHROMECAST_AVAILABLE = False


# ── Constants ─────────────────────────────────────────────────────────────

# Cast-device static codec support.  This matches what the Default
# Media Receiver decodes natively per
# https://developers.google.com/cast/docs/media — there is no runtime
# capability probe in the Cast protocol, so we trust Google's spec.
# Kept in lockstep with cast_codecs.DEFAULT_CAPS['cast'].
_CAST_CAPABILITIES: frozenset[str] = frozenset(
    {"mp3", "aac", "flac", "ogg", "opus", "wav"}
)

# Per-command timeout for the blocking pychromecast call.  10 s is
# generous; a healthy device responds in <500 ms.  Anything beyond
# that is almost certainly a disconnected / sleeping device and we'd
# rather fail fast than hang the request handler.
_COMMAND_TIMEOUT_S: float = 10.0

# Cast queue-message type strings — these are NOT exposed as named
# constants by pychromecast.  Spec source:
# https://developers.google.com/cast/docs/reference/web_receiver/cast.framework.messages.QueueLoadRequestData
_QUEUE_LOAD: str = "QUEUE_LOAD"
_QUEUE_INSERT: str = "QUEUE_INSERT"
_QUEUE_UPDATE: str = "QUEUE_UPDATE"

# metadataType for the receiver's MediaMetadata object.
# 0 = Generic, 1 = Movie, 2 = TV show, 3 = Music track, 4 = Photo.
# We always send 3 — every track SoniqBoom plays is music.
# https://developers.google.com/cast/docs/reference/web_receiver/cast.framework.messages.MusicTrackMediaMetadata
_METADATA_TYPE_MUSIC: int = 3


# ── Helpers ──────────────────────────────────────────────────────────────

def _build_music_metadata(
    *,
    title: str,
    artist: str,
    album: str,
    album_art_url: str,
) -> dict[str, Any]:
    """Build the receiver-side ``metadata`` dict for a music track.

    Shape is dictated by the CAF (Cast Application Framework) message
    schema for ``MusicTrackMediaMetadata``: see the Google reference
    linked in the module docstring.  ``images`` must be a list of
    ``{'url': ...}`` dicts; omitting an empty list is fine but we
    always send the key for shape consistency.
    """
    meta: dict[str, Any] = {
        "metadataType": _METADATA_TYPE_MUSIC,
        "title": title,
        "artist": artist,
        "albumName": album,
        "images": [{"url": album_art_url}] if album_art_url else [],
    }
    return meta


def _build_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a SoniqBoom queue-item dict into a Cast ``QueueItem``
    structure.

    The Cast receiver expects each queue item to wrap a full
    ``MediaInformation`` object (the same shape ``play_media`` builds
    internally).  Pulling this out into a helper keeps queue_load /
    queue_insert in lockstep.
    """
    stream_url = item.get("stream_url", "")
    content_type = item.get("content_type", "")
    duration_s = float(item.get("duration_s", 0.0) or 0.0)

    media_info: dict[str, Any] = {
        "contentId": stream_url,
        "contentType": content_type,
        # streamType BUFFERED = seekable file (the default).  LIVE
        # would disable the progress bar on the receiver UI; we always
        # serve seekable streams via cast_pipe / the byte-server.
        "streamType": "BUFFERED",
        "metadata": _build_music_metadata(
            title=item.get("title", ""),
            artist=item.get("artist", ""),
            album=item.get("album", ""),
            album_art_url=item.get("album_art_url", ""),
        ),
    }
    if duration_s > 0:
        media_info["duration"] = duration_s

    return {
        "media": media_info,
        # autoplay per-item controls whether the receiver starts on
        # that track when it becomes current.  We always want True —
        # if the user wants to pause, they call pause() afterward.
        "autoplay": True,
        # preloadTime: how many seconds before the current track ends
        # the receiver should start buffering the next.  10 s matches
        # Cast's default and is enough for gapless on a healthy LAN.
        "preloadTime": 10,
    }


# ── Controller ────────────────────────────────────────────────────────────

class ChromecastController:
    """Async-safe controller for a single Google Cast device.

    Instances are not shareable across event loops or processes — the
    underlying ``pychromecast.Chromecast`` holds a TLS socket and an
    internal worker thread.  Create one per (target, request_loop)
    pair and call ``disconnect()`` when done.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 8009,
        uuid: str | None = None,
        target_id: str,
    ) -> None:
        self._host = host
        self._port = port
        self._uuid = uuid
        self._target_id = target_id

        # Serialise every blocking pychromecast call.  The Chromecast
        # object is not thread-safe; without this, two concurrent
        # requests would corrupt the socket state.
        self._lock = asyncio.Lock()

        # Lazy-populated on connect().  Kept as Any to avoid leaking
        # pychromecast types into callers / type-checkers when the
        # dependency is absent.
        self._cast: Any = None
        self._media: Any = None
        self._connected: bool = False

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _require_dep() -> None:
        if not _PYCHROMECAST_AVAILABLE:
            raise RuntimeError("pychromecast not installed")

    async def _run(self, fn, *args, **kwargs):
        """Run a blocking pychromecast call on a worker thread with a
        per-command timeout, under the per-controller lock.

        Every public method that touches the device goes through here
        so the lock/timeout policy is enforced uniformly.
        """
        async with self._lock:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs),
                timeout=_COMMAND_TIMEOUT_S,
            )

    # ── Connection lifecycle ────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the TLS socket to the Cast device and wait until its
        Default Media Receiver app is ready to accept commands.

        Idempotent — repeated calls on an already-connected instance
        return immediately.  Uses ``pychromecast.Chromecast(host, port)``
        directly so we skip mDNS discovery (the caller already knows
        the host:port from cast_targets discovery).
        """
        self._require_dep()
        if self._connected and self._cast is not None:
            return

        def _open() -> tuple[Any, Any]:
            # Construct directly from host:port — no mDNS round-trip.
            # ``cast_info`` kwarg form changed across pychromecast
            # versions; the (host, port) positional form is stable
            # back to 9.x.
            cast = pychromecast.Chromecast(self._host, port=self._port)
            # wait_for_connection blocks until the worker thread has a
            # ready socket and the receiver has reported app status.
            # Without this, the first play_media call would race the
            # handshake and silently no-op on some firmware revisions.
            cast.wait()
            return cast, cast.media_controller

        cast, media = await self._run(_open)
        self._cast = cast
        self._media = media
        self._connected = True
        log.info(
            "chromecast connected: target=%s host=%s:%d uuid=%s",
            self._target_id, self._host, self._port, self._uuid,
        )

    async def disconnect(self) -> None:
        """Tear down the socket and stop the pychromecast worker thread.

        Safe to call on an already-disconnected instance.  Errors
        during disconnect are logged but not raised — the caller is
        usually in a cleanup path and doesn't care if the device is
        already gone.
        """
        if not self._connected or self._cast is None:
            return

        cast = self._cast
        self._connected = False
        self._cast = None
        self._media = None

        def _close() -> None:
            try:
                cast.disconnect(blocking=True)
            except Exception:  # noqa: BLE001 — cleanup, log only
                log.warning("chromecast disconnect raised", exc_info=True)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(_close),
                timeout=_COMMAND_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning(
                "chromecast disconnect timeout: target=%s",
                self._target_id,
            )
        log.info("chromecast disconnected: target=%s", self._target_id)

    # ── Capabilities ────────────────────────────────────────────────────

    async def capabilities(self) -> set[str]:
        """Return the codec set the Default Media Receiver decodes.

        Cast has no runtime capability probe — Google's spec is
        normative.  Returning a fresh ``set`` (not the frozenset) lets
        callers mutate it for negotiation without leaking state back.
        """
        self._require_dep()
        return set(_CAST_CAPABILITIES)

    # ── Single-track playback ───────────────────────────────────────────

    async def play(
        self,
        *,
        stream_url: str,
        content_type: str,
        title: str = "",
        artist: str = "",
        album: str = "",
        album_art_url: str = "",
        duration_s: float = 0.0,
    ) -> None:
        """Tell the receiver to load and play a single stream.

        Builds the ``MusicTrackMediaMetadata`` per Google's CAF schema
        (``metadataType: 3``) and forwards to
        ``MediaController.play_media``.  Optional ``duration_s`` is
        only included when > 0 — sending duration: 0 confuses some
        firmware revisions into showing an immediate "ended" state.
        """
        self._require_dep()
        if not self._connected or self._media is None:
            raise RuntimeError("chromecast not connected")

        metadata = _build_music_metadata(
            title=title, artist=artist, album=album, album_art_url=album_art_url,
        )

        media = self._media

        def _do_play() -> None:
            # play_media's signature varies subtly across pychromecast
            # versions, but (url, content_type, title=, metadata=) is
            # stable from 12.x onward.  Pass duration via metadata
            # rather than as a top-level kwarg — older pychromecast
            # drops unknown kwargs silently.
            kwargs: dict[str, Any] = {
                "title": title,
                "metadata": metadata,
            }
            if duration_s > 0:
                kwargs["stream_type"] = "BUFFERED"
            media.play_media(stream_url, content_type, **kwargs)

        log.debug(
            "chromecast play target=%s url=%s type=%s",
            self._target_id, stream_url, content_type,
        )
        await self._run(_do_play)

    async def pause(self) -> None:
        """Pause the currently-playing track."""
        self._require_dep()
        if not self._connected or self._media is None:
            raise RuntimeError("chromecast not connected")
        media = self._media
        log.debug("chromecast pause target=%s", self._target_id)
        await self._run(media.pause)

    async def resume(self) -> None:
        """Resume from pause.  pychromecast names this ``play()`` (the
        UPnP/Cast verb is the same word as our "start a new track"
        verb), so we forward to ``MediaController.play``."""
        self._require_dep()
        if not self._connected or self._media is None:
            raise RuntimeError("chromecast not connected")
        media = self._media
        log.debug("chromecast resume target=%s", self._target_id)
        await self._run(media.play)

    async def stop(self) -> None:
        """Stop playback and clear the current media session."""
        self._require_dep()
        if not self._connected or self._media is None:
            raise RuntimeError("chromecast not connected")
        media = self._media
        log.debug("chromecast stop target=%s", self._target_id)
        await self._run(media.stop)

    async def seek(self, *, seconds: float) -> None:
        """Seek to ``seconds`` from the start of the current track."""
        self._require_dep()
        if not self._connected or self._media is None:
            raise RuntimeError("chromecast not connected")
        media = self._media
        pos = float(seconds)
        log.debug(
            "chromecast seek target=%s pos=%.3f",
            self._target_id, pos,
        )
        await self._run(media.seek, pos)

    # ── Status ──────────────────────────────────────────────────────────

    async def position(self) -> dict:
        """Return current playback position and player state.

        Reads from the local ``MediaController.status`` cache rather
        than round-tripping the device — pychromecast keeps it warm
        via receiver status broadcasts, so this is effectively free.
        Returns ``{}`` if not connected so callers can poll without
        worrying about pre-connect state.
        """
        self._require_dep()
        if not self._connected or self._media is None:
            return {}

        media = self._media

        def _snapshot() -> dict:
            status = getattr(media, "status", None)
            if status is None:
                return {}
            # current_time is the receiver's last-reported playhead.
            # duration may be None for live streams — we coerce to 0.0
            # so callers can do arithmetic without None checks.
            duration = getattr(status, "duration", None) or 0.0
            position = getattr(status, "current_time", None) or 0.0
            # player_state strings per Cast spec:
            #   PLAYING, PAUSED, BUFFERING, IDLE.  pychromecast may
            #   surface None before the first status arrives; map to
            #   UNKNOWN so the UI has a stable enum to dispatch on.
            state = getattr(status, "player_state", None) or "UNKNOWN"
            return {
                "duration_s": float(duration),
                "position_s": float(position),
                "player_state": str(state),
            }

        return await self._run(_snapshot)

    # ── Queue (gapless multi-track) ────────────────────────────────────

    async def queue_load(self, items: list[dict]) -> None:
        """Replace the receiver's queue with ``items`` and start
        playback from the first one.

        pychromecast doesn't expose first-class queue ops, so we send
        the raw ``QUEUE_LOAD`` message on the media channel.  Message
        shape from Google's queueing docs:

            {
              'type': 'QUEUE_LOAD',
              'items': [<QueueItem>, ...],
              'startIndex': 0,
              'repeatMode': 'REPEAT_OFF',
            }

        Each QueueItem wraps a MediaInformation (built by
        ``_build_queue_item``).  ``startIndex: 0`` + per-item
        ``autoplay: True`` makes the queue start automatically — the
        ``autoplay`` flag in the request doc itself is not honoured by
        all firmware revisions, so we set it per-item too.
        """
        self._require_dep()
        if not self._connected or self._media is None:
            raise RuntimeError("chromecast not connected")
        if not items:
            raise ValueError("queue_load requires at least one item")

        queue_items = [_build_queue_item(it) for it in items]
        message = {
            "type": _QUEUE_LOAD,
            "items": queue_items,
            "startIndex": 0,
            "repeatMode": "REPEAT_OFF",
        }
        media = self._media

        def _send() -> None:
            # send_message is the low-level path on every
            # pychromecast controller — bypasses MediaController's
            # high-level helpers and writes the JSON straight to the
            # media channel.  Second arg ``inc_session_id=True`` makes
            # the receiver associate the queue with the current media
            # session (without it, some firmware ignores QUEUE_LOAD).
            media.send_message(message, inc_session_id=True)

        log.debug(
            "chromecast queue_load target=%s n_items=%d",
            self._target_id, len(queue_items),
        )
        await self._run(_send)

    async def queue_insert(
        self,
        items: list[dict],
        *,
        insert_before: int | None = None,
    ) -> None:
        """Insert ``items`` into the existing queue.

        ``insert_before`` is the *itemId* of the entry to insert ahead
        of — None means "append".  Caller is expected to obtain the
        itemId from a prior ``position()`` / status read; we don't try
        to translate playlist indices here because the receiver
        renumbers items on every mutation.
        """
        self._require_dep()
        if not self._connected or self._media is None:
            raise RuntimeError("chromecast not connected")
        if not items:
            raise ValueError("queue_insert requires at least one item")

        queue_items = [_build_queue_item(it) for it in items]
        message: dict[str, Any] = {
            "type": _QUEUE_INSERT,
            "items": queue_items,
        }
        if insert_before is not None:
            message["insertBefore"] = int(insert_before)

        media = self._media

        def _send() -> None:
            media.send_message(message, inc_session_id=True)

        log.debug(
            "chromecast queue_insert target=%s n_items=%d before=%s",
            self._target_id, len(queue_items), insert_before,
        )
        await self._run(_send)

    async def queue_next(self) -> None:
        """Advance to the next queue item.

        Implemented via ``QUEUE_UPDATE`` with ``jump: 1`` — the
        receiver's documented way to skip forward.  pychromecast has
        a ``queue_next`` shim on newer versions but its presence is
        version-dependent; the raw message is the stable contract.
        """
        self._require_dep()
        if not self._connected or self._media is None:
            raise RuntimeError("chromecast not connected")
        media = self._media

        def _send() -> None:
            media.send_message(
                {"type": _QUEUE_UPDATE, "jump": 1},
                inc_session_id=True,
            )

        log.debug("chromecast queue_next target=%s", self._target_id)
        await self._run(_send)

    async def queue_prev(self) -> None:
        """Go back to the previous queue item.

        Same as ``queue_next`` but with ``jump: -1``.  Note that some
        receiver firmware treats "prev" within the first ~3 s of a
        track as "restart current" — that's receiver-side behaviour
        and we can't override it from here.
        """
        self._require_dep()
        if not self._connected or self._media is None:
            raise RuntimeError("chromecast not connected")
        media = self._media

        def _send() -> None:
            media.send_message(
                {"type": _QUEUE_UPDATE, "jump": -1},
                inc_session_id=True,
            )

        log.debug("chromecast queue_prev target=%s", self._target_id)
        await self._run(_send)
