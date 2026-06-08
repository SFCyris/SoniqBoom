# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cast-session orchestrator: one ``CastSession`` per active target.

Sits above the three per-protocol controllers (DLNA / Cast / AirPlay)
and provides a uniform play/pause/stop/seek/queue surface for the
API layer.  Also owns:

  • The signed stream-URL minting (delegates to cast_tokens).
  • Codec negotiation between source + renderer (delegates to cast_codecs).
  • Lookahead prewarm for the next queued track (so transcode is
    already started by the time the renderer fetches it).
  • Cancellation: when a session goes away (user picks a different
    output, server shuts down, renderer drops off network), every
    in-flight ffmpeg launched on its behalf is killed.

We hold sessions in a process-global dict keyed by ``target_id``
(the cast_targets.CastTarget.id).  A single user controlling N
targets has N sessions; selecting the same target a second time
re-uses the existing session (idempotent connect).  Sessions
self-evict after 10 minutes of inactivity to free network resources.

Why "session" not "client": a Chromecast object is expensive to
construct (mDNS + WebSocket handshake), and DLNA description-URL
fetches involve a second HTTP round-trip — both want to be cached.
Sessions are also the right scope for queue state, since the
renderer's view of the queue must stay coherent across multiple
play/pause toggles from the SoniqBoom UI.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from soniqboom.core import cast_codecs, cast_tokens
from soniqboom.core.cast_targets import CastTarget
from soniqboom.core.data import get_track


# ── Lookahead prewarm window ────────────────────────────────────────────────
# Mirror the player.js lookahead (Spotify-research-derived: N+3 only pays
# off on uninterrupted queue listening; N+1 + N+2 covers the realistic
# zap pattern with the smallest cache-budget cost).  Each prewarm fires
# in the background via conversion_cache.start_background_render — so
# the user-facing /play returns immediately and the renderer's GET on
# track N+1 hits a warm cache.
_PREWARM_WINDOW = 2

log = logging.getLogger(__name__)


# Session idle timeout — 10 min covers a long pause without forcing a
# manual reconnect, while still freeing dead sessions promptly.
_IDLE_TIMEOUT_S = 10 * 60

# Time-to-live for the local LAN URL cache (see _server_base_url).
_BASE_URL_TTL_S = 60


# ── Public types ───────────────────────────────────────────────────────────

@dataclass
class QueueItem:
    track_id:        str
    title:           str = ""
    artist:          str = ""
    album:           str = ""
    duration_s:      float = 0.0
    album_art_url:   str = ""
    # Sub-song selector for SID / tracker / GME formats — ignored for
    # everything else.  Defaults to 0 (the first / only sub-song).
    subsong:         int = 0


@dataclass
class SessionState:
    target:          CastTarget
    last_activity:   float = field(default_factory=time.time)
    delivered_codec: str | None = None
    source_codec:    str | None = None
    transcode_used:  bool = False
    queue:           list[QueueItem] = field(default_factory=list)
    queue_index:     int = 0
    user_pref:       str = "auto"          # 'auto' | 'force-mp3' | 'force-flac' | 'force-original'

    def touch(self) -> None:
        self.last_activity = time.time()

    def to_public(self) -> dict:
        return {
            "target":          self.target.to_public(),
            "delivered_codec": self.delivered_codec,
            "source_codec":    self.source_codec,
            "transcode":       self.transcode_used,
            "queue_size":      len(self.queue),
            "queue_index":     self.queue_index,
            "user_pref":       self.user_pref,
        }


