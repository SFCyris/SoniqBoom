# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AirPlay 1/2 receiver controller, backed by ``pyatv``.

Two AirPlay generations coexist on most home networks and the protocol
differences leak into what we can do with each:

* **AirPlay 1 (RAOP, Remote Audio Output Protocol)** — the original
  iTunes-era protocol.  Audio is sent as an ALAC- or PCM-wrapped RTP
  stream.  The wire format has *no* track-metadata frames, so a
  HomePod 1st gen / Airport Express / older speaker will display
  "Unknown Track" no matter what we hand it locally.  Seek, position,
  and remote-control are limited or absent on many AirPlay 1
  receivers — they treat the source (us) as an opaque pipe.

* **AirPlay 2 (MRP / HAP)** — introduced 2018 alongside iOS 11.4 and
  tvOS 11.4.  Adds metadata frames, multi-room sync, position/seek,
  and bidirectional control.  Apple TV 4 (gen 4) onwards, HomePod 2nd
  gen, HomePod mini, and the licensed third-party speakers (Sonos,
  Bose, B&O) all speak AirPlay 2.

``pyatv`` papers over most of this difference for us: ``atv.stream``
exposes both ``stream_file`` (AirPlay 2, accepts an http(s) URL or
local path plus a typed ``MediaMetadata``) and ``play_url`` (AirPlay 1
legacy RAOP).  We detect which the device supports at connect-time
and route accordingly.  NOTE: pyatv ≤ 0.13 used to call the AirPlay-2
entry point ``stream_url`` and accepted a metadata dict; that API was
renamed in 0.14 and the dict-shaped call now raises AttributeError on
every play.  We use ``stream_file`` for all currently-supported pyatv
versions.

Concurrency: one ``asyncio.Lock`` per controller instance, held around
every operation, with a 10 s timeout via ``asyncio.wait_for``.  Most
``pyatv`` calls are fast (< 1 s) but a stalled receiver — e.g.
HomePod that's mid-reboot — can hang ``connect()`` indefinitely
without it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import pyatv
    from pyatv.const import Protocol, PairingRequirement
    _PYATV_AVAILABLE = True
except ImportError:                                              # pragma: no cover
    pyatv = None                                                 # type: ignore[assignment]
    Protocol = None                                              # type: ignore[assignment]
    PairingRequirement = None                                    # type: ignore[assignment]
    _PYATV_AVAILABLE = False

# pyatv exceptions used to recognise pairing-required vs generic failures.
# Imported defensively so a very old pyatv (or a build without the
# exceptions module) doesn't break import; the runtime path widens its
# exception matching when these are None.
try:
    from pyatv.exceptions import AuthenticationError as _AuthenticationError
    from pyatv.exceptions import PairingError as _PairingError
except ImportError:                                              # pragma: no cover
    _AuthenticationError = None                                  # type: ignore[assignment]
    _PairingError = None                                         # type: ignore[assignment]


class PairingRequiredError(Exception):
    """connect() failed because the device needs to be paired first.

    Raised when the receiver returns AuthenticationError (or any error
    whose message hints at "no credentials" / "device requires pairing").
    The API layer catches this specifically and returns HTTP 412 with
    ``requires_pairing=True`` so the frontend can show the PIN modal
    rather than a generic "could not reach target" toast.
    """
    def __init__(self, identifier: str, message: str = "") -> None:
        self.identifier = identifier
        super().__init__(message or f"AirPlay device {identifier} requires pairing")


def _looks_like_auth_error(exc: BaseException) -> bool:
    """Detect "device requires pairing" across pyatv versions.

    pyatv 0.14+ raises ``AuthenticationError``; older builds threw a
    generic ``Exception`` with the auth hint in the message.  We
    match both shapes so the picker UI shows the PIN modal regardless
    of which pyatv the operator has installed.
    """
    if _AuthenticationError is not None and isinstance(exc, _AuthenticationError):
        return True
    msg = str(exc).lower()
    return (
        "authentication" in msg or
        "no credentials" in msg or
        "requires pairing" in msg or
        "pin required" in msg or
        "not paired" in msg
    )

