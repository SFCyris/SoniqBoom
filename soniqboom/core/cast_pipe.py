# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stream-as-render: pipe ffmpeg stdout directly into an HTTP response.

The existing transcode path in stream.py blocks the request until the
full transcode lands in the on-disk cache, THEN range-serves it.  For
short native files (MP3) that's invisible; for a 7-minute DSD source
or a slow FLAC→MP3 it adds 10–30 s of cold-start latency — long
enough for AVPlayer / DLNA renderers to time out before the first
byte arrives.

This module ships a parallel ``ffmpeg → chunked StreamingResponse``
path that:

  • Spawns ffmpeg with stdout=PIPE.
  • Yields chunks to the client as soon as ffmpeg writes them.
  • Side-writes a copy to the conversion cache so the SECOND play is
    range-able and instant.
  • Cleans up on cancel (client disconnect, renderer skip): kills
    ffmpeg, unlinks the half-finished cache file.

The trade-off is no ``Content-Length`` on first play — we don't know
the output size until ffmpeg finishes.  Some renderers (older DLNA
stacks) require Content-Length; for those we fall back to the cached
range-served path.  The cast handler picks the right mode based on
the renderer's reported capabilities (Phase 1).

Concurrency: a second request for the SAME (track, codec, bitrate,
sr) tuple while the first is still in-flight should *attach* to the
existing render rather than starting a duplicate ffmpeg.  We do that
via the same _INFLIGHT_TRANSCODES dict the WAV path uses — that
keeps the two paths in lockstep.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator

log = logging.getLogger(__name__)

# Chunk size for ffmpeg → response pump.  64 KB is the sweet spot:
# smaller chunks burn syscalls, larger chunks delay the first byte
# beyond perceptual budget.
_CHUNK_SIZE = 64 * 1024


# ── ffmpeg encoder capability probe ────────────────────────────────────────
# Audio-1 P0 flagged that we ship the native vorbis encoder (a known-poor
# implementation) even when the user's ffmpeg has libvorbis built in.
# Probe once at module import; cache the result so the per-render hot path
# never forks an extra process.

_LIBVORBIS_AVAILABLE: bool | None = None


def _has_libvorbis(ffmpeg_bin: str) -> bool:
    """Return True iff ``ffmpeg_bin`` exposes the libvorbis encoder.

    Caches the first probe per process — the result is binary-stable for
    the lifetime of the process (the user can't change which ffmpeg is on
    PATH without restarting SoniqBoom)."""
    global _LIBVORBIS_AVAILABLE
    if _LIBVORBIS_AVAILABLE is not None:
        return _LIBVORBIS_AVAILABLE
    try:
        # ``ffmpeg -encoders`` prints one row per encoder; libvorbis only
        # shows up when ffmpeg was compiled --enable-libvorbis.  Limit
        # the wall budget so a missing/hung ffmpeg doesn't stall import.
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=2.5,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        _LIBVORBIS_AVAILABLE = "libvorbis" in out
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        _LIBVORBIS_AVAILABLE = False
    if not _LIBVORBIS_AVAILABLE:
        log.warning(
            "cast_pipe: ffmpeg %r lacks libvorbis — Ogg/Vorbis renders "
            "will use the native (experimental) encoder.  Install an "
            "ffmpeg build with --enable-libvorbis for transparent quality.",
            ffmpeg_bin,
        )
    return _LIBVORBIS_AVAILABLE


# Source extensions that need an aggressive lowpass before the encoder.
# DSD → PCM via ffmpeg's sigma-delta demodulator leaves wideband
# noise above ~24 kHz that bleeds into the lossy encoder, producing
# audible high-frequency hiss on lossy outputs (mp3/ogg/aac).  A
# pre-encode lowpass at 24 kHz puts the noise floor below the encoder's
# psychoacoustic mask.  No-op for FLAC/WAV (no perceptual coder).
_DSD_EXTS = (".dsf", ".dff", ".wsd")


# ── ffmpeg command builders ────────────────────────────────────────────────