class CastSession:
    """Wraps a per-target protocol controller + holds the running
    state we need across multiple API calls."""

    def __init__(self, target: CastTarget) -> None:
        self.target = target
        self.state  = SessionState(target=target)
        self._controller: Any | None = None
        self._caps: set[str] | None  = None
        self._lock = asyncio.Lock()
        # Strong-reference set for fire-and-forget prewarm tasks.
        # asyncio.create_task without a strong reference is GC-eligible
        # the moment the coroutine yields; on a stressed event loop the
        # background prewarm can silently disappear before the renderer
        # finishes — Audio-2 P1.  Add to the set on schedule, discard
        # via done-callback on completion.
        self._prewarm_tasks: set[asyncio.Task] = set()

    # ── Controller lazy-init ───────────────────────────────────────────

    async def _ensure_controller(self) -> Any:
        if self._controller is not None:
            return self._controller
        proto = self.target.protocol.lower()
        if proto == "dlna":
            from soniqboom.core.cast_dlna import DLNAController
            # SSDP's LOCATION header gives us the renderer's real
            # description URL; cast_targets.CastTarget now plumbs it
            # through.  Fall back to a host:port synthesis only when
            # discovery somehow returned a target without one (manual
            # add via Settings, for example).
            desc_url = (
                self.target.description_url
                or f"http://{self.target.host}:{self.target.port or 80}/description.xml"
            )
            ctrl = DLNAController(description_url=desc_url, target_id=self.target.id)
        elif proto == "cast":
            from soniqboom.core.cast_chromecast import ChromecastController
            ctrl = ChromecastController(
                host=self.target.host,
                port=self.target.port or 8009,
                uuid=self.target.id,
                target_id=self.target.id,
            )
        elif proto == "airplay":
            from soniqboom.core.cast_airplay import AirPlayController
            ctrl = AirPlayController(
                identifier=self.target.id,
                target_id=self.target.id,
            )
        else:
            raise ValueError(f"Unknown cast protocol: {self.target.protocol}")
        try:
            await ctrl.connect()
        except Exception:
            # connect() raised — release whatever resources the
            # controller may have already opened (aiohttp session,
            # pychromecast worker thread, pyatv session) before
            # propagating.  Without this every failed connect leaks
            # a controller object that holds OS-level handles open
            # until GC.
            try:
                await ctrl.disconnect()
            except Exception:
                pass
            raise
        self._controller = ctrl
        # First post-connect: probe capabilities once and cache.
        try:
            self._caps = await ctrl.capabilities()
        except Exception:
            log.warning("Capability probe failed for %s; using protocol default", self.target.id)
            self._caps = set(cast_codecs.DEFAULT_CAPS.get(proto, {"mp3"}))
        return ctrl

    # ── Public surface ─────────────────────────────────────────────────

    async def play_track(
        self,
        *,
        track_id: str,
        user_id: str | int | None = None,
        base_url: str | None = None,
        subsong: int = 0,
    ) -> dict:
        """Play a single track on the target.  Returns a dict with the
        delivered codec, transcode flag, and stream URL (for debug).

        ``subsong`` selects the sub-tune index for SID / tracker / GME
        formats.  Defaults to 0; the API layer plumbs the user-picked
        value through here, and we encode it in the signed cast URL so
        the renderer's GET resolves to the same sub-tune the user
        clicked in the SoniqBoom UI.
        """
        async with self._lock:
            return await self._play_track_locked(
                track_id=track_id, user_id=user_id, base_url=base_url,
                subsong=subsong,
            )

    async def _play_track_locked(
        self,
        *,
        track_id: str,
        user_id: str | int | None = None,
        base_url: str | None = None,
        subsong: int = 0,
    ) -> dict:
        """Lock-already-held variant of play_track.  Callers that already
        hold ``self._lock`` (e.g. queue_next) must use this — re-entering
        play_track would deadlock since asyncio.Lock isn't reentrant."""
        ctrl = await self._ensure_controller()
        track = await get_track(track_id)
        if not track:
            raise LookupError(f"Track {track_id} not in library")

        src_ext = (track.path or "").rsplit(".", 1)[-1].lower()
        src_codec = _NORMALISE_SRC.get(src_ext, src_ext)

        target_codec, needs_transcode = cast_codecs.negotiate_codec(
            source_codec   = src_codec,
            renderer_caps  = self._caps,
            protocol       = self.target.protocol,
            user_pref      = self.state.user_pref,
        )

        # DSD source — pin target sample rate to 88.2 kHz so the renderer
        # actually accepts the PCM stream (ffmpeg's default for 11.2 MHz
        # DSD256 is 352.8 kHz, which no consumer renderer decodes).
        forced_sr: int | None = None
        if src_codec in ("dsf", "dff", "wsd"):
            forced_sr = 88200

        url = cast_tokens.build_stream_url(
            base_url     = base_url or await _server_base_url(),
            track_id     = track_id,
            track_meta   = _track_meta(track),
            codec        = target_codec if needs_transcode else None,
            sample_rate  = forced_sr,
            subsong      = int(subsong or 0),
            user_id      = user_id,
            target_id    = self.target.id,
        )
        spec = cast_codecs.CODECS.get(target_codec)
        content_type = spec.content_type if spec else "audio/mpeg"

        # Build a DIDL-Lite payload only for DLNA — Cast / AirPlay
        # take their own metadata dicts.
        didl = ""
        if self.target.protocol == "dlna":
            from soniqboom.core.cast_dlna import build_didl_lite
            protocol_info = f"http-get:*:{content_type}:{cast_codecs.content_features(target_codec)}"
            didl = build_didl_lite(
                track_id       = track_id,
                title          = getattr(track, "title", "") or "",
                artist         = getattr(track, "artist", "") or "",
                album          = getattr(track, "album", "") or "",
                album_art_url  = "",
                duration_s     = float(getattr(track, "duration", 0) or 0),
                stream_url     = url,
                protocol_info  = protocol_info,
            )

        # Dispatch to the right controller signature.
        if self.target.protocol == "dlna":
            await ctrl.play(stream_url=url, didl_metadata=didl)
        elif self.target.protocol == "cast":
            await ctrl.play(
                stream_url    = url,
                content_type  = content_type,
                title         = getattr(track, "title", "") or "",
                artist        = getattr(track, "artist", "") or "",
                album         = getattr(track, "album", "") or "",
                duration_s    = float(getattr(track, "duration", 0) or 0),
            )
        elif self.target.protocol == "airplay":
            await ctrl.play(
                stream_url    = url,
                content_type  = content_type,
                title         = getattr(track, "title", "") or "",
                artist        = getattr(track, "artist", "") or "",
                album         = getattr(track, "album", "") or "",
            )

        self.state.delivered_codec = target_codec
        self.state.source_codec    = src_codec
        self.state.transcode_used  = needs_transcode
        self.state.touch()

        # Fire lookahead prewarm for N+1 / N+2 in the queue (if we
        # have one).  Bounded — the helper is a no-op for tracks the
        # cache already holds, so spamming is cheap.  Schedule
        # asynchronously so the user-facing /play response doesn't
        # block on the prewarm setup.  Strong-ref via _prewarm_tasks
        # so a stressed loop can't GC the task before it completes.
        _pt = asyncio.create_task(self._prewarm_lookahead(track_id))
        self._prewarm_tasks.add(_pt)
        _pt.add_done_callback(self._prewarm_tasks.discard)

        return {
            "stream_url":       url,
            "delivered_codec":  target_codec,
            "source_codec":     src_codec,
            "transcode":        needs_transcode,
            "content_type":     content_type,
        }

    async def _prewarm_lookahead(self, current_track_id: str) -> None:
        """Speculatively render the next N tracks in the cast queue
        so the renderer's GET on track N+1 hits a warm cache.

        Mirrors player.js's lookahead loop (which already powers the
        web UI via /api/stream/{id}/prewarm).  The renderer's
        ``/cast/{token}`` GET will then short-circuit through the
        ``cached_path is not None`` branch in cast_stream.cast_stream,
        which range-serves the existing on-disk WAV at < 100 ms first
        byte — same UX as a hot replay.

        No-op when the queue is empty or has a single item; bounded
        by ``_PREWARM_WINDOW`` so a 200-track queue load doesn't
        spawn 200 simultaneous renders.
        """
        if not self.state.queue:
            return
        # Find current track's position in the queue (it may have moved
        # since /queue was last set, or play_track was called outside
        # a queue context).
        try:
            cur_idx = next(
                i for i, q in enumerate(self.state.queue)
                if q.track_id == current_track_id
            )
        except StopIteration:
            return
        upcoming = self.state.queue[cur_idx + 1 : cur_idx + 1 + _PREWARM_WINDOW]
        if not upcoming:
            return

        # Late imports so a transient init failure in stream.py doesn't
        # take down the cast session — prewarm is best-effort.
        try:
            from soniqboom.api.stream import (
                _SID_EXTS, _MIDI_EXTS, _TRACKER_EXTS, _UADE_EXTS, _HVL_EXTS,
                _GME_EXTS_STREAM, _DSD_EXTS, NATIVE,
                _render_sid, _render_midi, _render_tracker, _render_uade,
                _render_hvl, _render_gme, _render_to_transcoded_flac,
            )
            from soniqboom.core.conversion_cache import (
                _cache_key as _ck,
                start_background_render,
                is_cache_ready,
            )
            from soniqboom.config import settings as _settings, get_active_soundfont
        except Exception:
            log.exception("cast prewarm: import of render helpers failed")
            return

        for item in upcoming:
            try:
                track = await get_track(item.track_id)
                if not track:
                    continue
                src_ext = "." + (track.path.rsplit(".", 1)[-1].lower())
                src_codec = src_ext.lstrip(".")

                # Skip native pass-through formats — they don't need
                # prewarm (range-served straight off disk).  EXCEPT
                # when the negotiator would transcode anyway (e.g.
                # FLAC source + MP3-only renderer caps).
                target_codec, needs_transcode = cast_codecs.negotiate_codec(
                    source_codec  = src_codec,
                    renderer_caps = self._caps,
                    protocol      = self.target.protocol,
                    user_pref     = self.state.user_pref,
                )
                if not needs_transcode and src_ext in NATIVE:
                    continue

                # Build the cache-key matching what cast_stream / the
                # main stream handler use.  If the entry is already
                # cached (or in-flight), start_background_render is
                # a fast no-op.
                if src_ext in _SID_EXTS:
                    target_dur = int(getattr(_settings, "sid_default_duration", 180))
                    ck = _ck(
                        track_id=item.track_id, format_type="sid",
                        subsong=int(item.subsong or 0), duration=target_dur,
                    )
                    if await is_cache_ready(ck):
                        continue
                    path = Path(track.path)
                    await start_background_render(
                        ck, "sid",
                        lambda p=path, ss=int(item.subsong or 0), d=target_dur:
                            _render_sid(p, subsong=ss, duration=d),
                    )
                elif src_ext in _MIDI_EXTS:
                    sf = get_active_soundfont()
                    ck = _ck(
                        track_id=item.track_id, format_type="midi",
                        soundfont_path=str(sf) if sf else "",
                    )
                    if await is_cache_ready(ck):
                        continue
                    path = Path(track.path)
                    await start_background_render(
                        ck, "midi",
                        lambda p=path: _render_midi(p),
                    )
                elif src_ext in _HVL_EXTS:
                    # HivelyTracker — bundled hvl2wav (uade/openmpt can't
                    # decode HVL).  Checked before the uade + tracker branches.
                    ck = _ck(
                        track_id=item.track_id, format_type="hvl",
                        subsong=int(item.subsong or 0),
                    )
                    if await is_cache_ready(ck):
                        continue
                    path = Path(track.path)
                    await start_background_render(
                        ck, "hvl",
                        lambda p=path, ss=int(item.subsong or 0):
                            _render_hvl(p, subsong=ss),
                    )
                elif src_ext in _UADE_EXTS:
                    # AHX — uade123, not openmpt123.  Checked before the
                    # tracker branch (same priority as the foreground path
                    # in stream.py).
                    ck = _ck(
                        track_id=item.track_id, format_type="uade",
                        subsong=int(item.subsong or 0),
                    )
                    if await is_cache_ready(ck):
                        continue
                    path = Path(track.path)
                    await start_background_render(
                        ck, "uade",
                        lambda p=path, ss=int(item.subsong or 0):
                            _render_uade(p, subsong=ss),
                    )
                elif src_ext in _TRACKER_EXTS:
                    ck = _ck(
                        track_id=item.track_id, format_type="tracker",
                        subsong=int(item.subsong or 0),
                    )
                    if await is_cache_ready(ck):
                        continue
                    path = Path(track.path)
                    await start_background_render(
                        ck, "tracker",
                        lambda p=path, ss=int(item.subsong or 0):
                            _render_tracker(p, subsong=ss),
                    )
                elif src_ext in _GME_EXTS_STREAM:
                    ck = _ck(
                        track_id=item.track_id, format_type="gme",
                        subsong=int(item.subsong or 0),
                    )
                    if await is_cache_ready(ck):
                        continue
                    path = Path(track.path)
                    await start_background_render(
                        ck, "gme",
                        lambda p=path, ss=int(item.subsong or 0):
                            _render_gme(p, subsong=ss),
                    )
                else:
                    # ffmpeg-native sources that still need transcoding
                    # to match the renderer's codec (FLAC→MP3, DSD→FLAC,
                    # ALAC→MP3, etc.).  Same cache namespace as the
                    # main stream handler so a foreground play hits
                    # the warmed entry.
                    #
                    # Audio-2 P0: DSD sources have their target_sample_rate
                    # forced to 88200 by self.play (line 226-231 here),
                    # and the cast_stream layer threads that forced value
                    # into _ck.  Without matching it in the prewarm key
                    # the entry we prewarm at "ar0" never serves the
                    # foreground play at "ar88200" — every DSD cold start
                    # re-renders despite the N+1 prewarm "completing".
                    _prewarm_forced_sr: int | None = None
                    if src_ext in ("dsf", "dff", "wsd"):
                        _prewarm_forced_sr = 88200
                    ck = _ck(
                        track_id=item.track_id, format_type="transcoded",
                        codec=target_codec, target_rate=_prewarm_forced_sr,
                    )
                    if await is_cache_ready(ck):
                        continue
                    path = Path(track.path)
                    src_dur = float(getattr(track, "duration", 0) or 0) or None
                    await start_background_render(
                        ck, "transcoded",
                        lambda p=path, c=target_codec, d=src_dur, sr=_prewarm_forced_sr:
                            _render_to_transcoded_flac(
                                p, codec=c, source_duration=d, target_rate=sr,
                            ),
                    )
            except Exception:
                log.exception(
                    "cast prewarm: skipping track %s after exception",
                    item.track_id,
                )

    async def pause(self) -> None:
        async with self._lock:
            ctrl = await self._ensure_controller()
            await ctrl.pause()
            self.state.touch()

    async def resume(self) -> None:
        async with self._lock:
            ctrl = await self._ensure_controller()
            await ctrl.resume()
            self.state.touch()

    async def stop(self) -> None:
        async with self._lock:
            ctrl = await self._ensure_controller()
            await ctrl.stop()
            self.state.touch()

    async def seek(self, *, seconds: float) -> None:
        async with self._lock:
            ctrl = await self._ensure_controller()
            await ctrl.seek(seconds=seconds)
            self.state.touch()

    async def position(self) -> dict:
        async with self._lock:
            ctrl = await self._ensure_controller()
            self.state.touch()
            try:
                return await ctrl.position()
            except Exception:
                return {}

    async def queue_load(
        self,
        items: list[QueueItem],
        *,
        user_id: str | int | None = None,
        base_url: str | None = None,
    ) -> None:
        """Replace the device queue with ``items``.

        For Cast (which has a real queue API) this issues a single
        QUEUE_LOAD.  For DLNA / AirPlay we stash the queue locally
        and use the lookahead prewarm to make track-to-track
        transitions feel gapless even without a native queue."""
        async with self._lock:
            ctrl = await self._ensure_controller()
            base = base_url or await _server_base_url()
            urls_meta: list[dict] = []
            for it in items:
                track = await get_track(it.track_id)
                if not track:
                    continue
                src_ext = (track.path or "").rsplit(".", 1)[-1].lower()
                src_codec = _NORMALISE_SRC.get(src_ext, src_ext)
                target_codec, needs_transcode = cast_codecs.negotiate_codec(
                    source_codec  = src_codec,
                    renderer_caps = self._caps,
                    protocol      = self.target.protocol,
                    user_pref     = self.state.user_pref,
                )
                url = cast_tokens.build_stream_url(
                    base_url    = base,
                    track_id    = it.track_id,
                    track_meta  = _track_meta(track),
                    codec       = target_codec if needs_transcode else None,
                    subsong     = int(it.subsong or 0),
                    user_id     = user_id,
                    target_id   = self.target.id,
                )
                spec = cast_codecs.CODECS.get(target_codec)
                urls_meta.append({
                    "stream_url":     url,
                    "content_type":   spec.content_type if spec else "audio/mpeg",
                    "title":          it.title or getattr(track, "title", "") or "",
                    "artist":         it.artist or getattr(track, "artist", "") or "",
                    "album":          it.album or getattr(track, "album", "") or "",
                    "album_art_url":  it.album_art_url,
                    "duration_s":     it.duration_s or float(getattr(track, "duration", 0) or 0),
                })

            self.state.queue = list(items)
            self.state.queue_index = 0

            # Fire lookahead prewarm starting from index 0 so the
            # first AND subsequent tracks have warm cache by the time
            # the renderer fetches them.  We don't await — the
            # foreground play call below races ahead to give the
            # renderer audio immediately, while the background renders
            # of N+1 / N+2 complete in parallel.  Strong-ref the task
            # — see __init__ comment on _prewarm_tasks.
            if items:
                _pt = asyncio.create_task(self._prewarm_lookahead(items[0].track_id))
                self._prewarm_tasks.add(_pt)
                _pt.add_done_callback(self._prewarm_tasks.discard)

            if self.target.protocol == "cast" and urls_meta:
                await ctrl.queue_load(urls_meta)
            elif urls_meta:
                # First item plays immediately; rest are managed by us.
                first = urls_meta[0]
                if self.target.protocol == "dlna":
                    from soniqboom.core.cast_dlna import build_didl_lite
                    didl = build_didl_lite(
                        track_id      = items[0].track_id,
                        title         = first["title"],
                        artist        = first["artist"],
                        album         = first["album"],
                        album_art_url = first["album_art_url"],
                        duration_s    = first["duration_s"],
                        stream_url    = first["stream_url"],
                        protocol_info = f'http-get:*:{first["content_type"]}:',
                    )
                    await ctrl.play(stream_url=first["stream_url"], didl_metadata=didl)
                elif self.target.protocol == "airplay":
                    await ctrl.play(
                        stream_url   = first["stream_url"],
                        content_type = first["content_type"],
                        title        = first["title"],
                        artist       = first["artist"],
                        album        = first["album"],
                    )

            self.state.touch()

    async def queue_next(
        self,
        *,
        user_id: str | int | None = None,
        base_url: str | None = None,
    ) -> dict | None:
        """Advance to the next queued track manually (for renderers
        without native queue support).  Cast handles this itself via
        the QUEUE_LOAD ``autoplay=true`` flag, so this is a no-op
        there.

        Held under self._lock for the duration — uses _play_track_locked
        rather than re-entering play_track (asyncio.Lock isn't
        reentrant, the old release-then-reacquire dance race'd against
        any concurrent set_user_pref / queue_load).
        """
        async with self._lock:
            ctrl = await self._ensure_controller()
            self.state.touch()
            if self.target.protocol == "cast":
                await ctrl.queue_next()
                return None
            if not self.state.queue:
                return None
            self.state.queue_index += 1
            if self.state.queue_index >= len(self.state.queue):
                return None
            item = self.state.queue[self.state.queue_index]
            return await self._play_track_locked(
                track_id=item.track_id, user_id=user_id, base_url=base_url,
                subsong=int(item.subsong or 0),
            )

    async def set_user_pref(self, pref: str) -> None:
        if pref not in {"auto", "force-mp3", "force-flac", "force-original"}:
            raise ValueError(f"unknown user_pref: {pref!r}")
        async with self._lock:
            self.state.user_pref = pref
            self.state.touch()

    async def close(self) -> None:
        async with self._lock:
            if self._controller is not None:
                try:
                    await self._controller.disconnect()
                except Exception:
                    log.exception("disconnect failed for %s", self.target.id)
                self._controller = None
            self._caps = None