# MediaMetadata was promoted to pyatv.interface in 0.14 alongside the
# rename of ``stream_url`` → ``stream_file``.  We import it defensively
# so a very old pyatv (which had ``stream_url`` and a dict-shaped
# metadata kwarg) still falls back to a no-metadata path instead of
# crashing import.  The runtime check below picks the right code path.
try:
    from pyatv.interface import MediaMetadata as _MediaMetadata
    _MEDIA_METADATA_AVAILABLE = True
except ImportError:                                              # pragma: no cover
    _MediaMetadata = None                                        # type: ignore[assignment]
    _MEDIA_METADATA_AVAILABLE = False

from soniqboom.core.cast_codecs import DEFAULT_CAPS

log = logging.getLogger(__name__)


# Per-operation timeout.  Scan/connect on a healthy LAN finishes in
# well under a second; we give it 10 s to cover slow networks and
# devices that take their time accepting the session.
_OP_TIMEOUT_S: float = 10.0

# pyatv.scan timeout.  Independent of _OP_TIMEOUT_S because scan is
# the slowest single step (it waits the full window for late mDNS
# replies) and we still want connect() to finish inside the outer
# wait_for budget.
_SCAN_TIMEOUT_S: float = 5.0

# The name AirPlay receivers show in their "Allow … to play music?" prompt
# and in the OS Now-Playing "from" line.  pyatv announces ``settings.info.name``
# (default "pyatv", and empty on some receivers) — we override it so the user
# sees the program that's actually casting.
_CAST_SENDER_NAME = "SoniqBoom"


async def _named_storage(config: "Any"):
    """A pyatv ``MemoryStorage`` whose advertised sender name is SoniqBoom.

    Passed to both ``pyatv.connect`` and ``pyatv.pair`` so the AirPlay
    pairing PIN dialog and the streaming "Allow … to play music" prompt
    identify the sender as ``SoniqBoom`` instead of "" / "pyatv".  Best
    effort — any failure degrades to the default (unnamed) storage rather
    than blocking the connection.
    """
    from pyatv.storage.memory_storage import MemoryStorage
    storage = MemoryStorage()
    try:
        settings = await storage.get_settings(config)
        settings.info.name = _CAST_SENDER_NAME
        settings.info.os_name = _CAST_SENDER_NAME
    except Exception:
        log.debug("AirPlay: could not set sender name on storage", exc_info=True)
    return storage