def _build_cmd(
    src_path: Path,
    *,
    codec: str,
    bitrate_kbps: int | None,
    sample_rate: int | None,
    ffmpeg_path: str | None,
    protocol: str | None = None,
    bit_depth: int | None = None,
) -> list[str]:
    """Build the ffmpeg command for a streaming transcode.

    ``codec`` is the target codec: mp3 / flac / ogg / opus / aac / wav.
    ``protocol``, when provided, lets us pick the right container per
    protocol family (raw FLAC vs ogg-FLAC; MP4-AAC vs ADTS-AAC).

    Output goes to ``pipe:1`` (stdout).  We pick a container that's
    streamable from byte zero and that the requesting renderer's
    decoder will accept:

      mp3  → -f mp3 -acodec libmp3lame -b:a 320k   (universal lossy)
      flac → -f flac -acodec flac                  (native FLAC — what
              + ``-write_application_metadata 0``    Sonos / Chromecast /
              + ``-flac_block_size 4096``           DLNA TVs actually accept;
                                                    the old "FLAC-in-OGG"
                                                    path was rejected by
                                                    Sonos / most DLNA gear)
      aac  → MP4-in-fragmented-MP4 for AirPlay
              (AppleTV/HomePod need the moov atom +
              esds; ADTS frames are refused over AirPlay 2);
              ADTS for everything else.
      ogg  → -f ogg   -acodec libvorbis
      opus → -f opus  -acodec libopus
      wav  → -f wav   -acodec pcm_s16le  (or s24le when bit_depth==24)
    """
    bin_ = ffmpeg_path or "ffmpeg"
    fmt = codec.lower()
    src_ext = src_path.suffix.lower() if hasattr(src_path, "suffix") else ""
    cmd: list[str] = [
        bin_,
        "-hide_banner", "-loglevel", "error", "-nostats",
        "-y",
        "-i", str(src_path),
        "-vn",
        "-threads", "0",
    ]

    # ── Filter chain ────────────────────────────────────────────────
    # DSD sources: ffmpeg's demodulator leaves wideband ultrasonic noise
    # that bleeds into any lossy psychoacoustic encoder (mp3/aac/ogg/opus).
    # A 24 kHz lowpass keeps the audible spectrum intact while blocking
    # the noise hash from leaking into the encoder's bit budget.  Even
    # for FLAC/WAV outputs this can reduce file size — the modulator
    # noise is uncorrelated and uncompressible.
    filters: list[str] = []
    if src_ext in _DSD_EXTS:
        # Full DSD → PCM filter chain.  The highpass strips DC bias the
        # delta-sigma demodulator leaves on near-silence DSD segments
        # (decodes to a -1.0 rail-pegged constant instead of zero —
        # browsers + OS audio drivers mute that as a DC offset for
        # speaker protection, presenting as silent gaps at the waveform's
        # tall peaks).  Lowpass kills noise-shaped ultrasonic content
        # so it can't intermodulate inside the encoder.  -6 dB gives the
        # remaining transients room.  Verified 2026-05-23 by direct PCM
        # sample analysis of a Setsuna Ogiso DSF.
        filters.append("highpass=f=20")
        filters.append("lowpass=f=24000")
        filters.append("volume=-6dB")
    # Dither when writing 16-bit PCM (only path that needs it — WAV with
    # explicit s16le and the implicit lossy chain going from 24/32-bit
    # source through ffmpeg's internal float pipeline back to 16-bit
    # encoder input).  Triangular high-pass dither is the perceptually
    # cleanest 1-LSB shape per Wannamaker/Vanderkooy.  No-op when the
    # source is already 16-bit (ffmpeg's resampler detects unchanged
    # depth and skips the dither step).
    want_16bit_pcm = fmt == "wav" and not (bit_depth and int(bit_depth) >= 24)
    if want_16bit_pcm:
        # Apply via aresample so the dither pass runs even when no sample
        # rate change was requested.  ``triangular_hp`` is ffmpeg's
        # high-pass triangular (TPDF) variant.
        filters.append("aresample=resampler=swr:dither_method=triangular_hp")
    if filters:
        cmd += ["-af", ",".join(filters)]

    # Sample-rate clamp for lossy encoders — libmp3lame caps at 48 kHz,
    # and ffmpeg's open_encoder call fails (writing zero bytes) when the
    # input rate is 88.2/96/192 kHz.  DSD sources arrive here at 88200
    # (forced by cast_session for renderer compatibility); a renderer
    # that negotiates MP3 would otherwise see "WAV header, no PCM" and
    # play silence — exactly the symptom reported when Amperfy asked
    # for a DSF via OpenSubsonic transcoding.
    eff_sr = int(sample_rate) if sample_rate else 0
    _LOSSY_MAX_SR = {"mp3": 48000, "ogg": 48000, "opus": 48000, "aac": 48000}
    if eff_sr and fmt in _LOSSY_MAX_SR and eff_sr > _LOSSY_MAX_SR[fmt]:
        log.info(
            "cast_pipe: clamping %s output rate %d → %d Hz "
            "(encoder limit; src=%s)",
            fmt, eff_sr, _LOSSY_MAX_SR[fmt], src_ext or "?",
        )
        eff_sr = _LOSSY_MAX_SR[fmt]
    if eff_sr:
        cmd += ["-ar", str(eff_sr)]
    # Lossless: bitrate doesn't apply.  Lossy: cap.  Default lossy
    # bitrate is 320 kbps (transparent-CBR for transcode-from-lossless);
    # libmp3lame's no-flag default is 128 kbps, which the brief
    # explicitly rejected as "MP3 sounds bad" UX.
    if fmt not in ("flac", "wav"):
        eff_br = int(bitrate_kbps) if bitrate_kbps else 320
        cmd += ["-b:a", f"{eff_br}k"]

    if fmt == "mp3":
        cmd += ["-f", "mp3", "-acodec", "libmp3lame"]
    elif fmt == "flac":
        # Native FLAC.  Real-world renderers (Sonos S2, Chromecast
        # Audio, Apple TV via AVPlayer, LG/Samsung DLNA) accept raw
        # FLAC bytes; FLAC-in-OGG was a v0 mistake.
        #
        # Note: do NOT add ffmpeg flags like ``-write_application_metadata``
        # or ``-flac_block_size`` — those names don't exist in mainline
        # ffmpeg and cause every FLAC render to exit rc=8 with
        # "Unrecognized option" before producing a single byte.  Plain
        # ``-f flac -acodec flac`` produces a 33-50 ms first-byte on
        # macOS / Linux dev boxes for typical sources.
        cmd += ["-f", "flac", "-acodec", "flac"]
    elif fmt == "ogg":
        # Prefer libvorbis (Xiph reference encoder, transparent quality
        # at ~192 kbps) over ffmpeg's built-in vorbis encoder (which is
        # marked experimental and produces audibly inferior output even
        # at 320 kbps — Audio-1 P0).  Probe once at module import; if
        # the host ffmpeg lacks libvorbis fall back to native vorbis so
        # the renderer still gets *some* audio rather than nothing.
        if _has_libvorbis(bin_):
            cmd += ["-f", "ogg", "-acodec", "libvorbis"]
        else:
            cmd += ["-f", "ogg", "-acodec", "vorbis", "-strict", "experimental"]
    elif fmt == "opus":
        cmd += ["-f", "opus", "-acodec", "libopus"]
    elif fmt == "wav":
        # 24-bit PCM when requested (used by hi-fi DLNA renderers that
        # advertise audio/L24).  Default to 16-bit for everything else.
        pcm = "pcm_s24le" if (bit_depth and int(bit_depth) >= 24) else "pcm_s16le"
        cmd += ["-f", "wav", "-acodec", pcm]
    elif fmt == "aac":
        proto = (protocol or "").lower()
        if proto == "airplay":
            # Fragmented MP4 — AirPlay 2 demands an M4A-in-MP4 with
            # moov atom up front.  empty_moov + frag_keyframe +
            # default_base_moof produces a streamable MP4 that Apple
            # TV / HomePod will actually play.
            cmd += [
                "-f", "mp4", "-acodec", "aac",
                "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            ]
        else:
            # ADTS-framed AAC for DLNA / Cast — every frame self-syncs.
            cmd += ["-f", "adts", "-acodec", "aac"]
    else:
        log.warning("stream-as-render: unknown codec %r; defaulting to mp3", fmt)
        cmd += ["-f", "mp3", "-acodec", "libmp3lame"]

    cmd += ["pipe:1"]
    return cmd


# ── Async generator: spawn ffmpeg + yield stdout chunks ───────────────────

async def render_stream(
    src_path: Path,
    *,
    codec: str,
    bitrate_kbps: int | None = None,
    sample_rate: int | None = None,
    cache_sink: Path | None = None,
    cache_register=None,
    ffmpeg_path: str | None = None,
    on_first_byte=None,
    protocol: str | None = None,
    bit_depth: int | None = None,
) -> AsyncIterator[bytes]:
    """Yield chunks of transcoded audio as ffmpeg produces them.

    Caller wraps this in a ``StreamingResponse`` (with no
    ``Content-Length`` — chunked transfer-encoding is implied).

    If ``cache_sink`` is set, every chunk is mirrored to that path so
    the second play hits the on-disk cache.  Failed renders unlink the
    sink — no half-written entries leak into the cache index.

    ``on_first_byte`` is invoked exactly once, right before the first
    chunk is yielded.  Used by the telemetry layer to mark the
    "audio actually started arriving" timestamp.

    ``cache_register``, when provided, is invoked with the final
    ``cache_sink`` path after a clean exit + atomic rename succeeds.
    It MUST be an async callable.  Used by the cast-stream layer to
    register the side-written file in the conversion-cache index
    (``conversion_cache.register_existing``) so the next play hits the
    range-served fast path rather than re-running ffmpeg — Audio-2 P0
    found that without registration the "second play is instant" claim
    was broken in-process and only true after a server restart.

    Cancellation safety:

      • Client disconnects     → asyncio.CancelledError propagates
                                  through the ``yield``; ``finally``
                                  block kills ffmpeg + unlinks sink.
      • ffmpeg crashes mid-way → AsyncIterator raises StopAsyncIteration
                                  with no further chunks; caller sees
                                  truncated stream (typically renderer
                                  reports "stream ended early").
      • Server shuts down      → CancelledError as above.
    """
    cmd = _build_cmd(
        src_path, codec=codec, bitrate_kbps=bitrate_kbps,
        sample_rate=sample_rate, ffmpeg_path=ffmpeg_path,
        protocol=protocol, bit_depth=bit_depth,
    )
    log.debug("stream-as-render spawning: %s", " ".join(cmd))

    # ``stderr=DEVNULL`` (rather than PIPE) eliminates the stderr-pipe-
    # backpressure failure mode: with PIPE, ffmpeg blocks once the 64 KB
    # kernel pipe buffer fills with warnings we never read.  We don't
    # need stderr in the happy path; on failure the non-zero exit code
    # already tells us "something went wrong" and the user can re-try.
    # When we genuinely need stderr for diagnosis we drain it via a
    # concurrent task (below).
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Put ffmpeg in its own process group so an emergency
        # kill(-pgid, SIGKILL) reliably takes it down even when we
        # can't await its return (e.g. shutdown SIGKILL path).
        start_new_session=True,
    )

    # Drain stderr concurrently so the pipe never backs up.  We collect
    # the tail for the failure-path log message; on success we just
    # discard.
    async def _stderr_drain():
        try:
            assert proc.stderr is not None
            while True:
                ln = await proc.stderr.readline()
                if not ln:
                    return
                # Keep only the last few lines (bounded memory).
                stderr_tail.append(ln.decode("utf-8", "replace").rstrip())
                if len(stderr_tail) > 20:
                    del stderr_tail[0]
        except Exception:
            return
    stderr_tail: list[str] = []
    stderr_task = asyncio.create_task(_stderr_drain())

    sink_fd: int | None = None
    partial_path: Path | None = None
    if cache_sink is not None:
        try:
            await asyncio.to_thread(cache_sink.parent.mkdir, parents=True, exist_ok=True)
            partial_path = cache_sink.with_suffix(cache_sink.suffix + ".partial")
            sink_fd = await asyncio.to_thread(
                os.open, str(partial_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o644,
            )
        except OSError:
            log.warning("cache sink open failed for %s — streaming without cache", cache_sink)
            sink_fd = None

    clean_exit = False
    first_chunk_seen = False
    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(_CHUNK_SIZE)
            if not chunk:
                # ffmpeg finished writing.
                break
            if not first_chunk_seen:
                first_chunk_seen = True
                if on_first_byte is not None:
                    try:
                        on_first_byte()
                    except Exception:
                        log.exception("on_first_byte callback raised")
            if sink_fd is not None:
                try:
                    await asyncio.to_thread(os.write, sink_fd, chunk)
                except OSError:
                    # Disk full / fs gone — keep streaming to the
                    # client even if the cache write failed.
                    try:
                        await asyncio.to_thread(os.close, sink_fd)
                    except OSError:
                        pass
                    sink_fd = None
                    partial_path = None
            yield chunk

        # Drain ffmpeg's exit code.  Non-zero = mid-way failure.
        rc = await proc.wait()
        if rc == 0:
            clean_exit = True
        else:
            log.warning(
                "stream-as-render ffmpeg rc=%d for %s: %s",
                rc, src_path.name, " | ".join(stderr_tail)[:400],
            )

    finally:
        # ── Kill ffmpeg if still alive (client cancel / our error).
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

        # ── Stop the stderr drainer.
        if not stderr_task.done():
            stderr_task.cancel()
            try:
                await asyncio.wait_for(stderr_task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        # ── Sink fd close.
        if sink_fd is not None:
            try:
                await asyncio.to_thread(os.close, sink_fd)
            except OSError:
                pass

        # ── Promote ``.partial`` → final path on clean exit, else unlink.
        if partial_path is not None:
            if clean_exit and cache_sink is not None:
                try:
                    await asyncio.to_thread(os.replace, str(partial_path), str(cache_sink))
                    log.debug("stream-as-render cached %s", cache_sink.name)
                    # Register the side-written file in the conversion-cache
                    # index so subsequent plays hit the range-served fast
                    # path instead of re-running ffmpeg.  Best-effort —
                    # registration failure leaves the file on disk and the
                    # warmup-from-disk pass at next restart will pick it up.
                    if cache_register is not None:
                        try:
                            await cache_register(cache_sink)
                        except Exception:
                            log.exception(
                                "stream-as-render cache_register raised for %s",
                                cache_sink.name,
                            )
                except OSError as exc:
                    log.warning("cache promote failed for %s: %s", cache_sink, exc)
                    try:
                        await asyncio.to_thread(partial_path.unlink, missing_ok=True)
                    except OSError:
                        pass
            else:
                # Failed or cancelled — wipe partial.
                try:
                    await asyncio.to_thread(partial_path.unlink, missing_ok=True)
                except OSError:
                    pass


# ── First-byte budget helper (for tests) ──────────────────────────────────

async def measure_first_byte_ms(
    src_path: Path,
    *,
    codec: str,
    **kwargs,
) -> int:
    """Spawn the stream-as-render pipeline and return milliseconds
    from spawn to first stdout chunk.  Used by the budget test."""
    started = time.monotonic()
    first_byte_ms: int = -1
    gen = render_stream(src_path, codec=codec, **kwargs)
    try:
        async for chunk in gen:
            if first_byte_ms < 0:
                first_byte_ms = int(round((time.monotonic() - started) * 1000))
            break
    finally:
        # aclose() drives the generator's finally clause synchronously,
        # so ffmpeg is killed + cache sink unlinked before we return.
        # The old "iterate to drain" approach left cleanup at GC time.
        try:
            await gen.aclose()
        except Exception:
            pass
    return first_byte_ms if first_byte_ms >= 0 else -1