# ── Process-global session registry ────────────────────────────────────────

_sessions: dict[str, CastSession] = {}
_sessions_lock = asyncio.Lock()


async def get_session(target: CastTarget) -> CastSession:
    """Get-or-create a session for ``target``.  Idempotent."""
    async with _sessions_lock:
        s = _sessions.get(target.id)
        if s is None:
            s = CastSession(target)
            _sessions[target.id] = s
        return s


async def get_session_by_id(target_id: str) -> CastSession | None:
    async with _sessions_lock:
        return _sessions.get(target_id)


async def list_sessions() -> list[CastSession]:
    async with _sessions_lock:
        return list(_sessions.values())


async def close_session(target_id: str) -> None:
    async with _sessions_lock:
        s = _sessions.pop(target_id, None)
    if s is not None:
        await s.close()


async def reap_idle_sessions() -> int:
    """Close sessions whose last_activity is older than _IDLE_TIMEOUT_S.
    Returns the number reaped.  Cheap to call frequently; intended for
    a periodic background task."""
    now = time.time()
    dead: list[CastSession] = []
    async with _sessions_lock:
        for tid, s in list(_sessions.items()):
            if now - s.state.last_activity > _IDLE_TIMEOUT_S:
                dead.append(_sessions.pop(tid))
    for s in dead:
        try:
            await s.close()
        except Exception:
            log.exception("reap close failed")
    return len(dead)