class AirPlayController:
    """Controls a single AirPlay 1 or AirPlay 2 receiver.

    Instantiation is cheap and doesn't touch the network — ``connect()``
    does the actual scan + session setup and is idempotent (safe to
    call multiple times; second call is a no-op while still connected).

    The controller is *not* thread-safe but *is* coroutine-safe via
    its internal lock — concurrent callers from the same event loop
    serialise rather than racing the underlying pyatv session.
    """

    def __init__(self, *, identifier: str, target_id: str) -> None:
        if not _PYATV_AVAILABLE:
            # Fail fast: nothing else in this class will work without
            # pyatv.  Callers handle this by either falling back to a
            # different controller or surfacing "AirPlay support not
            # installed" in the picker UI.
            raise RuntimeError("pyatv not installed")

        self.identifier: str = identifier
        self.target_id: str = target_id

        self._atv: Any | None = None        # pyatv.interface.AppleTV when connected
        self._config: Any | None = None     # pyatv.interface.BaseConfig
        self._is_airplay2: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        # In-flight pairing handler — set by ``begin_pair`` and consumed by
        # ``finish_pair``.  Held across the two HTTP requests (begin / submit
        # PIN) so we don't have to ask the device to re-show the PIN.
        self._pair_handler: Any | None = None
        # Display name captured during begin_pair so finish_pair can
        # persist a friendly label alongside the credentials blob.
        self._pair_device_name: str = ""

    # ── Connection lifecycle ───────────────────────────────────────────────

    @property
    def is_airplay2(self) -> bool:
        """True if the connected device supports AirPlay 2 (MRP/HAP).

        ``False`` if disconnected or if only the legacy RAOP service
        was advertised.  Used by ``play()`` to decide between
        ``stream_url`` (with metadata) and ``play_url`` (without).
        """
        return self._is_airplay2

    async def connect(self) -> None:
        """Resolve identifier via ``pyatv.scan``, then ``pyatv.connect``.

        Idempotent — calling while already connected is a no-op.  On
        failure the controller is left in the disconnected state so
        the caller can retry without first calling ``disconnect()``.
        """
        async with self._lock:
            if self._atv is not None:
                # Already connected — nothing to do.  We don't re-scan
                # because the identifier-based session is still valid
                # as long as the device stayed on the network.
                return

            loop = asyncio.get_running_loop()

            async def _do_connect() -> None:
                # pyatv.scan with an identifier filter returns at most
                # one config (or an empty list if the device dropped
                # off the network since the last discovery).
                results = await pyatv.scan(
                    loop,
                    identifier=self.identifier,
                    timeout=_SCAN_TIMEOUT_S,
                )
                if not results:
                    raise RuntimeError(
                        f"AirPlay device {self.identifier!r} not found on network"
                    )
                config = results[0]

                # Detect AirPlay 2 vs 1 BEFORE connecting.  Apple TV
                # 4+ and HomePod 2nd gen advertise an MRP service
                # alongside the AirPlay service; that combo (or the
                # newer HAP service) is the marker for AirPlay 2.
                # Pure-RAOP devices (Airport Express, original
                # HomePod in some configs, generic speakers) only
                # have the RAOP service.
                self._is_airplay2 = _detect_airplay2(config)

                # Apply any stored credentials before connecting.  AirPlay
                # 2 receivers (Apple TV 4+, HomePod, macOS AirPlay
                # Receiver) require a one-time PIN pairing; the resulting
                # credentials blob is persisted by ``finish_pair`` and
                # re-applied here so a server restart doesn't make the
                # user re-pair.  No-op when nothing is stored (first
                # connect) — in that case we let pyatv raise
                # AuthenticationError below and ``_looks_like_auth_error``
                # turns it into ``PairingRequiredError`` for the UI.
                try:
                    from soniqboom.core import airplay_credentials
                    stored = airplay_credentials.get(self.identifier)
                except Exception:
                    stored = None
                if stored:
                    try:
                        config.set_credentials(Protocol.AirPlay, stored)
                        log.debug("Applied stored AirPlay credentials for %s", self.identifier)
                    except Exception:
                        # Stale / corrupt creds — log and try unauthenticated.
                        # finish_pair will overwrite once the user re-pairs.
                        log.warning("Stored AirPlay credentials rejected for %s — "
                                    "will need re-pairing", self.identifier)
                else:
                    # No stored credentials.  Some AirPlay-2 receivers (macOS
                    # AirPlay Receiver, Apple TV) connect fine unauthenticated
                    # but only DEMAND the PIN at stream-setup time — pyatv then
                    # blocks on ``/pair-pin-start`` until our op-timeout fires,
                    # which we'd surface as a confusing "could not reach the
                    # cast target".  If the device advertises mandatory
                    # pairing, route to the PIN-entry modal up front instead of
                    # letting the stream time out.
                    svc = config.get_service(Protocol.AirPlay)
                    if (PairingRequirement is not None and svc is not None
                            and getattr(svc, "pairing", None) == PairingRequirement.Mandatory):
                        log.info("AirPlay %s requires pairing and we have no "
                                 "credentials — prompting for PIN", self.identifier)
                        raise PairingRequiredError(
                            self.identifier,
                            "AirPlay device requires PIN pairing — enter the "
                            "code shown on the device.",
                        )

                # Named storage → the receiver's "Allow … to play music"
                # prompt + OS Now-Playing source read "SoniqBoom".
                storage = await _named_storage(config)
                self._atv = await pyatv.connect(config, loop, storage=storage)
                self._config = config

            try:
                await asyncio.wait_for(_do_connect(), timeout=_OP_TIMEOUT_S)
            except Exception as exc:
                # Reset to a known-disconnected state on any failure so
                # retries don't see a half-built session.
                self._atv = None
                self._config = None
                self._is_airplay2 = False
                # Auth-required → distinct exception type so the API
                # layer can return HTTP 412 with ``requires_pairing=True``
                # rather than a generic 502.  The UI uses this to pop
                # the PIN-entry modal.
                if _looks_like_auth_error(exc):
                    raise PairingRequiredError(self.identifier, str(exc)) from exc
                raise

            log.info(
                "AirPlay connected: target=%s id=%s airplay2=%s",
                self.target_id, self.identifier, self._is_airplay2,
            )

    async def disconnect(self) -> None:
        """Tear down the pyatv session.  Safe to call when not connected."""
        async with self._lock:
            if self._atv is None:
                return
            atv = self._atv
            # Clear refs FIRST so a re-entrant call (e.g. from an
            # exception handler) sees the disconnected state.
            self._atv = None
            self._config = None
            self._is_airplay2 = False
            try:
                atv.close()
            except Exception:
                # pyatv.close() is best-effort; log and move on rather
                # than letting cleanup raise out of a finally block.
                log.warning("AirPlay close raised for %s", self.target_id, exc_info=True)
            log.info("AirPlay disconnected: target=%s", self.target_id)

    # ── Pairing ────────────────────────────────────────────────────────────

    async def begin_pair(self, device_name: str = "") -> dict:
        """Start an AirPlay pairing handshake.

        Triggers the receiver to display a 4-digit PIN.  The handler is
        stored on the controller so ``finish_pair`` (the next request)
        can submit the PIN against the same in-flight session — pyatv
        won't show a new PIN if we make the device do the dance twice.

        ``device_name`` is informational; persisted alongside the
        credentials once pairing succeeds.

        Returns ``{"pin_required": True}`` for the normal case (device
        shows a PIN we have to type back), or ``{"pin_required": False,
        "already_paired": True}`` if pyatv decides the device doesn't
        need a PIN (very rare with AirPlay 2 — kept for protocol
        completeness).
        """
        async with self._lock:
            loop = asyncio.get_running_loop()

            # Tear down any live play/connect session FIRST.  The failed play
            # that routed us into pairing left ``self._atv`` connected; if we
            # leave it open, the pairing handler below opens a SECOND
            # connection and the receiver shows two "accept"/PIN prompts,
            # confusing both sender and receiver.  Close it directly (not via
            # the locked disconnect(), which would re-enter self._lock).
            if self._atv is not None:
                try:
                    self._atv.close()
                except Exception:
                    log.debug("AirPlay: closing stale session before pair raised",
                              exc_info=True)
                self._atv = None
                self._config = None
                self._is_airplay2 = False

            # Always do a fresh scan — pairing requires the same
            # config + identifier the live connect() would use, and a
            # cached self._config could be stale (device IP moved,
            # protocol features changed, etc.).
            results = await pyatv.scan(
                loop, identifier=self.identifier, timeout=_SCAN_TIMEOUT_S,
            )
            if not results:
                raise RuntimeError(
                    f"AirPlay device {self.identifier!r} not found on network — "
                    f"check it's powered on and on the same Wi-Fi"
                )
            config = results[0]

            # Close any stale handler from a previous abandoned attempt.
            await self._close_pair_handler()

            # Pass the sender name both ways: ``name=`` is what the pairing
            # handler reads directly, and the named storage covers the
            # streaming SETUP — so the PIN dialog says "SoniqBoom", not "".
            storage = await _named_storage(config)
            handler = await pyatv.pair(
                config, Protocol.AirPlay, loop,
                storage=storage, name=_CAST_SENDER_NAME,
            )
            try:
                await asyncio.wait_for(handler.begin(), timeout=_OP_TIMEOUT_S)
            except Exception:
                try: await handler.close()
                except Exception: pass
                raise

            self._pair_handler = handler
            self._pair_device_name = device_name
            log.info("Begun AirPlay pairing for %s — device should show a PIN now",
                     self.identifier)
            return {
                "pin_required": bool(getattr(handler, "device_provides_pin", True)),
            }

    async def finish_pair(self, pin: str) -> dict:
        """Submit the user-entered PIN and persist the resulting credentials.

        Must be preceded by a matching ``begin_pair`` on the same
        controller instance.  On success the receiver remembers us, the
        credentials are written to ``data_dir/airplay_credentials.json``
        for future runs, and the pair handler is closed.

        Raises if ``pin`` is rejected (wrong code, expired session) or
        if begin_pair wasn't called first.
        """
        async with self._lock:
            handler = self._pair_handler
            if handler is None:
                raise RuntimeError(
                    "No pairing session in progress — call begin_pair first."
                )

            # ``pin()`` is sync in pyatv (just stashes the digits on the
            # handler); ``finish()`` is the actual async network call.
            try:
                handler.pin(pin)
            except Exception:
                # Most pyatv versions tolerate any string here and only
                # validate during finish(); but defensively re-raise so
                # the API layer can return 400.
                raise

            try:
                await asyncio.wait_for(handler.finish(), timeout=_OP_TIMEOUT_S)
            except Exception as exc:
                # PIN mismatch / expired — keep the handler around so the
                # user can re-submit?  pyatv leaves it in an unusable
                # state after finish() fails, so close + clear and force
                # the user to begin_pair again.  This also re-prompts
                # the device PIN, which is the right UX (the previous
                # PIN may have timed out on the device side already).
                await self._close_pair_handler()
                # Surface the actual reason so the UI can show e.g.
                # "Wrong PIN" vs "Pairing timed out".
                raise PairingError(str(exc)) from exc

            # Extract credentials.  pyatv exposes them as
            # ``handler.service.credentials`` — a string blob we round-
            # trip to ``set_credentials`` on the next connect.
            credentials = ""
            try:
                service = handler.service
                if service is not None:
                    credentials = service.credentials or ""
            except Exception:
                log.warning("Could not read credentials from pair handler — "
                            "pairing succeeded but won't persist", exc_info=True)

            paired = bool(getattr(handler, "has_paired", credentials))
            device_name = self._pair_device_name

            # Close the handler — credentials are extracted, the rest is
            # network state we no longer need.
            await self._close_pair_handler()

            if credentials:
                try:
                    from soniqboom.core import airplay_credentials
                    airplay_credentials.set(
                        self.identifier, credentials, device_name=device_name,
                    )
                except Exception:
                    log.warning("Failed to persist AirPlay credentials for %s — "
                                "user will need to re-pair next session",
                                self.identifier, exc_info=True)

            log.info("AirPlay pairing finished for %s — paired=%s creds=%s",
                     self.identifier, paired, bool(credentials))
            return {"paired": paired, "credentials_saved": bool(credentials)}

    async def _close_pair_handler(self) -> None:
        """Best-effort cleanup of an in-flight pair handler."""
        handler = self._pair_handler
        self._pair_handler = None
        self._pair_device_name = ""
        if handler is None:
            return
        try:
            await asyncio.wait_for(handler.close(), timeout=2.0)
        except Exception:
            # Cleanup failure isn't actionable — the handler is going
            # out of scope anyway.  Log at debug so we don't pollute
            # the operator's INFO stream with non-events.
            log.debug("Pair-handler close raised for %s", self.identifier, exc_info=True)


    # ── Capabilities ───────────────────────────────────────────────────────

    async def capabilities(self) -> set[str]:
        """Return the codec set we'll consider negotiating to.

        AirPlay 2 (Apple TV 4+, HomePod 2nd gen, HomePod mini, licensed
        speakers) accepts ALAC, AAC, MP3 and WAV/LPCM.  AirPlay 1
        (RAOP) practically supports ALAC and PCM-WAV — MP3/AAC are
        possible in spec but pyatv's RAOP path streams ALAC.

        Falls back to the protocol-wide default in
        ``cast_codecs.DEFAULT_CAPS['airplay']`` if we somehow don't
        know what we're talking to.
        """
        if self._atv is None:
            # Best-effort fallback — caller may be probing before
            # connecting (e.g. from the picker preview).
            return set(DEFAULT_CAPS.get("airplay", set()))

        if self._is_airplay2:
            return {"alac", "aac", "mp3", "wav"}

        # Legacy RAOP path.  Most third-party AirPlay-1 speakers
        # accept ALAC (Apple Lossless) over the RTP stream; WAV/LPCM
        # works on every receiver that speaks RAOP at all.  We
        # deliberately omit MP3/AAC here because pyatv's RAOP
        # implementation re-encodes to ALAC regardless and listing
        # MP3 would mislead the negotiator into not transcoding.
        return {"alac", "wav"}

    # ── Playback ───────────────────────────────────────────────────────────

    async def play(
        self,
        *,
        stream_url: str,
        content_type: str,
        title: str = "",
        artist: str = "",
        album: str = "",
        album_art: bytes | None = None,
    ) -> None:
        """Start playback of ``stream_url`` on the receiver.

        ``content_type`` is informational here — pyatv negotiates the
        codec on the wire — but we keep it in the signature so the
        cast pipeline can pass through the same value it stamped on
        the HTTP response (useful for telemetry / debugging).

        Metadata behaviour differs by protocol:

        * **AirPlay 2** — title/artist/album are wrapped in a
          ``pyatv.interface.MediaMetadata`` dataclass and forwarded
          to the receiver as MRP/HAP "Now Playing" frames (Apple TV,
          HomePod, lock-screen).  ``album_art`` is the raw cover-art
          *bytes* (JPEG/PNG) — ``MediaMetadata.artwork`` expects bytes,
          not a URL — and is passed straight through; the orchestrator
          (cast_session) resolves + caps + caches them.  ``None`` means
          "no artwork frame", which pyatv handles without crashing.
        * **AirPlay 1** — the RAOP protocol has *no* metadata frames.
          Anything we pass here is silently dropped.  HomePod 1st gen
          and other RAOP-only receivers will display "Unknown Track"
          and we can't do anything about it from this side — the
          metadata-display feature is genuinely absent from the
          protocol.  Document this loudly so nobody chases the bug.
        """
        if self._atv is None:
            raise RuntimeError("AirPlay not connected")

        async with self._lock:
            assert self._atv is not None
            stream = self._atv.stream

            async def _do_play() -> None:
                if self._is_airplay2:
                    # AirPlay 2.  pyatv 0.14+ renamed the URL-streaming
                    # entry point: there is no more ``stream_url`` — the
                    # method is ``stream_file`` and accepts either a
                    # local path or an http(s) URL.  Metadata is now a
                    # typed ``MediaMetadata`` dataclass, not a dict.
                    # The previous dict-kwarg call raised
                    # ``AttributeError: 'FacadeStream' object has no
                    # attribute 'stream_url'`` on every play attempt,
                    # which surfaced in the UI as "can't find device".
                    #
                    # Artwork in MediaMetadata is raw *bytes* — the
                    # orchestrator resolved + size-capped them for us, so
                    # we hand them straight to pyatv.  ``None`` is fine:
                    # pyatv emits no artwork frame rather than crashing.
                    if _MEDIA_METADATA_AVAILABLE:
                        md = _MediaMetadata(
                            title=title or None,
                            artist=artist or None,
                            album=album or None,
                            artwork=album_art or None,
                        )
                        log.debug(
                            "AirPlay2 stream_file target=%s url=%s title=%r artist=%r "
                            "album=%r artwork=%s",
                            self.target_id, stream_url, title, artist, album,
                            f"{len(album_art)}B" if album_art else "none",
                        )
                        await stream.stream_file(stream_url, metadata=md)
                    else:
                        # Very old pyatv without MediaMetadata — call
                        # without metadata so playback at least starts.
                        log.debug(
                            "AirPlay2 stream_file target=%s url=%s (no MediaMetadata in this pyatv)",
                            self.target_id, stream_url,
                        )
                        await stream.stream_file(stream_url)
                else:
                    # AirPlay 1: legacy RAOP.  ``play_url`` has no
                    # metadata kwarg.  See module docstring + this
                    # method's docstring for why.
                    log.debug(
                        "AirPlay1 play_url target=%s url=%s (metadata dropped — RAOP limitation)",
                        self.target_id, stream_url,
                    )
                    await stream.play_url(stream_url)

            try:
                await asyncio.wait_for(_do_play(), timeout=_OP_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning(
                    "AirPlay play() timed out after %.1fs target=%s",
                    _OP_TIMEOUT_S, self.target_id,
                )
                # A play timeout with NO stored credentials is almost always
                # the receiver demanding a PIN at stream-setup time (pyatv
                # blocks on ``/pair-pin-start``).  Route to the PIN-entry modal
                # instead of a dead-end "could not reach" error — belt-and-
                # suspenders for receivers whose advertised pairing requirement
                # didn't trip the proactive check in connect().
                try:
                    from soniqboom.core import airplay_credentials
                    _have_creds = airplay_credentials.get(self.identifier) is not None
                except Exception:
                    _have_creds = False
                if not _have_creds:
                    raise PairingRequiredError(
                        self.identifier,
                        "AirPlay device requires PIN pairing — enter the code "
                        "shown on the device.",
                    )
                raise
            except Exception:
                log.warning(
                    "AirPlay play() failed target=%s",
                    self.target_id, exc_info=True,
                )
                raise

    async def pause(self) -> None:
        """Pause playback via the remote-control interface."""
        await self._remote_call("pause")

    async def resume(self) -> None:
        """Resume after pause.  pyatv uses ``play()`` for resume."""
        await self._remote_call("play")

    async def stop(self) -> None:
        """Stop playback and release the audio session on the receiver."""
        await self._remote_call("stop")

    async def seek(self, *, seconds: float) -> None:
        """Seek to ``seconds`` from the start of the current track.

        AirPlay 1 receivers may silently ignore this — RAOP doesn't
        define a seek primitive and pyatv emulates by tearing down
        and re-establishing the stream at the new offset, which not
        every receiver tolerates.  Best-effort.
        """
        if self._atv is None:
            raise RuntimeError("AirPlay not connected")

        async with self._lock:
            assert self._atv is not None
            rc = self._atv.remote_control

            async def _do_seek() -> None:
                log.debug(
                    "AirPlay seek target=%s seconds=%.2f",
                    self.target_id, seconds,
                )
                await rc.set_position(int(round(seconds)))

            try:
                await asyncio.wait_for(_do_seek(), timeout=_OP_TIMEOUT_S)
            except Exception:
                log.warning(
                    "AirPlay seek failed target=%s seconds=%.2f",
                    self.target_id, seconds, exc_info=True,
                )
                raise

    # ── Status / introspection ────────────────────────────────────────────

    async def position(self) -> dict:
        """Snapshot of the receiver's current playback position.

        Returns ``{'duration_s', 'position_s', 'device_state'}`` where
        ``device_state`` is one of ``'playing' | 'paused' | 'idle' |
        'seeking'`` (normalised from pyatv's ``DeviceState`` enum).

        Returns ``{}`` if not connected, so callers can treat the
        empty dict as "no info available" without raising.
        """
        if self._atv is None:
            return {}

        async with self._lock:
            assert self._atv is not None
            atv = self._atv

            async def _do_position() -> dict:
                playing = await atv.metadata.playing()
                # ``playing.device_state`` is an enum; .name is the
                # string form ("Playing", "Paused", "Idle", "Seeking").
                # We lowercase for the public API.
                ds_obj = getattr(playing, "device_state", None)
                if ds_obj is None:
                    state = "idle"
                else:
                    # Enum: prefer .name; fall back to str() for ducks.
                    raw = getattr(ds_obj, "name", None) or str(ds_obj)
                    state = raw.split(".")[-1].lower()
                    # Normalise — pyatv's enum names happen to match
                    # our four canonical values, but guard against
                    # future additions (e.g. "Loading") by mapping
                    # anything unexpected to 'idle'.
                    if state not in ("playing", "paused", "idle", "seeking"):
                        state = "idle"

                duration = getattr(playing, "total_time", None)
                if duration is None:
                    duration = getattr(playing, "duration", None)
                position = getattr(playing, "position", None)

                return {
                    "duration_s": float(duration) if duration is not None else 0.0,
                    "position_s": float(position) if position is not None else 0.0,
                    "device_state": state,
                }

            try:
                return await asyncio.wait_for(_do_position(), timeout=_OP_TIMEOUT_S)
            except Exception:
                log.warning(
                    "AirPlay position() failed target=%s",
                    self.target_id, exc_info=True,
                )
                return {}

    async def set_volume(self, *, percent: float) -> None:
        """Set receiver volume.  ``percent`` is 0-100, clamped here.

        pyatv's ``audio.set_volume`` already takes a 0-100 float, so
        we forward directly after clamping.  Some AirPlay 1 receivers
        (notably Airport Express) ignore volume commands when they're
        feeding an external amp — that's a hardware limitation, not
        ours.
        """
        if self._atv is None:
            raise RuntimeError("AirPlay not connected")

        clamped = max(0.0, min(100.0, float(percent)))

        async with self._lock:
            assert self._atv is not None
            audio = self._atv.audio

            async def _do_set_volume() -> None:
                log.debug(
                    "AirPlay set_volume target=%s percent=%.1f",
                    self.target_id, clamped,
                )
                await audio.set_volume(clamped)

            try:
                await asyncio.wait_for(_do_set_volume(), timeout=_OP_TIMEOUT_S)
            except Exception:
                log.warning(
                    "AirPlay set_volume failed target=%s percent=%.1f",
                    self.target_id, clamped, exc_info=True,
                )
                raise

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _remote_call(self, action: str) -> None:
        """Run a parameterless remote_control method (pause/play/stop)
        under the lock + timeout.

        Centralising these three keeps the timeout / lock / logging
        identical across them so we don't accidentally drift one of
        them off the canonical pattern.
        """
        if self._atv is None:
            raise RuntimeError("AirPlay not connected")

        async with self._lock:
            assert self._atv is not None
            rc = self._atv.remote_control
            method = getattr(rc, action, None)
            if method is None:
                raise RuntimeError(f"pyatv remote_control has no {action!r}")

            async def _do_call() -> None:
                log.debug("AirPlay %s target=%s", action, self.target_id)
                await method()

            try:
                await asyncio.wait_for(_do_call(), timeout=_OP_TIMEOUT_S)
            except Exception:
                log.warning(
                    "AirPlay %s failed target=%s",
                    action, self.target_id, exc_info=True,
                )
                raise


# ── Module-level helpers ───────────────────────────────────────────────────

def _detect_airplay2(config: Any) -> bool:
    """Return True if ``config`` advertises AirPlay 2.

    pyatv represents each discovered protocol as a Service on the
    config.  AirPlay 2 receivers expose either an MRP service (Apple
    TV 4 / 4K) or a HAP service (HomePod) alongside the AirPlay
    service.  Pure-RAOP devices have ONLY the RAOP service (or an
    AirPlay service whose version field is 1.x).

    We try service-presence first because it's the most reliable
    signal, then fall back to feature probing.  Failure to detect
    defaults to AirPlay 1 — safer to under-claim caps than to send
    AirPlay 2 metadata frames to a receiver that'll choke on them.
    """
    if not _PYATV_AVAILABLE or Protocol is None:
        return False

    try:
        # MRP indicates Apple TV 4+ (full AirPlay 2 + MRP control).
        mrp_attr = getattr(Protocol, "MRP", None)
        if mrp_attr is not None and config.get_service(mrp_attr) is not None:
            return True

        # Companion / HAP indicates HomePod 2nd gen + HomePod mini.
        for proto_name in ("Companion", "HAP"):
            proto_attr = getattr(Protocol, proto_name, None)
            if proto_attr is not None and config.get_service(proto_attr) is not None:
                return True

        # Direct AirPlay service with version >= 2.0 — newer pyatv
        # versions expose the announced AirPlay version on the
        # service so we can disambiguate without other protocols.
        airplay_attr = getattr(Protocol, "AirPlay", None)
        if airplay_attr is not None:
            ap_service = config.get_service(airplay_attr)
            if ap_service is not None:
                version = getattr(ap_service, "properties", {}).get("srcvers", "")
                # AirPlay 2 src versions are 350+ (iOS 11.4 era and
                # later); AirPlay 1 stops at the 200s.  Cheap test.
                if version:
                    try:
                        major = int(version.split(".")[0])
                        if major >= 350:
                            return True
                    except (ValueError, IndexError):
                        pass
    except Exception:
        # Any introspection failure → treat as AirPlay 1.  This is a
        # capability question, not a connection question; we should
        # never raise out of detection.
        log.debug("AirPlay 2 detection raised; falling back to AirPlay 1", exc_info=True)
        return False

    return False


class PairingError(Exception):
    """User-visible pairing failure — wrong PIN, expired session, etc.

    Distinct from :class:`PairingRequiredError` (which signals that
    *pairing is needed*); this one signals *pairing failed* after the
    user tried.  The API layer maps this to HTTP 400 with the original
    message so the UI can show "Wrong PIN — try again."
    """
    pass
