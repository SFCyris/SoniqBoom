# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Anonymous byte-server for Cast / AirPlay / DLNA renderers.

Mounted at ``/cast/{token}/{filename}`` (outside ``/api/*`` so the
session-cookie middleware doesn't gate it).  Auth is the signed token
in the path — see ``soniqboom.core.cast_tokens``.

Two delivery modes:

  • **Cached fast-path** — when the requested ``(track, codec, bitrate,
    sr)`` tuple already lives in the conversion cache (or is a native
    source like FLAC/MP3 played natively).  We range-serve from disk
    via ``stream_track``.  First byte: < 100 ms.

  • **Stream-as-render** — when the tuple is cold AND the requesting
    renderer accepts chunked transfer-encoding.  We pipe ffmpeg
    stdout into a StreamingResponse; the client gets the first byte
    in < 500 ms even for DSD → MP3 conversions that take seconds in
    block-then-serve mode.

The handler also emits DLNA-spec response headers so cheap TV stacks
accept the stream (they sniff Content-Type from the URL extension
plus contentFeatures.dlna.org; both must be present).

Every successful play is timed via ``cast_telemetry.CastTimer`` so
the p95 first-byte dashboard stays honest.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from soniqboom.core import cast_telemetry, cast_tokens
from soniqboom.core.cast_codecs import CODECS, dlna_response_headers
from soniqboom.core.cast_pipe import render_stream
from soniqboom.core.conversion_cache import (
    _cache_key as _ck,
    _cache_path as _ccp,
    get_cached,
    register_existing as _register_existing,
    pin as _conv_pin,
    unpin as _conv_unpin,
)
from soniqboom.core.data import get_track
from soniqboom.config import settings

log = logging.getLogger(__name__)

# IMPORTANT: this router is mounted at the app root, NOT under /api,
# so the cookie-auth middleware (main.py:require_auth_on_api) leaves
# it alone.  Auth = signed token in the URL.
router = APIRouter(tags=["cast"])


# ── Helper: NATIVE-extension fast path ────────────────────────────────────
# Mirrors the small fast-path inside stream_track so we can skip the
# whole "inflight WAV / streaming-transcode" decision tree when the
# source codec equals the requested codec.

_NATIVE_EXTS = {".mp3", ".flac", ".wav", ".ogg", ".opus"}


def _ext_for(track_path: str) -> str:
    return Path(track_path.split("::")[-1] if "::" in track_path else track_path).suffix.lower()


def _safe_header_filename(raw: str) -> str:
    """Sanitize a user-controlled filename for reflection into
    ``Content-Disposition``.  The {filename} URL path part is
    URL-decoded by FastAPI — without sanitization a malicious URL
    like ``/cast/<tok>/x%22%0d%0aSet-Cookie:%20x=y.mp3`` could inject
    headers / split the response.  Strip CR/LF/quotes/backslash and
    cap length; replace controls + non-ASCII with ``_``."""
    if not raw:
        return "track.bin"
    cleaned = []
    for ch in raw:
        cp = ord(ch)
        if cp < 0x20 or cp == 0x7F:        # control chars + DEL
            cleaned.append("_")
        elif ch in ('"', "\\", "\r", "\n"):
            cleaned.append("_")
        elif not ch.isascii():
            cleaned.append("_")
        else:
            cleaned.append(ch)
    out = "".join(cleaned)[:128].strip()
    return out or "track.bin"


# ── Main route ────────────────────────────────────────────────────────────

@router.get("/cast/{token}/{filename}")
async def cast_stream(
    token: str,
    filename: str,
    request: Request,
):
    """Serve a track's bytes to a Cast / AirPlay / DLNA renderer.

    ``filename`` is cosmetic (shows up in some renderers' "Now Playing"
    strings) — we ignore everything except its extension, which we
    use to pick the Content-Type and contentFeatures.dlna.org header.
    """
    claims = cast_tokens.verify_token(token)
    if claims is None:
        # Indistinguishable response for "bad signature" / "expired" /
        # "tampered" — same 404 every time so a probe can't enumerate.
        raise HTTPException(404, "Stream link no longer valid.")

    remote_ip = request.client.host if request.client else None
    if not cast_tokens.replay_ok(claims, remote_ip):
        # log.debug rather than .warning — port-scanners hitting bad URLs
        # otherwise drive log volume linearly with the attack rate.
        log.debug(
            "cast: replay rejected for token jti=%s from ip=%s (first-seen elsewhere)",
            claims.get("jti"), remote_ip,
        )
        raise HTTPException(404, "Stream link no longer valid.")

    # Re-auth: the token claims encode the user_id that minted it.  If
    # that user has been disabled / deleted since the token was issued,
    # refuse to keep streaming.  This closes a 30-min window where a
    # token outlived the user record.
    uid = claims.get("uid")
    if uid:
        try:
            from soniqboom.core.users import get_user_store
            store = get_user_store()
            if store.has_any():
                # Look up by id then by username (cast_tokens accepts
                # either — uid may be set from user.id or user.username).
                u = None
                if hasattr(store, "get_by_id"):
                    try: u = store.get_by_id(str(uid))
                    except Exception: u = None
                if u is None and hasattr(store, "get_by_username"):
                    try: u = store.get_by_username(str(uid))
                    except Exception: u = None
                if u is None or not getattr(u, "enabled", True):
                    raise HTTPException(404, "Stream link no longer valid.")
        except HTTPException:
            raise
        except Exception:
            # If the user store is down, we'd rather degrade to "trust
            # the token" than fail open every cast request — the token
            # was already signature-validated.
            log.exception("cast: user-store re-auth check failed; trusting token")

    # Track existence: 410 Gone is the right code — the resource WAS
    # valid (token minted against it) but the underlying track is no
    # longer in the library.  Distinguishes from "bad token" 404 so
    # the controlling client knows whether to retry.
    track_id = claims["tid"]
    track = await get_track(track_id)
    if track is None:
        raise HTTPException(410, "Track no longer in library.")

    # Sanitise the filename before reflecting into Content-Disposition.
    filename = _safe_header_filename(filename)

    target_codec = (claims.get("c") or "").lower() or None
    target_bitrate = int(claims.get("br") or 0) or None
    target_sample_rate = int(claims.get("sr") or 0) or None
    # Bit-depth flows through the token claim (set when the renderer
    # advertises audio/L24 capability).  Defaults to 16 for the WAV
    # path so dither runs — see cast_pipe._build_cmd.
    target_bit_depth = int(claims.get("bd") or 0) or None

    src_ext = _ext_for(track.path)
    src_codec = src_ext.lstrip(".") if src_ext else "?"
    proto = claims.get("proto") or "http"  # used only for telemetry tagging
    target_for_tel = target_codec or src_codec

    # ── Cached / native fast-path ────────────────────────────────────────
    # If the target codec is None (native delivery) OR matches the source,
    # OR a cached transcode already exists, defer to stream_track which
    # handles Range serving + the existing in-flight WAV cache.
    use_native_fastpath = (
        target_codec is None
        or target_codec == src_codec
        or (src_ext in _NATIVE_EXTS and not target_codec)
    )

    cached_path: Path | None = None
    if not use_native_fastpath and target_codec:
        try:
            ck = _ck(
                track_id=track_id, format_type="transcoded",
                codec=target_codec, target_rate=target_sample_rate,
            )
            cached_path = await get_cached(ck)
        except Exception:
            cached_path = None

    with cast_telemetry.CastTimer(
        protocol=proto,
        target_id=str(claims.get("tg") or "-"),
        source=src_codec,
        target=target_for_tel,
    ) as t:
        if use_native_fastpath or cached_path is not None:
            # Delegate to the proven /api/stream handler — it knows
            # how to range-serve, handle remote FTP/SMB sources, and
            # render SID / MIDI / tracker formats.
            from soniqboom.api.stream import (
                stream_track,
                _set_cast_internal_bypass,
                _reset_cast_internal_bypass,
            )
            t.mark_first_byte()  # native path: first byte effectively immediate
            # The signed token in the URL has already been validated above.
            # We tell stream_track to skip its own _require_stream_auth via a
            # ContextVar (set/reset around the call) — this can NOT be set by
            # any inbound request, closing the anonymous-stream bypass the
            # previous "kwarg toggle" approach would have created.
            bypass_token = _set_cast_internal_bypass(True)
            try:
                response = await stream_track(
                    track_id=track_id,
                    request=request,
                    seek=0.0,
                    subsong=0,
                    file_path=None,
                    target_format=target_codec,
                    max_bitrate_kbps=target_bitrate or 0,
                    target_sample_rate=target_sample_rate or 0,
                    force_transcode=False,
                    sb_session=None,
                    u=None, p=None, s=None, t=None,
                )
            finally:
                _reset_cast_internal_bypass(bypass_token)
            # Attach DLNA headers based on what we actually delivered.
            delivered = target_codec or src_codec
            for k, v in dlna_response_headers(delivered).items():
                if v:
                    response.headers[k] = v
            # ContentDisposition with the cosmetic filename — some
            # renderers display this on the TV.
            response.headers.setdefault(
                "Content-Disposition",
                f'inline; filename="{filename}"',
            )
            t.outcome = "played"
            t.set_bytes(int(response.headers.get("content-length") or 0))
            return response

        # ── Stream-as-render path ─────────────────────────────────────
        # ffmpeg → chunked stdout → renderer.  Side-write to cache so
        # the second play hits the fast path.
        #
        # Resource-lifecycle invariant: every pin we acquire on the way
        # down has a matching unpin in the response's ``_counting_gen``
        # finally clause.  If we throw between pin and response return
        # (e.g. prepare_source_for_stream raises), the per-pin
        # try/except below releases without surfacing as a leak.
        path_obj = Path(track.path)
        # ZIP-contained tracks (modarchive_2007.zip::inner.mod and
        # friends — 42% of the library on this user's box).  Extract
        # the inner file to the stable on-disk cache the existing
        # /api/stream/ path uses, then operate on the extracted path
        # downstream.  Without this, ``path_obj.exists()`` returns
        # False for any ZIP-contained track and the cast call 410s.
        _zip_track_id_for_unpin: str | None = None
        if "::" in track.path:
            from soniqboom.api.stream import (
                _get_or_extract_zip_member,
                _zip_pin, _zip_unpin,
            )
            extracted = await _get_or_extract_zip_member(track.path, track_id)
            if extracted is None:
                raise HTTPException(410, "ZIP archive not found or unreadable.")
            path_obj = extracted
            # Pin so the LRU-eviction sweeper can't unlink the inner
            # file mid-stream.  We unpin after the response generator
            # completes (the StreamingResponse's body iterator will
            # do that — same trick the inner /api/stream/ path uses).
            #
            # QA-1 P0 flagged a leak window: if any of the calls between
            # this pin and the StreamingResponse return raise, the
            # pin sticks because the unpin lives inside _counting_gen
            # which never runs.  The handler-level try/except below
            # mirrors the pin into a function-scope cleanup that fires
            # on early-raise; the success path defers to _counting_gen.
            _zip_pin(track_id)
            _zip_track_id_for_unpin = track_id
        elif track.path.startswith(("smb://", "ftp://")):
            # Remote source — pull through cache first (blocking, but
            # the existing remote_cache.fetch already handles streaming
            # the FTP/SMB pull in a thread).
            from soniqboom.core.filesource import get_source, parse_remote_path
            from soniqboom.core.remote_cache import get_cache
            scan_root, remote_path = parse_remote_path(track.path)
            source = get_source(scan_root) if scan_root else None
            if source is None:
                raise HTTPException(503, "Network share unavailable.")
            import asyncio
            loop = asyncio.get_running_loop()
            try:
                path_obj = await loop.run_in_executor(
                    None, get_cache().fetch, scan_root, remote_path, source,
                )
            except Exception as exc:
                log.warning("cast: remote fetch failed for %s: %s", track.path, exc)
                raise HTTPException(502, "Could not fetch remote source.")
        elif not path_obj.exists():
            raise HTTPException(410, "Source file no longer on disk.")

        # ── Render rendered formats (SID/MIDI/tracker/GME) to WAV ──
        # before feeding to ffmpeg.  Without this step, cast_pipe would
        # hand ffmpeg a binary blob it can't parse and the renderer
        # would receive zero bytes — the symptom users see as "the
        # chiptune just doesn't play on the speaker".  prepare_source
        # is a no-op for ffmpeg-native sources, so the cost is pay-
        # per-rendered-format-track-played (and only on cache miss).
        #
        # We wrap the whole setup in try/except so any early-raise after
        # the ZIP pin (above) releases the pin instead of leaking it —
        # QA-1 P0 flagged the gap between pin and the response generator's
        # finally block.
        from soniqboom.core.cast_render import (
            prepare_source_for_stream, is_rendered_format,
        )
        target_subsong = int(claims.get("sn") or 0)
        was_rendered = is_rendered_format(_ext_for(track.path))
        # Cache-key for the rendered source (SID/MIDI/tracker/GME WAV).
        # Pinned during stream so a concurrent N+1/N+2 prewarm cannot
        # evict it mid-pump.  Audio-2 P0: without this pin, eviction
        # under cache pressure unlinks the WAV ffmpeg is reading from
        # — Linux/macOS survive via open-fd semantics; Windows breaks.
        _rendered_ck: str | None = None
        try:
            if was_rendered:
                try:
                    path_obj, _eff_src = await prepare_source_for_stream(
                        track_id   = track_id,
                        track_path = str(path_obj) if not track.path.startswith(("smb://", "ftp://")) else track.path,
                        subsong    = target_subsong,
                    )
                    # Phase mark — render done, transcode about to start.
                    # Audio-2 P1: dashboard now splits renderer slowness
                    # from ffmpeg slowness.
                    t.mark_render_done()
                except FileNotFoundError:
                    raise HTTPException(410, "Source file no longer on disk.")
                except RuntimeError as exc:
                    # Missing renderer binary (sidplayfp / fluidsynth / openmpt123 /
                    # libgme) — distinct from "device unreachable", surface as
                    # 501 Not Implemented so the controller UI can suggest the
                    # install hint instead of saying "speaker offline".
                    log.info("cast: rendered-format prep failed: %s", exc)
                    raise HTTPException(501, str(exc))
                except HTTPException:
                    raise
                except Exception:
                    log.exception("cast: prepare_source_for_stream failed for %s", track_id)
                    raise HTTPException(500, "Failed to render the source for cast streaming.")
                # Best-effort: figure out which cache key prepare_source
                # populated so we can pin it.  cast_render uses one of
                # several format_types depending on the source codec.
                _rendered_ck = _guess_rendered_ck(track_id, src_ext, target_subsong)
                if _rendered_ck:
                    try:
                        _conv_pin(_rendered_ck)
                    except Exception:
                        _rendered_ck = None

            cache_sink: Path | None = None
            ck: str | None = None
            try:
                # Cache-key for the eventual side-write.  MUST match the
                # key cast_session._prewarm_lookahead builds, otherwise
                # the prewarmed entry never serves the foreground play.
                # target_sample_rate is forced to 88200 for DSD by the
                # token-issuing layer (cast_session.play.forced_sr), so
                # we always thread it through here.
                ck = _ck(
                    track_id=track_id, format_type="transcoded",
                    codec=target_codec, target_rate=target_sample_rate,
                )
                cache_sink = _ccp(ck, "transcoded")
            except Exception:
                cache_sink = None
                ck = None

            spec = CODECS.get((target_codec or "mp3").lower())
            content_type = spec.content_type if spec else "audio/mpeg"

            headers = {
                "Accept-Ranges": "none",          # chunked stream — no Range first time
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="{filename}"',
            }
            for k, v in dlna_response_headers(target_codec or "mp3").items():
                if v:
                    headers[k] = v

            # Mark the start of the transcode phase right before we
            # construct the ffmpeg-driving generator — even for native
            # sources this gives a clean baseline for the dashboard.
            t.mark_transcode_started()

            def _mark_first_byte():
                t.mark_first_byte()

            # Callback that registers the side-written file in the
            # conversion-cache index once cast_pipe atomically renames
            # the .partial sink into place.  Without this, second-play
            # range-serving doesn't engage until next server restart —
            # Audio-2 P0.
            _ck_for_register = ck

            async def _on_cache_written(dest_path: Path) -> None:
                if _ck_for_register is None:
                    return
                try:
                    await _register_existing(_ck_for_register, "transcoded", dest_path)
                except Exception:
                    log.exception(
                        "cast: register_existing failed for %s", _ck_for_register,
                    )

            body_gen = render_stream(
                path_obj,
                codec=target_codec or "mp3",
                bitrate_kbps=target_bitrate,
                sample_rate=target_sample_rate,
                bit_depth=target_bit_depth,
                cache_sink=cache_sink,
                cache_register=_on_cache_written if cache_sink is not None else None,
                ffmpeg_path=settings.ffmpeg_path,
                on_first_byte=_mark_first_byte,
            )

            # Count bytes for telemetry without buffering — wrap the generator.
            bytes_sent_box = {"n": 0}

            # Capture for the finally block — closure-scope so the unpin
            # fires whether the stream completed cleanly, the client
            # cancelled, or ffmpeg crashed mid-pump.
            _unpin_tid = _zip_track_id_for_unpin
            _unpin_rendered_ck = _rendered_ck

            async def _counting_gen():
                try:
                    async for chunk in body_gen:
                        bytes_sent_box["n"] += len(chunk)
                        yield chunk
                    t.outcome = "played"
                except Exception:
                    t.outcome = "errored"
                    raise
                finally:
                    t.set_bytes(bytes_sent_box["n"])
                    # Release all pins — order doesn't matter, both are
                    # refcounted.  We catch broadly because cleanup
                    # exceptions inside a finally must not mask the
                    # original error.
                    if _unpin_tid is not None:
                        try:
                            from soniqboom.api.stream import _zip_unpin
                            _zip_unpin(_unpin_tid)
                        except Exception:
                            pass
                    if _unpin_rendered_ck is not None:
                        try:
                            _conv_unpin(_unpin_rendered_ck)
                        except Exception:
                            pass

            response = StreamingResponse(
                _counting_gen(),
                status_code=200,
                media_type=content_type,
                headers=headers,
            )
        except BaseException:
            # Any early-raise BEFORE the response is returned must
            # release the pins; ``_counting_gen`` won't run.
            if _rendered_ck is not None:
                try:
                    _conv_unpin(_rendered_ck)
                except Exception:
                    pass
            if _zip_track_id_for_unpin is not None:
                try:
                    from soniqboom.api.stream import _zip_unpin
                    _zip_unpin(_zip_track_id_for_unpin)
                except Exception:
                    pass
            raise

        return response


# ── Rendered-source cache-key inference ──────────────────────────────────
# cast_render uses different format_types depending on the source.  This
# mirror lets cast_stream pin the right key without importing cast_render's
# internals.  Audio-2 P0 noted that without pinning, an N+1 prewarm's
# _maybe_evict can unlink the rendered WAV mid-stream.

_SID_EXTS = {".sid"}
_MIDI_EXTS = {".mid", ".midi", ".kar"}
_TRACKER_EXTS = {".mod", ".s3m", ".xm", ".it", ".mptm", ".669", ".far", ".mtm"}
_GME_EXTS = {".nsf", ".nsfe", ".spc", ".gbs", ".vgm", ".vgz", ".ay", ".kss", ".sap", ".gym", ".hes"}


def _guess_rendered_ck(track_id: str, src_ext: str, subsong: int) -> str | None:
    """Build the conversion-cache key cast_render would have populated.

    Returns ``None`` for ffmpeg-native sources (no rendered cache entry
    to pin) or unknown extensions."""
    e = (src_ext or "").lower()
    try:
        if e in _SID_EXTS:
            return _ck(track_id=track_id, format_type="sid", subsong=subsong)
        if e in _MIDI_EXTS:
            # Soundfont path defaults to the configured one; cast_render
            # uses settings.midi_soundfont_path at render time.
            from soniqboom.config import settings as _s
            return _ck(
                track_id=track_id, format_type="midi",
                soundfont_path=getattr(_s, "midi_soundfont_path", None),
            )
        if e in _TRACKER_EXTS:
            return _ck(track_id=track_id, format_type="tracker", subsong=subsong)
        if e in _GME_EXTS:
            return _ck(track_id=track_id, format_type="gme", subsong=subsong)
    except Exception:
        return None
    return None