# ── Helpers ────────────────────────────────────────────────────────────────

# Map library "format" strings → canonical lowercase codec.
_NORMALISE_SRC = {
    "mp3":  "mp3",
    "flac": "flac",
    "wav":  "wav",
    "ogg":  "ogg",
    "opus": "opus",
    "m4a":  "m4a",   # may be ALAC or AAC; cast_codecs.negotiate_codec handles
    "aac":  "aac",
    "dsf":  "dsf",
    "dff":  "dff",
    "wsd":  "wsd",
}


def _track_meta(track) -> dict:
    return {
        "title":  getattr(track, "title", "") or "",
        "artist": getattr(track, "artist", "") or "",
        "album":  getattr(track, "album", "") or "",
        "format": getattr(track, "format", "") or "",
    }


_BASE_URL_CACHE: dict[str, Any] = {"value": "", "ts": 0.0}


async def _server_base_url() -> str:
    """Best LAN IP for the renderer to call back to.

    Cached for 60 s — interface discovery isn't free and the network
    state doesn't change between songs.
    """
    now = time.time()
    if now - _BASE_URL_CACHE["ts"] < _BASE_URL_TTL_S and _BASE_URL_CACHE["value"]:
        return _BASE_URL_CACHE["value"]
    from soniqboom.config import settings
    port = int(getattr(settings, "port", 8080) or 8080)
    ip = await asyncio.to_thread(_pick_lan_ip)
    base = f"http://{ip}:{port}"
    _BASE_URL_CACHE["value"] = base
    _BASE_URL_CACHE["ts"]    = now
    return base


def _pick_lan_ip() -> str:
    """Pick a non-loopback IPv4 reachable from the LAN.  Falls back to
    127.0.0.1 only when no other interface is available — renderers
    typically can't dial that back."""
    try:
        # Trick: open a UDP socket to a public address (no packets sent
        # since UDP is connectionless), then read the local endpoint
        # the kernel picked.  Works on macOS + Linux + most BSDs.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
    except OSError:
        pass
    return "127.0.0.1"
