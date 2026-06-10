# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Audio streaming — serves native-browser formats directly; transcodes the rest via ffmpeg.

Also supports rendered (instruction-based) formats: SID, MIDI, and tracker modules.
These are converted to PCM/WAV on-the-fly via external CLI tools (sidplayfp,
FluidSynth, openmpt123).

On-demand ingestion: if a track_id isn't in the store but a ``path`` query
parameter is provided, the file is ingested on the fly (metadata extracted,
track upserted to store) so that playback succeeds immediately — even before
a full library scan has processed the file.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import shutil
import tempfile
import time
import uuid as _uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask


# ── Internal auth-bypass context ─────────────────────────────────────────────
# Set to True by ``cast_stream.cast_stream`` AFTER it has verified the
# signed token in the URL path.  ``stream_track`` reads this and skips
# its own _require_stream_auth.  Critically, this CANNOT be set by any
# external request — FastAPI does NOT bind module-level ContextVars to
# query / header / body / cookie inputs, so the previous "bool kwarg"
# approach (which FastAPI happily exposed as a query parameter, opening
# a trivial anonymous-stream bypass) is replaced.
_cast_internal_bypass_ctx: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "soniqboom_cast_internal_bypass_auth", default=False,
)


def _set_cast_internal_bypass(value: bool):
    """Used ONLY by cast_stream.py — set the bypass flag in the current
    Task's context.  Returns the token so the caller can reset it."""
    return _cast_internal_bypass_ctx.set(bool(value))


def _reset_cast_internal_bypass(token) -> None:
    try:
        _cast_internal_bypass_ctx.reset(token)
    except (LookupError, ValueError):
        pass

from soniqboom.config import settings
from soniqboom.core.conversion_cache import _cache_key as _ck
from soniqboom.core.data import get_track

log = logging.getLogger(__name__)


# ── Range-aware file serving ────────────────────────────────────────────────
# Starlette's FileResponse does NOT handle HTTP Range requests.  Browsers
# rely on Range for audio seeking (audio.currentTime = X triggers a
# Range: bytes=X- request).  Without 206 support, every seek restarts the
# stream from byte 0.

# Per-file stat cache: a single browser audio element issues 5–20 Range
# requests per playback (preload, seek, mid-track top-up).  ``stat()`` is
# a sync syscall and, on a slow SMB / NFS data dir under 5 concurrent
# streams, it can block the event loop ~5–30 ms per call.  A short TTL
# (file size only changes when the file is rewritten, which is exceedingly
# rare during the playback lifetime) eliminates that cost on hot paths
# without forcing operators to manually invalidate.
_STAT_CACHE: dict[str, tuple[int, float, float]] = {}
_STAT_CACHE_TTL = 5.0  # seconds


async def _cached_stat(file_path: Path) -> tuple[int, float]:
    """Return (st_size, st_mtime) with a per-path TTL cache.

    Re-stat only after the TTL elapses; intermediate Range requests reuse
    the previous result and never hit the syscall.
    """
    key = str(file_path)
    now = time.time()
    entry = _STAT_CACHE.get(key)
    if entry is not None and (now - entry[2]) < _STAT_CACHE_TTL:
        return entry[0], entry[1]
    st = await asyncio.to_thread(file_path.stat)
    _STAT_CACHE[key] = (st.st_size, st.st_mtime, now)
    return st.st_size, st.st_mtime


# Range slices larger than this stream chunked via ``os.pread`` rather than
# materialising the whole slice in RAM.  Below the threshold the simpler
# single-read path stays — small slices (browser HEAD probes, the initial
# 256 KB preflight) finish faster as a single bytes object than as a
# StreamingResponse.
_RANGE_STREAMING_THRESHOLD = 256 * 1024
_RANGE_STREAMING_CHUNK = 64 * 1024


async def _range_file_response(
    request: Request,
    file_path: Path | str,
    media_type: str,
    headers: dict[str, str] | None = None,
    background: BackgroundTask | None = None,
) -> Response:
    """Serve a file with HTTP Range support (single-range only)."""
    file_path = Path(file_path)
    total, _ = await _cached_stat(file_path)
    extra = dict(headers or {})
    extra["Accept-Ranges"] = "bytes"

    range_hdr = request.headers.get("range")
    if not range_hdr or not range_hdr.strip().startswith("bytes="):
        # No Range header → serve the full file normally
        return FileResponse(
            file_path, media_type=media_type,
            headers=extra, background=background,
        )

    # Parse "bytes=START-END" (END is optional)
    range_spec = range_hdr.strip()[6:]  # strip "bytes="
    parts = range_spec.split("-", 1)
    try:
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else total - 1
    except ValueError:
        return FileResponse(
            file_path, media_type=media_type,
            headers=extra, background=background,
        )

    # Clamp to valid range
    start = max(0, min(start, total - 1))
    end = max(start, min(end, total - 1))
    length = end - start + 1

    extra["Content-Range"] = f"bytes {start}-{end}/{total}"
    extra["Content-Length"] = str(length)

    # Large slice → stream in 64 KB chunks via os.pread so we never hold
    # the whole slice in RAM.  Five concurrent users seeking around in
    # 30 MB FLACs used to peak the worker at 150 MB of transient buffers;
    # with chunked pread the working-set stays at ~320 KB total.
    if length >= _RANGE_STREAMING_THRESHOLD:
        async def _stream_range():
            fd = await asyncio.to_thread(os.open, str(file_path), os.O_RDONLY)
            try:
                pos = start
                remaining = length
                while remaining > 0:
                    to_read = min(_RANGE_STREAMING_CHUNK, remaining)
                    chunk = await asyncio.to_thread(os.pread, fd, to_read, pos)
                    if not chunk:
                        break
                    yield chunk
                    pos += len(chunk)
                    remaining -= len(chunk)
            finally:
                try:
                    await asyncio.to_thread(os.close, fd)
                except OSError:
                    pass

        return StreamingResponse(
            _stream_range(),
            status_code=206,
            media_type=media_type,
            headers=extra,
            background=background,
        )

    # Small slice: single read stays simpler and avoids the per-chunk
    # to_thread overhead that dominates at small sizes.
    def _read_slice() -> bytes:
        with open(file_path, "rb") as f:
            f.seek(start)
            return f.read(length)
    data = await asyncio.to_thread(_read_slice)

    return Response(
        content=data,
        status_code=206,
        media_type=media_type,
        headers=extra,
        background=background,
    )

router = APIRouter(prefix="/stream", tags=["stream"])

# Formats ALL major browsers can decode natively (Chrome, Firefox, Safari).
# .m4a and .aac are intentionally excluded: they may contain ALAC
# (Apple Lossless), which only Safari supports. Chrome/Firefox fail silently
# on ALAC, so we transcode all .m4a/.aac through ffmpeg to be safe.
NATIVE: dict[str, str] = {
    ".mp3":  "audio/mpeg",
    ".flac": "audio/flac",
    ".wav":  "audio/wav",
    ".ogg":  "audio/ogg",
    ".opus": "audio/ogg; codecs=opus",
}

TRANSCODE_MIME = {
    "flac": "audio/flac",
    "mp3":  "audio/mpeg",
    "ogg":  "audio/ogg",
}

# Formats that need transcoding (ALAC, AIFF, WavPack, Musepack, M4A/AAC, …)
# Anything not in NATIVE ends up here automatically.

# ── Rendered format extension sets ────────────────────────────────────────────
_SID_EXTS = {".sid", ".psid"}
_MIDI_EXTS = {".mid", ".midi"}
# Tracker formats decoded by openmpt123.  AHX (.ahx) and Hively (.hvl)
# used to live here but openmpt123 doesn't decode them — they now route
# through uade123 via the _UADE_EXTS set + _render_uade (see below).
_TRACKER_EXTS = {
    ".mod", ".s3m", ".xm", ".it", ".mtm", ".med", ".oct",
    ".669", ".dbm", ".ult", ".stm", ".far",
    ".amf", ".gdm", ".imf", ".okt", ".sfx", ".wow", ".dsm",
}
# DSD containers — transcoded to PCM via ffmpeg, downsampled so the FLAC
# stream is reasonable for browser playback.  176.4 kHz output would be
# audiophile-pure but ~30 MB/min; 96 kHz is the practical sweet spot
# (already above CD, preserves all audible content).
_DSD_EXTS = {".dsf", ".dff", ".wsd"}
_DSD_OUTPUT_RATE = 96000


def _find_renderer(configured_path: str, binary_name: str) -> str | None:
    """Find a renderer binary: config path -> PATH lookup -> None."""
    if configured_path:
        p = Path(configured_path)
        if p.is_file():
            return str(p)
    return shutil.which(binary_name)


def _cleanup_paths(*paths: Path | None):
    """Remove temp files after response is sent."""
    for p in paths:
        if p is not None:
            Path(p).unlink(missing_ok=True)


def _is_file_not_found(exc: BaseException) -> bool:
    """Detect "file is missing on the source" across backends.

    The remote-fetch path raises a grab-bag of exception types depending
    on the protocol:

    * FTP ``ftplib.error_perm`` with a "550 ... No such file or directory"
      reply (the most common case — peer is alive but the path is gone)
    * Generic :class:`FileNotFoundError` for local-FS sources after a
      mid-playback ``rm``
    * SMB ``smbprotocol.exceptions.SMBOSError`` (often surfaced as the
      builtin ``FileNotFoundError`` subclass on macOS) or messages
      containing ``STATUS_OBJECT_NAME_NOT_FOUND``

    Returns True if the exception is best mapped to HTTP 404 rather than
    502 — i.e. the caller should rescan, not retry.
    """
    if isinstance(exc, FileNotFoundError):
        return True
    # ftplib subclasses Exception; ``error_perm`` (550 ...) carries the
    # numeric reply at the start of str(exc).  We avoid importing ftplib
    # here so this module stays import-light on platforms without it.
    msg = str(exc)
    if "550 " in msg or msg.startswith("550 "):
        # 550 = "Requested action not taken: File unavailable"
        # The most common cause is genuine file-not-found, but it can
        # also mean permission denied.  Either way the right user
        # action is "rescan and retry", not "we'll auto-retry".
        if "no such file" in msg.lower() or "not found" in msg.lower():
            return True
    if "STATUS_OBJECT_NAME_NOT_FOUND" in msg:
        return True
    return False


def _cache_key_for(
    format_type: str, track_id: str,
    codec: str | None = None, target_rate: int | None = None,
    subsong: int = 0, duration: int | None = None,
) -> str:
    """Thin wrapper around ``conversion_cache._cache_key`` for callers in
    this module that need the same key the cache will use internally — e.g.
    pinning the currently-playing entry, or building a stable identifier
    for the prewarm queue."""
    return _ck(track_id, format_type, subsong=subsong,
               duration=duration, codec=codec, target_rate=target_rate)


# Global cap on concurrent renderer subprocesses so a render-status poll
# storm + several user-driven plays can't stack ffmpeg/sidplayfp/fluidsynth/
# openmpt123 to CPU saturation on a 4-core box.  Sized at half the CPU
# count, min 2 — Perf #1 flagged the stacking risk under the 5-user load.
import os as _os_for_render
_RENDER_SLOTS = max(2, (_os_for_render.cpu_count() or 4) // 2)
_render_sem = asyncio.Semaphore(_RENDER_SLOTS)


async def _await_renderer(
    cmd: list[str], tmp_path: Path, *, timeout: float, kind: str,
) -> None:
    """Run a renderer subprocess with a timeout and check its exit status.

    Without this guard, the previous code awaited ``proc.wait()`` unbounded —
    a hung renderer (e.g. ``fluidsynth`` blocked on a malformed input) parks
    the HTTP request forever — and ignored the return code, so a renderer
    failure produced an empty WAV that played as silence with no error.

    The outer ``try/finally`` also handles ``asyncio.CancelledError`` so
    when a prewarm is cancelled by the FIFO cap (or the request is closed),
    the subprocess gets ``SIGKILL`` and the temp file is unlinked — without
    this, "user mashes Next 30 times" can leave 30 orphan ffmpeg processes
    pegging CPU.
    """
    async with _render_sem:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                Path(tmp_path).unlink(missing_ok=True)
                raise HTTPException(504, f"{kind} render timed out after {int(timeout)}s")
            if proc.returncode != 0:
                Path(tmp_path).unlink(missing_ok=True)
                raise HTTPException(
                    502, f"{kind} renderer exited with status {proc.returncode}",
                )
        finally:
            # If we got here on cancel/timeout/error, make sure the subprocess
            # is dead and the temp file is gone.  Idempotent — successful
            # runs are no-ops (proc already exited, tmp_path is the cache
            # source that ``store_cached`` will have already moved).
            if proc.returncode is None:
                try:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, Exception):
                        pass
                except ProcessLookupError:
                    pass
                Path(tmp_path).unlink(missing_ok=True)


# ── SID rendering ─────────────────────────────────────────────────────────────

async def _render_sid(path: Path, subsong: int = 0, duration: int | None = None) -> Path:
    """Render SID file to a temp WAV via sidplayfp and return the path.

    ``duration`` overrides the default — HVSC supplies the actual
    per-tune length (often shorter than the 5 min default), so without
    this override every SID would render to the safety-cap duration.
    Falls back to ``settings.sid_default_duration`` when HVSC has no
    entry for the file."""
    binary = _find_renderer(settings.sidplayfp_path, "sidplayfp")
    if not binary:
        raise HTTPException(501, "sidplayfp not installed")

    dur = int(duration if duration is not None else settings.sid_default_duration)
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()

    cmd = [binary]
    if subsong > 0:
        cmd.append(f"-o{subsong}")      # sidplayfp flags: no space between flag and value
    cmd.extend([
        f"-t{dur}",
        f"-w{tmp_wav.name}",
        str(path),
    ])

    await _await_renderer(cmd, Path(tmp_wav.name), timeout=dur + 30, kind="SID")
    return Path(tmp_wav.name)


# ── libgme rendering (NSF/SPC/GBS/VGM/AY/KSS/SAP/HES/GYM) — E-14 ─────────────

_GME_EXTS_STREAM = {".nsf", ".nsfe", ".spc", ".gbs", ".vgm", ".vgz",
                    ".ay", ".kss", ".sap", ".gym", ".hes"}


async def _render_gme(path: Path, subsong: int = 0) -> Path:
    """Render a libgme chiptune file to a temp WAV.

    Prefers an explicit ``gme`` CLI when configured.  Falls back to
    ffmpeg's built-in gme demuxer (``ffmpeg -i file.nsf -t N output.wav``)
    when the helper isn't available — that path works on standard Homebrew
    ffmpeg builds with libgme."""
    duration = settings.sid_default_duration   # shares the chiptune default
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    out = Path(tmp_wav.name)

    # Preferred path: in-process libgme via ctypes.  Homebrew ffmpeg ships
    # without --enable-libgme and there is no standalone gme CLI, so on a stock
    # macOS/Linux box this is the ONLY working renderer for NSF/SPC/GBS/... —
    # the CLI / ffmpeg branches below stay as fallbacks for hosts that have them.
    from soniqboom.core import gme_render
    if gme_render.is_available():
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, path.read_bytes)
        wav = await loop.run_in_executor(
            None, gme_render.render_wav, data, subsong, int(duration),
        )
        if wav:
            out.write_bytes(wav)
            return out
        log.info("libgme produced no audio for %s — trying gme CLI / ffmpeg", path.name)

    gme_bin = _find_renderer(settings.gme_path, "gme")
    if gme_bin:
        # gme CLI signature: ``gme <input> <output.wav> [track=N] [length=Nms]``
        cmd = [gme_bin, str(path), str(out)]
        if subsong > 0:
            cmd.append(f"track={subsong}")
        cmd.append(f"length={int(duration * 1000)}")
    else:
        # ffmpeg fallback — works if the build has --enable-libgme.
        ff = settings.ffmpeg_path or "ffmpeg"
        cmd = [
            ff, "-hide_banner", "-loglevel", "error",
            "-t", str(duration),
        ]
        if subsong > 0:
            cmd += ["-track_index", str(subsong)]
        cmd += ["-i", str(path), "-y", str(out)]
    await _await_renderer(cmd, out, timeout=duration + 30, kind="GME")
    return out


# ── AdLib / OPL2 FM rendering (AdPlug) ────────────────────────────────────────
# AdPlug decodes id Software / Apogee IMF (Wolfenstein 3D, Commander Keen, …)
# plus the wider AdLib/OPL family — ROL, CMF, D00, RAD, LucasArts LAA, Sierra
# SCI, DOSBox DRO, HSC, RIX, …  Rendered to WAV via its ``adplay`` disk writer,
# the same subprocess pattern as sidplayfp / openmpt123 / uade123.
#
# ``.imf`` is deliberately NOT in this set: the extension is shared with the
# Imago Orpheus *tracker* format (decoded by openmpt123).  ``_render_imf``
# disambiguates the two by content signature.
_ADLIB_EXTS = {
    ".rol", ".cmf", ".d00", ".rad", ".laa", ".sci", ".dro",
    ".hsc", ".rix", ".a2m", ".adl", ".bam", ".ksm",
}
_ADLIB_DEFAULT_TIMEOUT_S = 8 * 60


async def _render_adlib(path: Path, subsong: int = 0) -> Path:
    """Render an AdLib / OPL2 FM tune to WAV via AdPlug's ``adplay`` disk writer.

    Output: 44.1 kHz / stereo / 16-bit signed LE — matches the other rendered
    formats so the cache + cast pipeline treat them uniformly.  adplay renders
    the tune once (AdPlug reports the song's end) then exits; the timeout bounds
    any endless / looping tune.
    """
    binary = _find_renderer(settings.adplay_path, "adplay")
    if not binary:
        raise HTTPException(
            501,
            "adplay (AdPlug) not installed — AdLib/OPL formats (id IMF, ROL, "
            "CMF, D00, RAD, …) require it.  Install via 'brew install adplay' "
            "(macOS) or 'apt install adplug-utils' (Debian/Ubuntu).",
        )
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    cmd = [binary, "-O", "disk", "-d", tmp_wav.name, "-f", "44100", "--stereo"]
    if subsong > 0:
        cmd += ["-s", str(subsong)]      # multi-song AdLib formats (RAD, …)
    cmd.append(str(path))
    await _await_renderer(
        cmd, Path(tmp_wav.name),
        timeout=_ADLIB_DEFAULT_TIMEOUT_S, kind="adlib",
    )
    return Path(tmp_wav.name)


async def _render_imf(path: Path, subsong: int = 0) -> Path:
    """Render a ``.imf`` file, disambiguating the overloaded extension.

    Two unrelated formats share ``.imf``:
      * **Imago Orpheus** — a PC tracker module (decoded by openmpt123).
      * **id Software / Apogee IMF** — an OPL2 FM register dump (Wolfenstein 3D,
        Commander Keen, Duke Nukem …) decoded by AdPlug.

    Imago Orpheus carries an ``IM10`` signature at offset 0x3C (60); id IMF does
    not — so we read that signature and route to the right renderer.
    """
    # Sniff the 64-byte header off the event-loop thread.  _render_imf is
    # awaited inline by the conversion-cache render path (conversion_cache
    # does ``await render_fn()`` on the loop), so even a sub-millisecond
    # synchronous file read belongs in an executor — blocking the loop is
    # exactly the failure class hardened against elsewhere this release.
    def _sniff() -> bytes:
        try:
            with open(path, "rb") as fh:
                return fh.read(64)
        except OSError:
            return b""
    head = await asyncio.get_running_loop().run_in_executor(None, _sniff)
    if len(head) >= 64 and head[60:64] == b"IM10":
        return await _render_tracker(path, subsong=subsong)   # Imago Orpheus
    return await _render_adlib(path, subsong=subsong)          # id/Apogee AdLib IMF


# ── MIDI rendering ────────────────────────────────────────────────────────────

async def _render_midi(path: Path) -> Path:
    """Render MIDI file to a temp WAV via FluidSynth and return the path."""
    binary = _find_renderer(settings.fluidsynth_path, "fluidsynth")
    if not binary:
        raise HTTPException(501, "FluidSynth not installed")

    from soniqboom.config import get_active_soundfont
    soundfont = get_active_soundfont()
    if not soundfont:
        raise HTTPException(501, "No soundfont available — upload one in Admin settings")

    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()

    cmd = [
        binary,
        "-ni",                # no interactive shell
        "-a", "file",         # file audio driver
        "-T", "wav",          # output format
        "-F", tmp_wav.name,   # write to temp file
        str(soundfont),
        str(path),
    ]

    await _await_renderer(cmd, Path(tmp_wav.name), timeout=600, kind="MIDI")
    return Path(tmp_wav.name)


# ── Tracker module rendering ─────────────────────────────────────────────────

async def _render_tracker(path: Path, subsong: int = 0) -> Path:
    """Render tracker module to a temp WAV via openmpt123 and return the path.

    Side effect: in parallel with the audio render, we kick off a VU
    extraction pass that produces a ``.vu`` sidecar via the in-process
    libopenmpt ctypes binding.  The sidecar lands next to the cached
    WAV (the conversion cache moves the WAV from temp to its final
    home; the VU writer follows the same path).  See
    ``soniqboom/core/openmpt_vu.py`` and ``docs/vu-cache-format.md``.

    The VU pass is best-effort: failures (lib not loaded, malformed
    module, unsupported format) are swallowed and the frontend falls
    back to its FFT-spectrum visualiser.
    """
    binary = _find_renderer(settings.openmpt123_path, "openmpt123")
    if not binary:
        raise HTTPException(501, "openmpt123 not installed")

    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()

    cmd = [binary, "--batch", "--quiet", "--force", "-o", tmp_wav.name]
    if subsong > 0:
        cmd.extend(["--subsong", str(subsong)])
    cmd.extend(["--", str(path)])

    # Kick off the VU extraction concurrently — it runs against the
    # source module file via libopenmpt directly and doesn't share I/O
    # with the openmpt123 subprocess.  Reads the file ONCE in this
    # coroutine to avoid two readers of a flaky network share / ZIP
    # virtual path.
    vu_task: asyncio.Task | None = None
    try:
        vu_task = asyncio.create_task(
            _extract_vu_sidecar(path, subsong, Path(tmp_wav.name)),
            name=f"vu_extract[{path.name}]",
        )
    except Exception:
        log.debug("VU extract task scheduling failed", exc_info=True)

    try:
        await _await_renderer(cmd, Path(tmp_wav.name), timeout=600, kind="tracker")
    finally:
        # Let the VU pass finish (bounded), but don't block scan-complete
        # forever if libopenmpt hangs on a malformed file.
        if vu_task is not None:
            try:
                await asyncio.wait_for(vu_task, timeout=30)
            except (asyncio.TimeoutError, Exception):
                vu_task.cancel()

    return Path(tmp_wav.name)


async def _extract_vu_sidecar(
    src_path: Path, subsong: int, wav_path: Path,
) -> None:
    """Background helper: run the VU extraction pass and write the
    ``.vu`` sidecar next to *wav_path*.  Best-effort; logs on failure
    but never raises.

    Runs the libopenmpt call in a thread (the ctypes calls release the
    GIL, but the whole pass is bounded and short) to avoid stalling
    the event loop on a very long module.
    """
    try:
        from soniqboom.core import openmpt_vu
        if not openmpt_vu.is_available():
            return
        loop = asyncio.get_event_loop()
        file_bytes = await loop.run_in_executor(None, src_path.read_bytes)
        result = await loop.run_in_executor(
            None,
            lambda: openmpt_vu.extract_vu(
                file_bytes,
                subsong=subsong if subsong > 0 else -1,
            ),
        )
        if result is None or result.frames == 0:
            log.debug("VU extract for %s: no result", src_path)
            return
        # Sidecar path: same stem as the WAV with .vu extension.
        vu_path = wav_path.with_suffix(".vu")
        await loop.run_in_executor(
            None, openmpt_vu.write_sidecar, vu_path, result,
        )
        log.info(
            "VU sidecar written for %s: %d channels × %d frames @ %d Hz",
            src_path.name, result.channels, result.frames, result.sample_rate,
        )
    except Exception:
        log.warning("VU extract failed for %s", src_path, exc_info=True)


# ── UADE renderer (AHX / Hively / ~200 other Amiga formats) ───────────────
# openmpt123 doesn't decode AHX (AbyssHighestExperience) or Hively
# tracker.  uade123 — Unix Amiga Delitracker Emulator — runs the
# original Amiga player binaries through libuae and renders to WAV.
# Optional dep (``brew install uade`` on macOS, ``apt-get install uade``
# on Debian/Ubuntu); fall back to a clear 501 when missing so the UI
# can surface an install hint instead of swallowing the silence.

# AHX stays on uade123 (its AbyssHighestExperience replay works).  HVL
# (HivelyTracker, AHX's multi-channel successor) is NOT in the Homebrew uade
# player set and libopenmpt can't load it either, so it has its own renderer
# below (bundled HivelyTracker replay → hvl2wav).
_UADE_EXTS = {".ahx"}
_HVL_EXTS = {".hvl"}
_hvl2wav_bin: "Path | None" = None

# uade123 has no native "render exactly N seconds" mode — it relies on
# the player binary's end-detection.  Most AHX tunes are < 5 minutes;
# we cap at 8 to bound the worst case while leaving plenty of headroom
# for the rare longer arrangement.
_UADE_DEFAULT_TIMEOUT_S = 8 * 60


async def _render_uade(path: Path, subsong: int = 0) -> Path:
    """Render an AHX / Hively / Amiga-tracker module to WAV via uade123.

    Returns the temp-file path; caller (``conversion_cache.get_or_render``)
    moves it into the on-disk cache and unlinks the temp.

    Output spec: 44.1 kHz / stereo / 16-bit signed LE.  Matches what
    sidplayfp + openmpt123 produce so the downstream cast pipeline
    can treat all rendered formats uniformly.

    ``subsong`` is honoured for multi-tune containers (rare in AHX,
    common in HVL).  uade123 uses 0-indexed subsongs like the rest of
    SoniqBoom — no off-by-one translation needed.
    """
    binary = _find_renderer(settings.uade123_path, "uade123")
    if not binary:
        raise HTTPException(
            501,
            "uade123 not installed — Amiga formats (AHX / Hively) require it. "
            "Install via 'brew install uade' (macOS) or 'apt install uade' (Debian/Ubuntu).",
        )

    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()

    # ``--filter=A1200`` picks the Amiga 1200 LED-filter model (the
    # default A500 sounds muffled on modern listeners).  ``--headphones``
    # adds a tiny stereo-widening effect that mimics what AHX players
    # commonly did at the time.  ``-e wav`` plus ``-f`` forces output
    # path (uade123 defaults to a temporary streaming sink).
    cmd = [
        binary,
        "--filter=A1200",
        "--headphones",
        "-e", "wav",
        "-f", tmp_wav.name,
    ]
    if subsong > 0:
        # uade uses ``--subsong=N``.  Default range 0..N where N is the
        # max subsong index reported by the player.
        cmd += [f"--subsong={subsong}"]
    cmd += ["--", str(path)]

    await _await_renderer(
        cmd, Path(tmp_wav.name),
        timeout=_UADE_DEFAULT_TIMEOUT_S, kind="uade",
    )
    return Path(tmp_wav.name)


# ── Hively (HVL) renderer ─────────────────────────────────────────────────
# HivelyTracker (.hvl) is AHX's multi-channel successor.  The Homebrew uade123
# build ships no Hively replay and libopenmpt can't load HVL either, so we
# bundle the HivelyTracker project's self-contained replay (BSD, vendored under
# ``soniqboom/native/hvl``) and compile a tiny ``hvl2wav`` converter once, on
# first use, into the writable data dir (so it works from a read-only app too).
async def _ensure_hvl2wav() -> "Path | None":
    """Return a built ``hvl2wav`` path, compiling it once if needed.

    Returns None when no C compiler is available — the caller raises a clear
    501 rather than the cryptic generic render failure.
    """
    global _hvl2wav_bin
    if _hvl2wav_bin and _hvl2wav_bin.exists():
        return _hvl2wav_bin
    src_dir = Path(__file__).resolve().parent.parent / "native" / "hvl"
    csrc = [src_dir / "hvl2wav.c", src_dir / "replay.c"]
    if not all(p.exists() for p in csrc):
        log.warning("HVL: bundled replay source missing under %s", src_dir)
        return None
    from soniqboom.config import get_data_dir
    out_dir = get_data_dir() / "native"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    binp = out_dir / "hvl2wav"
    newest_src = max(p.stat().st_mtime for p in csrc)
    if binp.exists() and binp.stat().st_mtime >= newest_src:
        _hvl2wav_bin = binp
        return binp
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        log.warning("HVL: no C compiler (cc/clang/gcc) — cannot build hvl2wav")
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            cc, "-O2", "-w", str(csrc[0]), str(csrc[1]), "-o", str(binp), "-lm",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("HVL: hvl2wav build failed to launch: %s", exc)
        return None
    if proc.returncode != 0 or not binp.exists():
        log.warning("HVL: hvl2wav build failed: %s", (err or b"").decode("utf-8", "replace")[:300])
        return None
    try:
        binp.chmod(0o755)
    except OSError:
        pass
    log.info("HVL: built hvl2wav at %s", binp)
    _hvl2wav_bin = binp
    return binp


async def _render_hvl(path: Path, subsong: int = 0) -> Path:
    """Render a HivelyTracker (.hvl) module to WAV via the bundled hvl2wav.

    44.1 kHz / stereo / 16-bit signed LE — matches the other renderers so the
    cache + cast pipeline treats every rendered format uniformly.
    """
    binary = await _ensure_hvl2wav()
    if not binary:
        raise HTTPException(
            501,
            "HivelyTracker (HVL) decoder unavailable — a C compiler (cc / clang / "
            "gcc) is required to build the bundled replay.",
        )
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    # hvl2wav takes ATTACHED args: -f<freq>, -o<out>, -s<subsong>.  It writes
    # ``<out>.tmp`` then copies to ``<out>`` (overwriting our 0-byte temp).
    cmd = [str(binary), "-f44100", f"-o{tmp_wav.name}"]
    if subsong > 0:
        cmd.append(f"-s{subsong}")
    cmd.append(str(path))
    await _await_renderer(cmd, Path(tmp_wav.name), timeout=300, kind="HVL")
    return Path(tmp_wav.name)


def _is_safari(request: Request) -> bool:
    """True for desktop/iOS Safari but not Chrome, Edge, or other Chromium UAs.

    Chrome's UA also contains "Safari"; Edge contains "Edg/"; Chromium forks
    add "Chrome" or their own token. Require "Safari" and absence of those.
    """
    ua = request.headers.get("user-agent", "")
    if "Safari" not in ua:
        return False
    return not any(t in ua for t in ("Chrome", "Chromium", "Edg/", "OPR/"))


async def _probe_codec(path: Path) -> str | None:
    """Return the audio codec name via ffprobe, or None on failure.

    Uses asyncio.create_subprocess_exec so the event loop is never blocked
    while ffprobe inspects the file.  Bounded by a timeout so a slow SMB
    share or pathological file can't park the stream endpoint forever.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            log.warning("ffprobe timed out after 15s on %s", path)
            return None
        return stdout.decode().strip().lower() or None
    except Exception:
        return None


# ── ZIP extraction cache ────────────────────────────────────────────────────
# Per-track stable disk path for archive-contained files.  Each HTTP Range
# request used to re-extract the full member; with audio elements issuing
# 5–20 range requests per playback this was the single biggest source of
# perceived latency for any track inside a ZIP.  Now extracted once,
# served via the standard Range path on every subsequent request.
#
# Invalidation: outer-zip mtime is checked on every cache hit.  Any change
# (re-zip, edit, replace) triggers a fresh extraction.  The cache lives in
# ``data_dir/zip-extracts/`` so an admin can blow it away wholesale.
_ZIP_EXTRACT_CACHE: dict[str, dict] = {}
# Per-track locks rather than a single global ``asyncio.Lock``.  Under the
# old global lock, an extraction in progress for track A serialised every
# concurrent request for track B/C/D — meaning a single big-FLAC extract
# could stall every other user's playback start until it finished.
_zip_locks: dict[str, asyncio.Lock] = {}
_zip_locks_guard = asyncio.Lock()

# Disk budget for extracted ZIP members.  Mirrors the conversion-cache
# pattern but uses a smaller slice (1/4 of conversion cache) — extractions
# are easy to reproduce on cache miss, so eviction here is cheaper than
# eviction of a transcoded WAV.
_ZIP_EXTRACT_TOTAL_BYTES = 0


def _zip_extract_max_bytes() -> int:
    """Budget for the ZIP-extract cache.

    Priority order:
      1. ``settings.zip_extract_cache_max_mb`` when explicitly set (the
         operator-controlled value surfaced by the admin Settings panel).
      2. Implicit derivation from ``conversion_cache_max_bytes`` (1/4
         share, capped at 2 GB) — preserves the previous default-budget
         behaviour for installs that haven't customised it.
    """
    cfg_mb = getattr(settings, "zip_extract_cache_max_mb", 0) or 0
    if cfg_mb > 0:
        return cfg_mb * 1024 * 1024
    base = getattr(settings, "conversion_cache_max_bytes", 0) or 0
    return max(512 * 1024 * 1024, min(2 * 1024 * 1024 * 1024, base // 4 or 2 * 1024 * 1024 * 1024))


def _zip_extract_dir() -> Path:
    from soniqboom.config import get_data_dir
    d = get_data_dir() / "zip-extracts"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _zip_lock_for(track_id: str) -> asyncio.Lock:
    """Lazily allocate (and return) the per-track lock.

    A short critical section under a guard avoids two concurrent extracts
    racing on lock allocation for the same track — both would get
    different lock objects and neither would serialise correctly.
    """
    lock = _zip_locks.get(track_id)
    if lock is not None:
        return lock
    async with _zip_locks_guard:
        lock = _zip_locks.get(track_id)
        if lock is None:
            lock = asyncio.Lock()
            _zip_locks[track_id] = lock
        return lock


# Refcounted pins for in-flight readers of ZIP extracts.  Mirrors the
# conversion_cache pin mechanism — without this, LRU eviction could
# unlink a file while a FileResponse is mid-Range, leaking the inode
# on Linux/macOS and outright failing on Windows (R2/R3 finding).
_zip_pin_refs: dict[str, int] = {}
_zip_pending_purge: dict[str, str] = {}  # tid -> path-to-unlink-on-zero-refs


def _zip_pin(track_id: str) -> None:
    _zip_pin_refs[track_id] = _zip_pin_refs.get(track_id, 0) + 1


def _zip_unpin(track_id: str) -> None:
    cur = _zip_pin_refs.get(track_id, 0)
    if cur <= 1:
        _zip_pin_refs.pop(track_id, None)
        # If eviction queued an unlink while pinned, execute it now.
        pending = _zip_pending_purge.pop(track_id, None)
        if pending:
            try: Path(pending).unlink(missing_ok=True)
            except OSError: pass
    else:
        _zip_pin_refs[track_id] = cur - 1


def _zip_evict_until_under_budget() -> None:
    """LRU evict ZIP-extract entries until under the configured budget.

    Pinned entries (currently being streamed) defer their unlink until
    the last reader unpins — the file is removed from the in-memory
    cache immediately so a new extraction takes over the cache slot,
    but its on-disk bytes survive until the active stream finishes.
    """
    global _ZIP_EXTRACT_TOTAL_BYTES
    max_bytes = _zip_extract_max_bytes()
    while _ZIP_EXTRACT_TOTAL_BYTES > max_bytes and _ZIP_EXTRACT_CACHE:
        oldest_tid = min(
            _ZIP_EXTRACT_CACHE,
            key=lambda k: _ZIP_EXTRACT_CACHE[k].get("extracted_at", 0),
        )
        entry = _ZIP_EXTRACT_CACHE.pop(oldest_tid, None)
        if not entry:
            break
        size = entry.get("size", 0)
        _ZIP_EXTRACT_TOTAL_BYTES = max(0, _ZIP_EXTRACT_TOTAL_BYTES - size)
        path_to_drop = entry.get("path")
        if oldest_tid in _zip_pin_refs:
            # Defer — last reader will unlink in _zip_unpin.
            if path_to_drop:
                _zip_pending_purge[oldest_tid] = path_to_drop
            continue
        if path_to_drop:
            try: Path(path_to_drop).unlink(missing_ok=True)
            except OSError: pass


async def reap_orphan_zip_extracts() -> int:
    """Drop any on-disk extract whose track_id is no longer in the store.

    Run at startup so a long-uptime install doesn't accumulate extracts
    of files that have been deleted from the library.  Returns the count
    removed for the log line.
    """
    from soniqboom.core.data import get_track as _get_track
    extract_dir = _zip_extract_dir()
    removed = 0
    if not extract_dir.exists():
        return 0
    for child in extract_dir.iterdir():
        # Filename is "<track_id><suffix>" — recover the track_id by
        # stripping the suffix.
        tid = child.stem
        try:
            track = await _get_track(tid)
        except Exception:
            track = None
        if track is None:
            try:
                child.unlink()
                removed += 1
            except OSError:
                pass
    return removed


async def _get_or_extract_zip_member(path_str: str, track_id: str) -> Path | None:
    """Return a stable on-disk path for a ZIP-contained track.

    Extracts on first request, caches on disk, reuses on every subsequent
    Range request.  Outer-zip mtime gates invalidation: if the archive is
    rewritten the cached extraction is dropped and we re-extract.
    """
    global _ZIP_EXTRACT_TOTAL_BYTES
    parts = path_str.split("::")
    outer_zip = Path(parts[0])
    if not outer_zip.exists():
        return None
    try:
        zip_mtime = outer_zip.stat().st_mtime
    except OSError:
        return None

    lock = await _zip_lock_for(track_id)
    async with lock:
        entry = _ZIP_EXTRACT_CACHE.get(track_id)
        if entry is not None:
            cached_path = Path(entry["path"])
            if (entry.get("zip_mtime") == zip_mtime
                    and entry.get("zip_path") == str(outer_zip)
                    and cached_path.exists()):
                # Refresh LRU recency.
                entry["extracted_at"] = time.time()
                return cached_path
            # Stale or missing — drop and re-extract.
            _ZIP_EXTRACT_TOTAL_BYTES = max(
                0, _ZIP_EXTRACT_TOTAL_BYTES - entry.get("size", 0),
            )
            _ZIP_EXTRACT_CACHE.pop(track_id, None)
            try: cached_path.unlink()
            except OSError: pass

        member_name = parts[-1]
        suffix = Path(member_name).suffix.lower()
        dest = _zip_extract_dir() / f"{track_id}{suffix}"

        def _extract() -> Path:
            from soniqboom.core.scanner import _read_from_zip_path
            data, _name = _read_from_zip_path(path_str)
            # Write atomically — .partial then rename — so a crash mid-write
            # doesn't leave a half-extracted file that we'd serve as if
            # complete on the next request.
            tmp = dest.with_suffix(dest.suffix + ".partial")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(str(tmp), str(dest))
            return dest

        try:
            path = await asyncio.to_thread(_extract)
        except Exception as exc:
            log.warning("ZIP extract failed for %s: %s", path_str, exc)
            return None

        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        _ZIP_EXTRACT_CACHE[track_id] = {
            "path": str(path),
            "zip_path": str(outer_zip),
            "zip_mtime": zip_mtime,
            "extracted_at": time.time(),
            "size": size,
        }
        _ZIP_EXTRACT_TOTAL_BYTES += size
        # Run eviction off the lock so a slow disk on the unlink doesn't
        # block the next extraction in the queue.
        try:
            await asyncio.to_thread(_zip_evict_until_under_budget)
        except Exception:
            log.exception("ZIP-extract eviction failed")
        return path


# ── In-flight WAV cache (adaptive cold start, PERC-8) ───────────────────────
# Why WAV: it's the only format whose total byte size is computable from
# (duration × sample_rate × channels × bytes_per_sample) BEFORE encoding,
# which is the property we need to serve Range requests against a file
# that's still being written.  ffmpeg's WAV muxer writes placeholder
# chunk sizes (0xFFFFFFFF) at the start and patches them at the end —
# unusable mid-render — so we pre-write our own correct header here and
# feed ffmpeg's raw PCM output ("-f s16le") into the file directly.
#
# The render outruns the play-head at ~5–10× realtime on modern hardware,
# so by the time the browser has read 1 s of audio, the cache file
# already has 5–10 s queued.  Seek-ahead within the rendered portion is
# instant; seek-ahead beyond it blocks the response generator until
# ffmpeg catches up (bounded by ``_GROWING_READ_TIMEOUT``).
#
# Indexed by track_id — at most one render runs per track via the
# conversion-cache per-key lock, so collisions between concurrent
# subscribers are physically impossible.
#
# Format choice: 16-bit / source-channel-count / target-sample-rate.
# 16 bit is well below the audible noise floor of any DSD source and
# halves the wire bytes vs 24-bit; the user explicitly licensed disk
# overhead so we don't optimise for compression.
_INFLIGHT_TRANSCODES: dict[str, dict] = {}
_INFLIGHT_LOCK = asyncio.Lock()
_GROWING_READ_TIMEOUT = 60.0   # seconds to block on bytes beyond current size
_GROWING_POLL_INTERVAL = 0.08  # how often the response generator re-stats
                               # the cache file when waiting on ffmpeg


def _build_wav_header(sample_rate: int, channels: int, total_samples: int,
                      bits_per_sample: int = 24) -> bytes:
    """Build a 44-byte canonical RIFF/WAVE PCM header with EXACT chunk sizes.

    Browsers compute audio.duration from (data chunk size) / (byte rate)
    when reading the WAV header.  Pre-computing both up front means the
    duration is correct from the very first read — the seek bar shows
    the right total immediately, no "Infinity" placeholder, no late
    correction once the file finishes writing.

    Default depth is 24-bit so DSD / hi-res ALAC sources keep their full
    dynamic range through the cache; the conversion path used to flatten
    everything to 16-bit, dropping audible detail near the noise floor.
    """
    bytes_per_sample = bits_per_sample // 8
    data_size = total_samples * channels * bytes_per_sample
    byte_rate = sample_rate * channels * bytes_per_sample
    block_align = channels * bytes_per_sample
    riff_chunk_size = 36 + data_size
    # struct-pack equivalents inlined for clarity — the header is tiny
    # and the spec is rigid, so a hand-built bytestring is clearer than
    # struct.pack with eight format codes.
    return (
        b"RIFF"
        + riff_chunk_size.to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")            # fmt chunk size
        + (1).to_bytes(2, "little")             # PCM = 1
        + channels.to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + bits_per_sample.to_bytes(2, "little")
        + b"data"
        + data_size.to_bytes(4, "little")
    )


_WAV_HEADER_LEN = 44


async def _pump_pcm_to_wav(
    track_id: str,
    src_path: Path,
    wav_path: Path,
    sample_rate: int,
    channels: int,
    source_duration: float,
    cache_key: str,
    format_type: str,
    on_complete=None,
) -> None:
    """Run ffmpeg → raw PCM → append to a pre-headered WAV file.

    Updates ``_TRANSCODE_PROGRESS`` so the determinate badge keeps
    working during the very short window before audio actually starts
    (modern hardware renders the first second in well under that).

    On clean exit: rename ``.partial`` to the final cache name + invoke
    ``on_complete`` so the conversion cache picks it up.  On failure
    (ffmpeg non-zero, cancellation, or aborted pump): unlink the
    ``.partial`` file so it isn't adopted by ``warmup_from_disk`` at
    next boot.
    """
    bytes_per_sample = 3  # s24le — preserves hi-res / DSD source detail
    total_samples = int(round(source_duration * sample_rate))
    expected_data_bytes = total_samples * channels * bytes_per_sample
    expected_size = _WAV_HEADER_LEN + expected_data_bytes

    started_at = time.time()
    _TRANSCODE_PROGRESS[track_id] = {
        "percent": 0.0,
        "eta_seconds": None,
        "started_at": started_at,
        "target_duration": source_duration,
        "ready": False,
        "finished_at": 0.0,
    }

    src_ext = src_path.suffix.lower()
    is_dsd_source = src_ext in _DSD_EXTS

    cmd = [
        settings.ffmpeg_path or "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-nostats",
        "-i", str(src_path),
        "-vn",
        "-threads", "0",
        "-ar", str(sample_rate),
        "-ac", str(channels),
    ]
    # DSD sources: low-pass at 40 kHz to suppress noise-shaped ultrasonic
    # energy before the rate conversion, then use the SoX resampler at high
    # precision with TPDF dither so the 24-bit PCM faithfully captures the
    # audible band without ringing artefacts at the cut-off.  Plain ffmpeg
    # ``aresample`` defaults to a fast linear-phase polyphase filter that
    # leaves audible aliasing at 88.2→96 kHz on dense material.
    #
    # Non-DSD sources: dither only on the 24→16 reduction path.  Since the
    # cache file is now 24-bit (see ``bytes_per_sample`` above) this branch
    # currently has no effect — kept here so that a future config knob that
    # lowers the target depth picks up dither automatically.
    if is_dsd_source:
        # DSD → PCM: lowpass at 40 kHz before decimation suppresses the
        # DSD modulator noise that lives in 30–90 kHz from leaking into
        # the audible band as IM distortion.  We deliberately do NOT
        # request the ``soxr`` resampler engine — many ffmpeg builds
        # (notably Homebrew's default + some Linux distro builds) ship
        # without ``--enable-libsoxr``, which makes the filter chain
        # fail with "Requested resampling engine is unavailable" and
        # the pump writes only the WAV header + silence padding.
        # ffmpeg's built-in swresample is the safe default and is
        # transparent at 24-bit output.
        #
        # Full DSD → PCM filter chain (verified 2026-05-23 on the user's
        # Setsuna Ogiso DSF whose 0:17 segment was previously a -1.0 DC
        # rail-peg the browser silenced as speaker-protection):
        #
        #   highpass=f=20  — strips DC bias the delta-sigma demodulator
        #                    leaves on certain SACD-authored DSD chunks.
        #                    Without this, segments of the source that
        #                    represent "near-silence" in DSD's bit
        #                    pattern decode to a constant -8388578 PCM
        #                    value (full negative rail), not zero.  The
        #                    OS audio driver / browser output stage
        #                    correctly identifies that as a DC offset
        #                    and mutes it for speaker protection — the
        #                    user hears the "silent gaps aligning with
        #                    the waveform's tall peaks".
        #   lowpass=f=40000 — suppresses noise-shaped ultrasonic content
        #                    above the audible band so it doesn't
        #                    intermodulate inside the encoder.
        #   volume=-6dB    — headroom for remaining transients now that
        #                    the highpass has restored proper bipolar
        #                    swing.  Without this the s24le encoder
        #                    still clips on percussion peaks.
        cmd += ["-af", "highpass=f=20,lowpass=f=40000,volume=-6dB"]
    elif bytes_per_sample == 2:
        # 24 → 16 bit reduction: ask for TPDF dither.  swresample
        # honours ``dither_method`` directly without needing soxr.
        cmd += ["-af", "aresample=dither_method=triangular_hp"]
    cmd += [
        "-f", "s24le",
        "-acodec", "pcm_s24le",
        "-progress", "pipe:2",
        "pipe:1",
    ]

    # Cap the wait on the render semaphore — if the box is so overloaded
    # that all render slots have been busy for 30 s, returning 503 is far
    # kinder than parking the request forever (the client would otherwise
    # see the audio element silently stall).
    try:
        await asyncio.wait_for(_render_sem.acquire(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(503, "Server busy, retry shortly")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Ring buffer of the last 4 KB of stderr — when ffmpeg exits with a
        # non-zero status the operator deserves to see *why*, not just the
        # return code.  We keep only the tail to avoid pinning megabytes of
        # error spam for a misbehaving encoder.
        stderr_ring = bytearray()
        _STDERR_RING_LIMIT = 4096

        async def _consume_progress() -> None:
            assert proc.stderr is not None
            last_broadcast_sec = -1
            try:
                while True:
                    raw = await proc.stderr.readline()
                    if not raw:
                        return
                    # Buffer the raw bytes for the failure path.
                    stderr_ring.extend(raw)
                    if len(stderr_ring) > _STDERR_RING_LIMIT:
                        del stderr_ring[: len(stderr_ring) - _STDERR_RING_LIMIT]
                    line = raw.decode("ascii", "replace").strip()
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k in ("out_time_us", "out_time_ms"):
                        try:
                            sec = int(v) / 1_000_000.0
                        except ValueError:
                            continue
                        pct = max(0.0, min(99.5, sec / source_duration * 100.0))
                        elapsed = time.time() - started_at
                        eta = max(0.0, elapsed * (100.0 - pct) / pct) if pct > 1.0 else None
                        entry = _TRANSCODE_PROGRESS.get(track_id)
                        if entry is not None and not entry.get("ready"):
                            entry["percent"] = pct
                            entry["eta_seconds"] = eta
                            # WS push (throttled ~1 Hz) so the determinate bar
                            # updates without the old per-tick HTTP poll; the
                            # poll endpoint still reads the entry every tick.
                            cur_sec = int(elapsed)
                            if cur_sec != last_broadcast_sec:
                                last_broadcast_sec = cur_sec
                                try:
                                    await _broadcast_transcode_progress({
                                        "event": "transcode_progress",
                                        "track_id": track_id,
                                        "percent": pct,
                                        "eta_seconds": eta,
                                        "ready": False,
                                    })
                                except Exception:
                                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        progress_task = asyncio.create_task(_consume_progress())

        # Open the partial file in append-binary mode — the header was
        # already written by the caller, so we just glue PCM frames on
        # the end.  ``f.flush() + os.fsync()`` isn't required because
        # response readers stat() the file's apparent size, which the
        # kernel updates as soon as bytes hit the page cache.
        bytes_written = 0
        clean_exit = False
        try:
            with open(wav_path, "ab") as f:
                # Track bytes since the last wakeup — fire the event every
                # ≥256 KB written.  Readers ``await`` this with a short
                # timeout so they wake on real progress rather than polling
                # the file size every 80 ms.
                bytes_since_event = 0
                inflight_for_event = _INFLIGHT_TRANSCODES.get(track_id)
                while True:
                    try:
                        chunk = await proc.stdout.read(65536)
                    except asyncio.CancelledError:
                        raise
                    if not chunk:
                        break
                    f.write(chunk)
                    f.flush()
                    bytes_written += len(chunk)
                    bytes_since_event += len(chunk)
                    # ≥256 KB of fresh data → wake any growing-file readers.
                    if bytes_since_event >= 256 * 1024:
                        if inflight_for_event is None:
                            inflight_for_event = _INFLIGHT_TRANSCODES.get(track_id)
                        if inflight_for_event is not None:
                            ev = inflight_for_event.get("data_event")
                            if ev is not None:
                                ev.set()
                                ev.clear()
                        bytes_since_event = 0

                # Top up to expected_data_bytes when ffmpeg's output is a
                # few hundred bytes short of the (duration × sample_rate)
                # estimate.  Routine off-by-N samples from rounding —
                # padding here keeps the in-flight wire response from
                # tripping NS_ERROR_NET_PARTIAL_TRANSFER on Firefox.
                shortfall = expected_data_bytes - bytes_written
                if 0 < shortfall <= 1_048_576:
                    f.write(b"\x00" * shortfall)
                    f.flush()
                    bytes_written += shortfall
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            # Always rewrite the WAV header to match the ACTUAL bytes we
            # wrote.  The pre-computed header assumed source_duration was
            # exactly right; in practice it can drift for a host of
            # reasons (DFF metadata quirks via the iff demuxer, a stale
            # store-side duration from an older ingest, ffmpeg's decoder
            # producing fewer samples than headline duration implies, or
            # the decoder exiting on a non-fatal warning).  An advertised
            # length that exceeds the real PCM is the source of the
            # "audio cuts off, seek shows silence" symptom — the browser
            # trusts the data chunk size, the timeline shows a longer
            # track than exists, range requests past real EOF return
            # nothing, and the user hears silence.  Patching the header
            # in place after we know how many bytes really landed makes
            # the file self-consistent.
            try:
                actual_data_bytes = bytes_written
                bps = bytes_per_sample
                actual_total_samples = actual_data_bytes // (channels * bps)
                correct_header = _build_wav_header(
                    sample_rate, channels, actual_total_samples,
                )
                with open(wav_path, "r+b") as hf:
                    hf.seek(0)
                    hf.write(correct_header)
                    hf.flush()
            except OSError:
                log.warning("Could not patch WAV header on %s", wav_path)

            # Clean exit if ffmpeg returned success AND we got at least
            # ~5 s of audio.  With the header now patched, the cache file
            # is self-consistent whatever the actual length turned out to
            # be — so we no longer need the old 95 %-of-estimate gate
            # that wrongly rejected renders when the source_duration
            # estimate was a hair too generous (the common path for
            # DSD/DFF files where ffprobe duration is brittle).
            min_acceptable = 5 * sample_rate * channels * bytes_per_sample
            clean_exit = (proc.returncode == 0 and bytes_written >= min_acceptable)
            if proc.returncode is not None and proc.returncode != 0:
                # Decode the stderr ring buffer for the operator log.  Cap the
                # log payload so a flood of warnings (e.g. corrupt-frame
                # spam) can't blow up disk or journald.
                tail = bytes(stderr_ring).decode("utf-8", errors="replace").strip()
                last_line = tail.splitlines()[-1] if tail else ""
                log.error(
                    "ffmpeg pump exit=%s for %s (cmd: %s)\nstderr tail:\n%s",
                    proc.returncode, src_path, " ".join(cmd), tail[-4096:],
                )
                # Surface the failure to the caller so the foreground stream
                # path returns a clean 502 instead of silently producing an
                # incomplete cache file.
                raise HTTPException(
                    502,
                    detail=f"ffmpeg failed (exit {proc.returncode}): {last_line}",
                )
        except asyncio.CancelledError:
            clean_exit = False
            raise
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                    try: await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, Exception): pass
                except ProcessLookupError:
                    pass
            if not progress_task.done():
                progress_task.cancel()
                try: await progress_task
                except (asyncio.CancelledError, Exception): pass

            # Wake any reader parked on more-data BEFORE we touch the
            # progress flag — those readers don't care whether the cache
            # has been promoted yet, only that no more bytes are coming.
            inflight = _INFLIGHT_TRANSCODES.get(track_id)
            if inflight is not None:
                inflight["complete_event"].set()
                inflight["clean_exit"] = clean_exit
                ev = inflight.get("data_event")
                if ev is not None:
                    ev.set()

            # Promote the .partial to its final cache name BEFORE marking
            # the progress entry ready.  The frontend polls
            # ``/transcode-status`` and the moment it sees ``ready: True``
            # it fires ``transcode-ready`` → app.js re-fetches the
            # waveform.  Previously the rename happened AFTER ready=True,
            # so the refresh hit ``get_cached(cache_key)`` → None → fell
            # back to ``_compute_waveform(path_str)`` where ``path_str``
            # for an FTP track is ``ftp://host/scan:/relative`` — ffmpeg
            # can't decode that pseudo-URL, returned empty stdout, the
            # waveform endpoint stored all-zeros, and the next call hit
            # the (now-poisoned) waveform fast-path forever.  Doing the
            # rename first means by the time ready=True propagates, the
            # cached WAV is at the path waveform computation looks up.
            if clean_exit and on_complete is not None:
                try:
                    await on_complete(wav_path)
                except Exception:
                    log.exception("on_complete failed for in-flight WAV pump")
            elif not clean_exit:
                try: wav_path.unlink()
                except OSError: pass

            entry = _TRANSCODE_PROGRESS.get(track_id)
            if entry is not None:
                if clean_exit:
                    entry["percent"] = 100.0
                    entry["eta_seconds"] = 0.0
                    entry["ready"] = True
                    entry["finished_at"] = time.time()
                else:
                    _TRANSCODE_PROGRESS.pop(track_id, None)
                # Terminal WS push — the client no longer continuously polls,
                # so it must learn ready/error over the socket: ready → 100% +
                # transcode-ready (PERC-9 waveform refresh); failure → badge
                # torn down.  The fallback watchdog only fires when NO push
                # arrives, so this terminal is required for the pump path.
                try:
                    if clean_exit:
                        await _broadcast_transcode_progress({
                            "event": "transcode_progress", "track_id": track_id,
                            "percent": 100.0, "eta_seconds": 0.0, "ready": True,
                        })
                    else:
                        await _broadcast_transcode_progress({
                            "event": "transcode_progress", "track_id": track_id,
                            "percent": 0.0, "eta_seconds": None,
                            "ready": False, "error": True,
                        })
                except Exception:
                    pass
    finally:
        _render_sem.release()


_INFLIGHT_CACHE_CODEC = "wav"
_INFLIGHT_CACHE_MIME = "audio/wav"


def _inflight_cache_key(track_id: str, target_rate: int | None) -> str:
    """Cache key for the adaptive in-flight WAV path.

    Pinned to ``codec="wav"`` so a future Subsonic ``?format=flac`` request
    gets its own slot and never collides with the WAV entry.  Target rate
    is part of the key so DSD-96 kHz and ALAC-source-rate cache to
    distinct files just like they did under the previous FLAC layout.
    """
    return _ck(track_id, "transcoded", subsong=0,
               codec=_INFLIGHT_CACHE_CODEC, target_rate=target_rate)


async def _get_or_start_inflight_wav(
    track_id: str,
    src_path: Path,
    track,
    target_rate: int | None,
    target_channels_hint: int | None,
) -> dict:
    """Return the in-flight dict for this track, starting a new render if
    none is running.  Shared by the foreground stream path and the
    prewarm path so both populate the same cache slot.

    The caller is responsible for incrementing ``subscribers`` if it
    intends to stream the file — prewarm doesn't, foreground does.
    """
    from soniqboom.core.conversion_cache import (
        store_cached, _cache_path as _ccp,
    )

    # First critical section — claim the slot quickly.  We hold the lock
    # only long enough to either find an existing entry or insert a
    # placeholder.  All slow work (ffprobe, header write, pump_task
    # spawn) happens OUTSIDE the lock so other tracks' cold starts aren't
    # serialised behind this one's 200-500 ms ffprobe.
    we_own_setup = False
    async with _INFLIGHT_LOCK:
        existing = _INFLIGHT_TRANSCODES.get(track_id)
        if existing is not None:
            inflight = existing
        else:
            inflight = {"setup_ready": asyncio.Event()}
            _INFLIGHT_TRANSCODES[track_id] = inflight
            we_own_setup = True

    if not we_own_setup:
        # Another coroutine owns the cold-start.  If it's still in setup,
        # wait for it; otherwise the dict is already fully populated.
        ready = inflight.get("setup_ready")
        if ready is not None and not ready.is_set():
            await ready.wait()
        return _INFLIGHT_TRANSCODES.get(track_id) or inflight

    setup_ready = inflight["setup_ready"]
    try:
        # Cold start — derive output params, pre-write the header,
        # spawn the pump task.  All I/O is OUTSIDE _INFLIGHT_LOCK now so
        # other tracks' cold starts don't block on this track's 200-500 ms
        # ffprobe + header write.
        #
        # ALWAYS ffprobe up front, even when track.duration looks
        # plausible.  A stale or buggy stored value (especially for DSD
        # ingested before the _extract_dsd fallback chain landed) leads
        # to a WAV header that lies about the data chunk size; the
        # browser then plays the (correctly-rendered) PCM until the
        # advertised length elapses and substitutes silence for the
        # rest, regardless of seek.  Patching the header after render
        # makes the cache file self-consistent for subsequent plays,
        # but the *first* response has already sent the wrong header.
        # Probing the source once up front (~ a few hundred ms on a
        # local file) keeps the first play honest too.
        info = await _probe_source_info(src_path)
        probed_dur = info.get("duration") if info else None
        stored_dur = float(getattr(track, "duration", 0) or 0) or None
        # Prefer the probe.  Fall back to the stored value only if the
        # probe failed outright.
        src_dur = probed_dur or stored_dur
        src_sample_rate = info.get("sample_rate") if info else None
        src_channels = info.get("channels") if info else None
        if not src_dur or src_dur <= 0:
            # Last-ditch probe: try opening with ffmpeg in null-mux mode
            # so it walks the entire file and reports a duration.  This
            # is slow (decodes the whole stream) but recovers DSF files
            # whose container header omits or lies about duration.  We
            # bound the wait at 30 s — long enough for a full DSD walk
            # at ~160× realtime up to a 3-hour SACD, fast enough that a
            # genuinely corrupt file (TABIJI.dff in the user's library:
            # "Invalid data found when processing input") still surfaces
            # a clear error inside the request timeout.
            try:
                proc = await asyncio.create_subprocess_exec(
                    settings.ffmpeg_path or "ffmpeg",
                    "-hide_banner", "-loglevel", "error",
                    "-nostats",
                    "-i", str(src_path),
                    "-vn", "-f", "null", "-",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=30,
                    )
                except asyncio.TimeoutError:
                    try: proc.kill()
                    except ProcessLookupError: pass
                    stderr_bytes = b""
                # ffmpeg writes a final "size= ... time=HH:MM:SS.ms ..."
                # status line when -loglevel info, but we're at error.
                # Re-run with stats enabled if the first probe failed.
                if stderr_bytes:
                    import re as _re
                    m = _re.search(
                        rb"time=(\d+):(\d{2}):(\d{2})(?:\.(\d+))?",
                        stderr_bytes,
                    )
                    if m:
                        h, mi, s, frac = m.groups()
                        src_dur = int(h) * 3600 + int(mi) * 60 + int(s)
                        if frac:
                            src_dur += float(f"0.{frac.decode()}")
            except Exception:
                log.exception("Duration last-ditch probe failed for %s", src_path)
        # If the probe failed but ffmpeg-walk recovered a duration, use it.
        # If both failed AND ffmpeg can't open the file (corrupt source),
        # surface a 415 with the file path so the user can investigate
        # rather than seeing an opaque 500.
        if not src_dur or src_dur <= 0:
            raise HTTPException(
                415,
                f"Cannot determine duration for {Path(src_path).name} — "
                "the file may be corrupt or use an unsupported variant. "
                "Try ffmpeg on it directly to confirm.",
            )

        eff_rate = target_rate or src_sample_rate or 48000
        eff_channels = target_channels_hint or src_channels or 2

        cache_key = _inflight_cache_key(track_id, target_rate)
        final_path = _ccp(cache_key, "transcoded")
        partial_path = final_path.with_suffix(".partial.wav")
        partial_path.parent.mkdir(parents=True, exist_ok=True)

        total_samples = int(round(src_dur * eff_rate))
        header_bytes = _build_wav_header(eff_rate, eff_channels, total_samples)
        with open(partial_path, "wb") as f:
            f.write(header_bytes)
        # 3 bytes per sample (s24le) — matches _pump_pcm_to_wav's
        # bytes_per_sample.  Mismatched accounting here was the source of
        # mid-track "audio cuts off, seek to silence" symptoms when the
        # cache file was 16-bit but the header advertised 24-bit, or
        # vice versa.
        expected_size = _WAV_HEADER_LEN + total_samples * eff_channels * 3

        complete_event = asyncio.Event()
        data_event = asyncio.Event()

        async def _on_complete(wav_path: Path) -> None:
            try:
                await store_cached(cache_key, "transcoded", wav_path)
            except Exception:
                log.exception("store_cached failed for in-flight WAV %s", track_id)

        pump_task = asyncio.create_task(_pump_pcm_to_wav(
            track_id=track_id,
            src_path=src_path,
            wav_path=partial_path,
            sample_rate=eff_rate,
            channels=eff_channels,
            source_duration=src_dur,
            cache_key=cache_key,
            format_type="transcoded",
            on_complete=_on_complete,
        ))

        # Second lock — publish the fully-populated inflight dict so other
        # subscribers can start reading.  Cheap critical section: just a
        # dict.update + Event.set().
        async with _INFLIGHT_LOCK:
            inflight.update({
                "wav_path": partial_path,
                "expected_size": expected_size,
                "pump_task": pump_task,
                "complete_event": complete_event,
                "data_event": data_event,
                "sample_rate": eff_rate,
                "channels": eff_channels,
                "source_duration": src_dur,
                "started_at": time.time(),
                "subscribers": 0,
                "clean_exit": False,
            })

        def _on_pump_done(_t: asyncio.Task) -> None:
            # _INFLIGHT_TRANSCODES mutation must be serialised against
            # other coroutines reading / inserting under _INFLIGHT_LOCK.
            # Schedule the pop as a task instead of doing it lock-free in
            # the callback — the previous implementation raced against a
            # subscriber-counter increment in _serve_inflight_wav and
            # could leak inflight entries (or orphan subscribers).
            async def _cleanup() -> None:
                async with _INFLIGHT_LOCK:
                    if _INFLIGHT_TRANSCODES.get(track_id) is inflight:
                        _INFLIGHT_TRANSCODES.pop(track_id, None)
            try:
                asyncio.create_task(_cleanup())
            except RuntimeError:
                # Loop already closed (interpreter shutdown) — best
                # effort lock-free pop.
                if _INFLIGHT_TRANSCODES.get(track_id) is inflight:
                    _INFLIGHT_TRANSCODES.pop(track_id, None)
        pump_task.add_done_callback(_on_pump_done)

        setup_ready.set()
        return inflight
    except Exception:
        # Setup failed — clear the sentinel slot and propagate.  Without
        # this, a failed cold start would leave a half-populated dict in
        # _INFLIGHT_TRANSCODES that the next caller would treat as live.
        async with _INFLIGHT_LOCK:
            if _INFLIGHT_TRANSCODES.get(track_id) is inflight:
                _INFLIGHT_TRANSCODES.pop(track_id, None)
        setup_ready.set()
        raise


async def _serve_inflight_wav(
    request: Request,
    track,
    src_path: Path,
    track_id: str,
    target_rate: int | None,
    target_channels_hint: int | None,
    original_codec_label: str,
    background_task,
) -> Response:
    """Adaptive cold-start dispatcher.

    States, in priority order:
      1. Cache hit         → serve final WAV with Range. Zero penalty.
      2. In-flight attach  → growing-file Range response against the
                              partial WAV that an earlier subscriber or
                              the prewarm path is already producing.
      3. Cold start        → kick off a new render via _get_or_start_inflight_wav
                              then attach as state 2.
    """
    from soniqboom.core.conversion_cache import (
        get_cached, pin as _pin, unpin as _unpin,
    )

    cache_key = _inflight_cache_key(track_id, target_rate)

    # Build a unpin-on-response-close background task that composes with
    # any existing cleanup the caller passed in.  Pinning at response
    # start + unpinning when the response closes is what makes the
    # conversion-cache's refcounted pin model actually work — without
    # the matching unpin every play permanently anchored its cache entry
    # and LRU eviction silently became a no-op (R2/R3 finding).
    def _make_unpin_task(prior_task):
        def _do_unpin():
            try:
                _unpin(cache_key)
            except Exception:
                pass
            if prior_task is not None:
                try:
                    prior_task()
                except Exception:
                    pass
        return BackgroundTask(_do_unpin)

    cached_path = await get_cached(cache_key)
    if cached_path is not None:
        _pin(cache_key)
        return await _range_file_response(
            request, cached_path, media_type=_INFLIGHT_CACHE_MIME,
            headers={"X-Transcoded": "1", "X-Original-Codec": original_codec_label,
                     "X-Target-Codec": _INFLIGHT_CACHE_CODEC, "X-Cache": "hit"},
            background=_make_unpin_task(background_task),
        )

    inflight = await _get_or_start_inflight_wav(
        track_id=track_id, src_path=src_path, track=track,
        target_rate=target_rate, target_channels_hint=target_channels_hint,
    )
    # Track foreground subscribers only — the prewarm path attaches but
    # doesn't count, so this header reflects "active listeners".
    async with _INFLIGHT_LOCK:
        inflight["subscribers"] += 1

    _pin(cache_key)

    headers = {
        "X-Transcoded": "1",
        "X-Original-Codec": original_codec_label,
        "X-Target-Codec": _INFLIGHT_CACHE_CODEC,
        "X-Cache": "miss-inflight",
        "X-Inflight-Subscribers": str(inflight["subscribers"]),
    }
    if original_codec_label == "dsd":
        headers["X-DSD-Output-Rate"] = str(inflight["sample_rate"])

    # ── PERC-9: hybrid chunked first-play vs Range path ───────────────
    # The chunked path is gated on (a) the request being an initial
    # open-ended GET and (b) the request coming from our own web UI
    # (identified by the session cookie).  Why scope it?
    #
    #   • Subsonic clients (Amperfy, DSub, Symfonium, play:Sub) flow
    #     through this same _serve_inflight_wav via subsonic.py
    #     forwarding to stream_track.  Many of them require
    #     ``Content-Length`` for their seek bar + offline-download UI,
    #     and some choke on chunked transfer-encoding.  Keeping their
    #     responses on the Range path means: byte-accurate Content-
    #     Length, no Subsonic regression.
    #
    #   • DLNA renderers (LG WebOS TV, Sonos S2, strict Samsung) that
    #     pull a DSD through /cast/{token}/ also reach stream_track →
    #     _serve_inflight_wav for the inflight-WAV format.  DLNA
    #     Networked Device Guidelines §7.4 explicitly call out
    #     Content-Length as required for certain transferMode values.
    #     Chunked would silently break Sonos.
    #
    # Detection: the SoniqBoom browser UI authenticates via the
    # ``sb_session`` cookie.  Subsonic clients authenticate via
    # ``?u=&p=`` (or ``?u=&s=&t=``), no cookie.  DLNA cast tokens
    # authenticate via the path-embedded JWT, no cookie either.  So a
    # session-cookie presence is the cleanest signal for "this is our
    # web UI" without an explicit User-Agent sniff.
    is_web_ui = bool(request.cookies.get("sb_session"))
    range_hdr = (request.headers.get("range") or "").strip()
    is_initial_get = (
        not range_hdr
        or range_hdr in ("bytes=0-", "bytes=0-0")
        or range_hdr == "bytes=0-1"  # probe range some browsers send
    )
    if is_web_ui and is_initial_get:
        return await _chunked_growing_file_response(
            request,
            inflight["wav_path"],
            inflight["expected_size"],
            inflight["complete_event"],
            media_type=_INFLIGHT_CACHE_MIME,
            headers=headers,
            data_event=inflight.get("data_event"),
            inflight=inflight,
            unpin_key=cache_key,
        )
    return await _growing_file_range_response(
        request,
        inflight["wav_path"],
        inflight["expected_size"],
        inflight["complete_event"],
        media_type=_INFLIGHT_CACHE_MIME,
        headers=headers,
        data_event=inflight.get("data_event"),
        inflight=inflight,
        unpin_key=cache_key,
    )


async def _chunked_growing_file_response(
    request: Request,
    file_path: Path,
    expected_size: int,
    complete_event: asyncio.Event,
    media_type: str,
    headers: dict[str, str] | None = None,
    data_event: asyncio.Event | None = None,
    inflight: dict | None = None,
    unpin_key: str | None = None,
) -> Response:
    """Serve a growing inflight WAV via chunked transfer-encoding.

    Differs from ``_growing_file_range_response``:

      • No ``Content-Length`` → ``Transfer-Encoding: chunked`` implied
        by Starlette.  Browsers don't gate on HAVE_FUTURE_DATA at all;
        playback starts as soon as the WAV header is read and the
        first PCM chunk arrives.
      • Always starts from offset 0.  This is the "first play, cold
        cache" path — subsequent Range requests from the same browser
        (seeks, prefetches) are routed to the Range-served path which
        DOES handle byte ranges.
      • Reads via ``os.pread`` so this response and any concurrent
        Range readers don't fight over a shared file offset.

    The trade-off is no seeking during this single response — but the
    moment the file is promoted to the conversion cache (post-pump
    completion), the next request goes to the cache-hit fast path
    with full Range support.
    """
    extra = dict(headers or {})
    # KEEP Accept-Ranges: bytes even though THIS response is chunked.
    # The header signals to the browser "the resource supports byte
    # ranges" — it doesn't claim THIS specific response does.  When the
    # user seeks, the browser tears down the chunked connection and
    # issues a new GET with a Range header; the dispatcher routes that
    # to ``_growing_file_range_response`` (against the still-growing
    # partial WAV) or the cache-hit fast path if the transcode has
    # finished.  Without this header, Chrome / Safari permanently
    # disable seeking on the resource because the FIRST response said
    # it wasn't seekable — even after the cache populates, the audio
    # element refuses to issue further range requests for that URL.
    # (Verified 2026-05-23 against the user's DSD playback regression.)
    extra["Accept-Ranges"] = "bytes"
    extra["X-Stream-Mode"] = "chunked-inflight"

    async def _stream_pcm():
        pin_released = False

        def _release_pin_once():
            nonlocal pin_released
            if pin_released:
                return
            pin_released = True
            if unpin_key is not None:
                try:
                    from soniqboom.core.conversion_cache import unpin
                    unpin(unpin_key)
                except Exception:
                    pass

        try:
            fd = await asyncio.to_thread(
                os.open, str(file_path), os.O_RDONLY,
            )
        except OSError as exc:
            log.warning("chunked-inflight: open failed for %s: %s",
                        file_path, exc)
            _release_pin_once()
            return

        pos = 0
        try:
            while True:
                # Read whatever is currently available.  pread doesn't
                # advance a shared offset, so concurrent Range readers
                # against the same fd-target don't interfere.
                try:
                    chunk = await asyncio.to_thread(
                        os.pread, fd, _RANGE_STREAMING_CHUNK, pos,
                    )
                except OSError as exc:
                    log.warning("chunked-inflight: pread failed at %d: %s",
                                pos, exc)
                    break
                if chunk:
                    yield chunk
                    pos += len(chunk)
                    continue
                # No new bytes — either ffmpeg is still writing or it's done.
                if complete_event.is_set():
                    # ffmpeg has finished.  If we've sent everything, exit.
                    # If ffmpeg under-wrote vs the WAV header's stated
                    # data-chunk size (rounding on DSF duration), pad
                    # with silence so the browser's WAV duration check
                    # doesn't trip NS_ERROR_NET_PARTIAL_TRANSFER on
                    # Firefox or a silent cut-off on Chrome.
                    if pos >= expected_size:
                        return
                    pad_left = expected_size - pos
                    while pad_left > 0:
                        n = min(_RANGE_STREAMING_CHUNK, pad_left)
                        yield b"\x00" * n
                        pad_left -= n
                    return
                # Wait for the pump to signal new data (or short-poll
                # if the inflight wiring didn't expose data_event).
                if data_event is not None:
                    try:
                        await asyncio.wait_for(
                            data_event.wait(),
                            timeout=_GROWING_READ_TIMEOUT,
                        )
                        data_event.clear()
                    except asyncio.TimeoutError:
                        # No data in 60 s — assume the pump is stuck.
                        log.warning(
                            "chunked-inflight: no data in %ds, ending stream at %d",
                            int(_GROWING_READ_TIMEOUT), pos,
                        )
                        break
                else:
                    await asyncio.sleep(_GROWING_POLL_INTERVAL)
        finally:
            try:
                await asyncio.to_thread(os.close, fd)
            except OSError:
                pass
            # Decrement subscriber counter symmetrically with the
            # Range-served path; the pump_task's own cleanup handles
            # the inflight dict eviction.
            if inflight is not None:
                try:
                    async with _INFLIGHT_LOCK:
                        inflight["subscribers"] = max(
                            0, inflight.get("subscribers", 1) - 1,
                        )
                except Exception:
                    pass
            _release_pin_once()

    return StreamingResponse(
        _stream_pcm(),
        status_code=200,
        media_type=media_type,
        headers=extra,
    )


async def _growing_file_range_response(
    request: Request,
    file_path: Path,
    expected_size: int,
    complete_event: asyncio.Event,
    media_type: str,
    headers: dict[str, str] | None = None,
    data_event: asyncio.Event | None = None,
    inflight: dict | None = None,
    unpin_key: str | None = None,
) -> Response:
    """Serve a file that's still being written.

    ``expected_size`` is the FINAL size — known up front because the WAV
    header carries duration × byte-rate.  Range requests against bytes
    that haven't been written yet wait on ``data_event`` (fired by the
    pump every ≥256 KB written) with a short timeout — wake-on-progress
    instead of the 80 ms poll loop that pre-dated this change.

    Crucially: ``Content-Length`` is the final expected size, not the
    current size.  Browsers compute ``audio.duration`` and the seek
    range from this value — getting it right is what makes the timeline
    correct from the very first byte of header.
    """
    extra = dict(headers or {})
    extra["Accept-Ranges"] = "bytes"

    # Parse the Range header (single-range only — same convention as
    # ``_range_file_response``).
    range_hdr = request.headers.get("range")
    if range_hdr and range_hdr.strip().startswith("bytes="):
        spec = range_hdr.strip()[6:]
        parts = spec.split("-", 1)
        try:
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else expected_size - 1
        except ValueError:
            start, end = 0, expected_size - 1
        start = max(0, min(start, expected_size - 1))
        end = max(start, min(end, expected_size - 1))
        status_code = 206
        extra["Content-Range"] = f"bytes {start}-{end}/{expected_size}"
    else:
        start, end = 0, expected_size - 1
        status_code = 200
    length = end - start + 1
    extra["Content-Length"] = str(length)

    # Generate silent PCM padding lazily in 64 KB chunks — used when
    # ffmpeg's output undershoots the expected_size we promised in
    # Content-Length.  Routine off-by-N samples from duration-vs-actual
    # rounding would otherwise truncate the response and trip
    # NS_ERROR_NET_PARTIAL_TRANSFER on Firefox (silent cut-off on Chrome).
    _SILENT_CHUNK = b"\x00" * 65536

    async def _yield_silent_padding(pos: int, end: int):
        remaining = end - pos + 1
        while remaining > 0:
            sz = min(len(_SILENT_CHUNK), remaining)
            yield _SILENT_CHUNK if sz == len(_SILENT_CHUNK) else _SILENT_CHUNK[:sz]
            remaining -= sz

    async def _yield_growing_range():
        pos = start
        last_chunk = 65536
        try:
            # Open once and keep the descriptor for the duration of the
            # response.  Crucially: we size the file via ``os.fstat(fd)``,
            # NOT ``file_path.stat()`` — when the pump's on_complete runs
            # ``store_cached`` does ``os.replace(partial, final)``, which
            # removes the file at ``file_path`` from the namespace.  The
            # inode is still alive (our fd holds the last reference) and
            # ``read()``/``fstat()`` continue to work normally; only path
            # lookups fail.  Statting the path here meant "audio plays
            # for ~the browser's buffer-ahead window then goes silent"
            # because the OSError on the now-missing path triggered the
            # padding fallback before we'd actually drained the inode.
            with open(file_path, "rb") as f:
                fd = f.fileno()
                f.seek(pos)
                while pos <= end:
                    try:
                        current_size = os.fstat(fd).st_size
                    except OSError:
                        async for buf in _yield_silent_padding(pos, end):
                            yield buf
                        return
                    available_end = min(current_size, end + 1)
                    if pos < available_end:
                        to_read = min(last_chunk, available_end - pos)
                        chunk = f.read(to_read)
                        if not chunk:
                            async for buf in _yield_silent_padding(pos, end):
                                yield buf
                            return
                        yield chunk
                        pos += len(chunk)
                        continue

                    # Pending: bytes for ``pos`` haven't been written.
                    if complete_event.is_set():
                        # ffmpeg has exited.  Any shortfall here is the
                        # expected duration-vs-actual rounding gap — pad
                        # to satisfy Content-Length so Firefox doesn't
                        # raise NS_ERROR_NET_PARTIAL_TRANSFER.
                        async for buf in _yield_silent_padding(pos, end):
                            yield buf
                        return

                    deadline = time.time() + _GROWING_READ_TIMEOUT
                    while pos >= available_end:
                        # Event-driven wake: wait for the pump to signal
                        # fresh data (≥256 KB since last wake) OR for the
                        # poll-interval safety timeout in case the event
                        # was missed.  Trades a constant 80 ms poll for
                        # near-zero-overhead wakeup.
                        if data_event is not None:
                            try:
                                await asyncio.wait_for(
                                    data_event.wait(),
                                    timeout=0.2,
                                )
                            except asyncio.TimeoutError:
                                pass
                        else:
                            await asyncio.sleep(_GROWING_POLL_INTERVAL)
                        if complete_event.is_set():
                            break
                        if time.time() > deadline:
                            try:
                                cur = os.fstat(fd).st_size
                            except OSError:
                                cur = -1
                            log.warning(
                                "Growing-file response timed out waiting "
                                "for bytes >= %d (file size = %d, expected %d)",
                                pos, cur, expected_size,
                            )
                            async for buf in _yield_silent_padding(pos, end):
                                yield buf
                            return
                        try:
                            current_size = os.fstat(fd).st_size
                        except OSError:
                            async for buf in _yield_silent_padding(pos, end):
                                yield buf
                            return
                        available_end = min(current_size, end + 1)
        except asyncio.CancelledError:
            # Client disconnected mid-stream — just exit cleanly.
            raise
        finally:
            # Decrement subscriber count on response end (success, error,
            # or client disconnect).  The X-Inflight-Subscribers header
            # was set at response start so its value stays informational,
            # but the internal counter now stays accurate across the
            # full subscriber lifecycle.
            if inflight is not None:
                try:
                    async with _INFLIGHT_LOCK:
                        cur = inflight.get("subscribers", 0)
                        if cur > 0:
                            inflight["subscribers"] = cur - 1
                except Exception:
                    pass

    # Compose unpin into a BackgroundTask so the cache entry's refcount
    # drops as soon as the client closes the response — without this every
    # play would permanently anchor its cache entry and LRU eviction would
    # silently stop working (R2/R3 finding).
    bg = None
    if unpin_key is not None:
        from soniqboom.core.conversion_cache import unpin as _unpin
        def _do_unpin():
            try: _unpin(unpin_key)
            except Exception: pass
        bg = BackgroundTask(_do_unpin)

    return StreamingResponse(
        _yield_growing_range(),
        status_code=status_code,
        media_type=media_type,
        headers=extra,
        background=bg,
    )


# ── Transcode progress tracking ──────────────────────────────────────────────
# Indexed by track_id (not the cache key) so the frontend can poll without
# knowing the codec/sample-rate the server picked.  Cache invariants
# (per-key lock in conversion_cache + render semaphore here) guarantee at
# most one transcode runs per track at a time, so track_id is unambiguous.
#
# Each entry carries percent (0..100), eta_seconds (float | None), the
# wall-clock start time, the source duration, and ``ready`` (true once
# ffmpeg exits cleanly).  Stale entries get pruned on read so the dict
# stays bounded by "tracks currently transcoding".
_TRANSCODE_PROGRESS: dict[str, dict] = {}
_TRANSCODE_PROGRESS_TTL = 60.0   # seconds an entry survives after "ready"


def _prune_transcode_progress(now: float | None = None) -> None:
    """Drop progress entries older than TTL.  Cheap O(N) sweep; N is bounded
    by ``_RENDER_SLOTS`` × a small fan-out so we never need a heap."""
    now = now or time.time()
    stale = [
        k for k, v in _TRANSCODE_PROGRESS.items()
        if v.get("ready") and (now - v.get("finished_at", now)) > _TRANSCODE_PROGRESS_TTL
    ]
    for k in stale:
        _TRANSCODE_PROGRESS.pop(k, None)


async def _broadcast_transcode_progress(payload: dict) -> None:
    """Push a ``transcode_progress`` event to the library WebSocket fan-out.

    The WS connection manager and its ``_broadcast`` coroutine live in
    :mod:`soniqboom.api.library`.  We import it **lazily, inside this
    function body** rather than at module top so the two modules can keep
    importing each other without a load-order cycle (library.py imports
    stream-side state on connect; stream.py emits via library here).

    Best-effort: a failure to reach the WS layer (e.g. library not yet
    imported, no clients) must never break or stall the transcode itself.
    """
    try:
        from soniqboom.api.library import _broadcast
    except Exception as exc:  # pragma: no cover — import wiring only
        log.debug("transcode_progress broadcast import failed: %s", exc)
        return
    try:
        await _broadcast(payload)
    except Exception as exc:  # pragma: no cover — WS fan-out is best-effort
        log.debug("transcode_progress broadcast failed: %s", exc)


async def _probe_source_duration(path: Path) -> float | None:
    """Cheap ffprobe call for source duration (seconds), or None on failure.

    Bounded at 10 s — slow SMB shares occasionally hang ffprobe forever.
    Result feeds the determinate progress UI; on None, the badge stays
    indeterminate (legacy behaviour) — graceful degradation.
    """
    info = await _probe_source_info(path)
    return info.get("duration") if info else None


async def _probe_source_info(path: Path) -> dict | None:
    """Pull duration + sample_rate + channels in one ffprobe roundtrip.

    Returns ``{"duration": float, "sample_rate": int, "channels": int}``
    or None on failure.  Used by the in-flight WAV-cache path to size
    the response Content-Length exactly — no estimation — so the
    audio element can compute ``duration`` and serve Range requests
    against arbitrary positions from the moment the header is read.
    """
    bin_ = settings.ffmpeg_path
    probe = (str(Path(bin_).parent / "ffprobe") if bin_ else "ffprobe")
    if bin_ and not Path(probe).exists():
        probe = "ffprobe"
    try:
        proc = await asyncio.create_subprocess_exec(
            probe, "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels:format=duration",
            "-of", "default=noprint_wrappers=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return None
        out = stdout.decode("ascii", "replace")
        info: dict = {}
        for line in out.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k == "duration" and v and v != "N/A":
                try: info["duration"] = float(v)
                except ValueError: pass
            elif k == "sample_rate" and v and v != "N/A":
                try: info["sample_rate"] = int(v)
                except ValueError: pass
            elif k == "channels" and v and v != "N/A":
                try: info["channels"] = int(v)
                except ValueError: pass
        if info.get("duration", 0) > 0:
            return info
        return None
    except Exception:
        return None


async def _render_to_transcoded_flac(
    path: Path, target_rate: int | None = None,
    codec: str | None = None, bitrate_kbps: int | None = None,
    progress_key: str | None = None,
    source_duration: float | None = None,
) -> Path:
    """Run ffmpeg to produce a cached transcode for non-native sources.

    Writes to a real file so the result can be range-served, prewarmed by
    the N+1/N+2 path, and replayed without re-running ffmpeg.  Caller
    (``get_or_render``) handles the cache placement.

    ``codec`` overrides ``settings.transcode_format`` (used by the
    OpenSubsonic transcoding extension — client asks for mp3 instead
    of flac, etc.).  ``bitrate_kbps`` caps the output bitrate for
    lossy codecs.  ``target_rate`` sets the output sample rate.

    ``progress_key`` and ``source_duration`` together enable live
    progress reporting — ffmpeg's ``-progress pipe:1`` output is parsed
    into ``_TRANSCODE_PROGRESS`` so the UI can surface a determinate
    progress bar with ETA instead of an opaque spinner.  PhD-UX rationale
    (Hofman 2009; Card 1983; Nielsen): an indeterminate wait > 3 s
    *increases* perceived wait; a determinate one with a visible ETA
    consistently reads as faster than even no indicator at all.
    """
    fmt   = (codec or settings.transcode_format).lower()
    if fmt not in TRANSCODE_MIME:
        fmt = settings.transcode_format
    acodec = "flac" if fmt == "flac" else fmt
    tmp_out = tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False)
    tmp_out.close()
    out = Path(tmp_out.name)

    cmd = [settings.ffmpeg_path or "ffmpeg",
           "-hide_banner", "-loglevel", "error",
           "-nostats",
           "-y",
           "-i", str(path),
           # -threads 0 → ffmpeg picks max-useful (typically cpu_count).
           # The FLAC encoder used to run single-threaded with the old
           # default, leaving most of the box idle during a render.
           "-threads", "0",
           "-vn"]

    src_ext = path.suffix.lower()
    is_dsd_source = src_ext in _DSD_EXTS

    # ── Sample-rate clamp for lossy encoders ────────────────────────────
    # libmp3lame supports {8/11.025/12/16/22.05/24/32/44.1/48} kHz only —
    # asking for 88.2/96/192 kHz makes the encoder open-call fail before
    # writing any output ("Specified sample rate N is not supported by
    # the libmp3lame encoder").  The DSD path passes target_rate=96000
    # so DSF→FLAC stays hi-fi, but DSF→MP3 (Amperfy's default request)
    # exploded with that combo: 0 bytes written, the response framed a
    # "valid-looking WAV with no audio inside", the client streamed
    # silence and then immediately stopped on pause/resume.
    #
    # libvorbis tolerates arbitrary rates but most consumer DACs cap at
    # 48 kHz internally, so clamping there doesn't lose audible content.
    # AAC (libfdk_aac, native aac) is similar.
    eff_target_rate = target_rate
    _LOSSY_MAX_RATE = {
        "mp3":  48000,
        "ogg":  48000,
        "opus": 48000,
        "aac":  48000,
    }
    if eff_target_rate and fmt in _LOSSY_MAX_RATE:
        if eff_target_rate > _LOSSY_MAX_RATE[fmt]:
            log.info(
                "Transcode: clamping %s output rate %d → %d Hz "
                "(encoder limit; source %s)",
                fmt, eff_target_rate, _LOSSY_MAX_RATE[fmt], path.name,
            )
            eff_target_rate = _LOSSY_MAX_RATE[fmt]
    if eff_target_rate:
        cmd += ["-ar", str(eff_target_rate)]
    if bitrate_kbps and fmt != "flac":
        # FLAC is lossless — bitrate is determined by content, not a knob.
        cmd += ["-b:a", f"{bitrate_kbps}k"]

    # Audio-filter chain.  Two cases:
    #   - DSD source → low-pass below the noise-shaping band, then
    #     resample via the SoX precision-28 path with TPDF dither so the
    #     PCM faithfully represents the audible band.
    #   - Non-DSD 16-bit target → high-pass-triangular dither on the
    #     SoX resampler, applied to keep the noise floor smooth.
    #
    # FLAC output is always 24-bit unless we explicitly downshift, so the
    # 16-bit branch only triggers for callers asking for ``mp3``/``ogg``
    # via the transcoding extension (where the lossy codec itself does
    # the depth reduction internally — the dither is a no-op overhead
    # there but harmless).
    if is_dsd_source:
        # Same chain as ``_pump_pcm_to_wav`` — see that function's comment
        # for the full rationale.  The ``highpass=f=20`` is the load-
        # bearing fix: DSD's bit pattern for certain near-silence
        # segments decodes to a -1.0 DC rail instead of zero, which the
        # browser silences as a DC-bias speaker-protection event.
        # Verified 2026-05-23 against a Setsuna Ogiso DFF.
        cmd += ["-af", "highpass=f=20,lowpass=f=40000,volume=-6dB"]

    if fmt == "flac":
        # Cached output worth taking the time to compress properly —
        # level 5 is the FLAC reference default and produces ~30 % smaller
        # files than level 0 for ~3-5 % more encode time at this scale.
        # The cache hit on subsequent plays makes the trade-off lopsided
        # in favour of disk savings.
        cmd += ["-compression_level", "5"]
    if progress_key and source_duration:
        cmd += ["-progress", "pipe:1"]
    cmd += ["-f", fmt, "-acodec", acodec, str(out)]

    # Derive timeout from source duration when possible.  The size proxy
    # used here previously was wildly inaccurate for high-compression
    # codecs (a 4 MB Opus track might be 60 minutes long).  Source
    # duration ÷ realtime gives a far more honest worst-case wait.
    # Fall back to a generous size estimate only when the probe failed.
    if source_duration and source_duration > 0:
        timeout_s = min(3600, max(180, int(source_duration * 3)))
    else:
        timeout_s = 180
        try:
            st = await asyncio.to_thread(Path(path).stat)
            approx_secs = max(60, int(st.st_size / 32_000))
            timeout_s = min(3600, max(180, approx_secs * 2))
        except (OSError, AttributeError):
            pass

    # Fast path: no progress requested → reuse the shared renderer helper
    # so the standard semaphore + cancel cleanup applies unchanged.
    if not (progress_key and source_duration):
        await _await_renderer(cmd, out, timeout=timeout_s, kind="Transcode")
        # Sanity-check the output: a zero-byte ffmpeg result is poison
        # for the cache (next call serves an empty WAV/MP3/FLAC and the
        # client plays silence forever).  Most common cause: an encoder
        # parameter the source isn't compatible with (DSD→MP3 at 96 kHz
        # before the rate clamp; an opaque container ffmpeg can't open).
        # Unlink + raise so the caller surfaces 502 instead of caching
        # the bad output.
        try:
            sz = await asyncio.to_thread(out.stat)
            if sz.st_size == 0:
                try:
                    await asyncio.to_thread(out.unlink, missing_ok=True)
                except OSError:
                    pass
                raise HTTPException(
                    502,
                    f"Transcode produced no audio for {path.name} "
                    f"(codec={fmt}, target_rate={target_rate}); "
                    "check the server log for ffmpeg's error message.",
                )
        except FileNotFoundError:
            raise HTTPException(502, "Transcode produced no output.")
        return out

    # Progress path: spawn ffmpeg ourselves so we can read its
    # ``-progress`` pipe concurrently with waiting for the process to
    # exit.  Shares ``_render_sem`` with the standard helper so the box
    # never runs more concurrent transcodes than CPU/2.
    started_at = time.time()
    _TRANSCODE_PROGRESS[progress_key] = {
        "percent": 0.0,
        "eta_seconds": None,
        "started_at": started_at,
        "target_duration": float(source_duration),
        "ready": False,
        "finished_at": 0.0,
    }

    async with _render_sem:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Throttle WS pushes to ~1 Hz: ffmpeg emits ``out_time_*`` every
        # frame (tens of ticks/sec), but the badge only needs ~1 update/sec
        # to read as continuous motion.  We broadcast only when the whole
        # second of *elapsed wall-clock* changes; the in-memory entry is
        # still updated every tick so the back-compat HTTP poll stays fresh.
        last_broadcast_sec = -1

        async def _consume_progress() -> None:
            nonlocal last_broadcast_sec
            assert proc.stdout is not None
            try:
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        return
                    line = raw.decode("ascii", "replace").strip()
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k in ("out_time_us", "out_time_ms"):
                        # Both keys are microseconds in modern ffmpeg
                        # despite the historical ``_ms`` naming.
                        try:
                            sec = int(v) / 1_000_000.0
                        except ValueError:
                            continue
                        if source_duration <= 0:
                            continue
                        pct = max(0.0, min(99.5, sec / source_duration * 100.0))
                        elapsed = time.time() - started_at
                        if pct > 1.0:
                            eta = max(0.0, elapsed * (100.0 - pct) / pct)
                        else:
                            eta = None
                        entry = _TRANSCODE_PROGRESS.get(progress_key)
                        if entry is not None and not entry.get("ready"):
                            entry["percent"] = pct
                            entry["eta_seconds"] = eta
                            cur_sec = int(elapsed)
                            if cur_sec != last_broadcast_sec:
                                last_broadcast_sec = cur_sec
                                await _broadcast_transcode_progress({
                                    "event": "transcode_progress",
                                    "track_id": progress_key,
                                    "percent": pct,
                                    "eta_seconds": eta,
                                    "ready": False,
                                })
                    elif k == "progress" and v == "end":
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Progress reader exited on %s: %s", progress_key, exc)

        progress_task = asyncio.create_task(_consume_progress())

        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                Path(out).unlink(missing_ok=True)
                raise HTTPException(
                    504, f"Transcode render timed out after {int(timeout_s)}s",
                )
            if proc.returncode != 0:
                Path(out).unlink(missing_ok=True)
                raise HTTPException(
                    502, f"Transcode exited with status {proc.returncode}",
                )
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, Exception):
                        pass
                except ProcessLookupError:
                    pass
                Path(out).unlink(missing_ok=True)
            if not progress_task.done():
                progress_task.cancel()
                try:
                    await progress_task
                except (asyncio.CancelledError, Exception):
                    pass
            entry = _TRANSCODE_PROGRESS.get(progress_key)
            if entry is not None:
                if proc.returncode == 0:
                    entry["percent"] = 100.0
                    entry["eta_seconds"] = 0.0
                    entry["ready"] = True
                    entry["finished_at"] = time.time()
                    # Terminal event — always pushed (not throttled) so the
                    # badge flips to "ready" the instant the render lands.
                    await _broadcast_transcode_progress({
                        "event": "transcode_progress",
                        "track_id": progress_key,
                        "percent": 100.0,
                        "eta_seconds": 0.0,
                        "ready": True,
                    })
                else:
                    # Drop failed entries straight away so the frontend
                    # falls back to the indeterminate badge instead of
                    # spinning on a "stuck at 47 %" reading.
                    _TRANSCODE_PROGRESS.pop(progress_key, None)
                    # Terminal failure — tell clients to stop showing a
                    # determinate bar and fall back gracefully.
                    await _broadcast_transcode_progress({
                        "event": "transcode_progress",
                        "track_id": progress_key,
                        "percent": float(entry.get("percent") or 0.0),
                        "eta_seconds": None,
                        "ready": False,
                        "error": True,
                    })
    return out


async def _transcode_stream(path: Path, seek_sec: float = 0.0,
                            target_rate: int | None = None):
    """Yield chunks from ffmpeg transcoding to the configured output format.

    seek_sec > 0 uses a fast pre-input seek (-ss before -i) so the user can
    jump to any position in a transcoded stream without re-decoding from start.

    ``target_rate`` forces an output sample rate — used for DSD sources where
    the natural ffmpeg PCM output rate (176.4 kHz / 352.8 kHz) is wasteful
    over the wire and the audible content fits comfortably in 96 kHz FLAC.
    """
    fmt   = settings.transcode_format
    codec = "flac" if fmt == "flac" else fmt
    cmd   = [settings.ffmpeg_path, "-hide_banner", "-loglevel", "error"]
    if seek_sec > 0:
        # Place -ss before -i for keyframe-accurate fast seek
        cmd += ["-ss", f"{seek_sec:.3f}"]
    cmd += [
        "-i", str(path),
        "-vn",           # drop video/cover art
    ]
    if target_rate:
        cmd += ["-ar", str(target_rate)]
    cmd += [
        "-f", fmt,
        "-acodec", codec,
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # Per-read timeout — if ffmpeg blocks on a pathological input we don't
    # want the response generator to park indefinitely with the client still
    # holding the connection (the renderer-helper got this fix already; the
    # inline transcoder needed the same guard).
    try:
        # The previous 30s timeout was too aggressive: when the user pauses
        # playback, the browser stops reading from the connection, ffmpeg's
        # stdout pipe fills, ffmpeg blocks on its write, and no new chunks
        # arrive on this side — a legitimate pause looked like a hang.
        # 300s catches truly stuck renders while leaving room for the
        # normal "user wandered off" pattern.
        idle_timeout = 300
        while True:
            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.read(65536), timeout=idle_timeout,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "Transcode stream idle for %ds on %s — stream truncated",
                    idle_timeout, path,
                )
                break
            if not chunk:
                break
            yield chunk
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


@router.get("/{track_id}/render-status")
async def render_status(
    track_id: str,
    subsong: int = Query(default=0, ge=0),
):
    """Check SID render state for progressive playback.

    Returns the per-track target duration (honouring HVSC Songlengths), what's
    currently cached, and whether the full-duration version is ready.
    """
    from soniqboom.core.conversion_cache import (
        is_cache_ready, _cache_key, find_shorter_sid_entry,
    )
    # Mirror the per-track duration logic used by the SID stream branch so the
    # UI never reads a stale global default while playback honours HVSC.
    target_dur = settings.sid_default_duration
    track = await get_track(track_id)
    if track is not None:
        meta = track.__dict__ if hasattr(track, "__dict__") else {}
        hvsc_lengths = meta.get("hvsc_lengths") or []
        if hvsc_lengths and 0 <= subsong < len(hvsc_lengths):
            target_dur = int(round(float(hvsc_lengths[subsong])))
        elif meta.get("duration") and float(meta["duration"]) > 0:
            target_dur = int(round(float(meta["duration"])))
    target_dur = max(5, target_dur)

    full_key = _cache_key(track_id, "sid", subsong, duration=target_dur)
    full_ready = await is_cache_ready(full_key)

    cached_dur = target_dur if full_ready else 0
    partial = False

    if not full_ready:
        shorter = await find_shorter_sid_entry(track_id, subsong, target_dur)
        if shorter:
            cached_dur = shorter[1]
            partial = True

    return {
        "ready": full_ready,
        "partial": partial,
        "cached_seconds": cached_dur,
        "target_seconds": target_dur,
        "track_id": track_id,
    }


@router.get("/{track_id}/transcode-status")
async def transcode_status(track_id: str):
    """Determinate progress for an in-flight transcode (DSD / ALAC / AIFF / …).

    Returns ``{ready, in_progress, percent, eta_seconds, target_duration}``.
    Frontend polls this while the converting badge is up so the indeterminate
    spinner can be swapped for a determinate bar with a visible ETA — the
    single biggest perceived-latency lever once the wait genuinely exceeds
    ~3 s (Hofman 2009; Card 1983).
    """
    _prune_transcode_progress()
    entry = _TRANSCODE_PROGRESS.get(track_id)
    if entry is None:
        return {
            "ready": False,
            "in_progress": False,
            "percent": 0.0,
            "eta_seconds": None,
            "target_duration": 0.0,
            "track_id": track_id,
        }
    return {
        "ready": bool(entry.get("ready")),
        "in_progress": not entry.get("ready"),
        "percent": float(entry.get("percent") or 0.0),
        "eta_seconds": entry.get("eta_seconds"),
        "target_duration": float(entry.get("target_duration") or 0.0),
        "track_id": track_id,
    }


# ── Prewarm queue (lookahead transcode/render) ──────────────────────────────
# Bounded set of in-flight prewarm tasks.  The frontend asks us to prepare
# the next 1–2 tracks before they're needed; we kick off a background render
# of each so playback of N+1/N+2 is instant when the user (or `ended` event)
# advances.  Cap prevents runaway CPU when the user mashes Next: when the
# cap is reached, the oldest task is cancelled — preserves the most
# recently-requested (most relevant) prewarms.
from collections import OrderedDict as _OrderedDict
_prewarm_tasks: "_OrderedDict[str, asyncio.Task]" = _OrderedDict()
# Sized for ~5 active users × N+2 prewarm = 10, plus a little headroom for
# rapid-skip bursts where multiple tracks-ahead get queued before any
# completes.  Previously 4 was too tight — a 5-user playlist could push
# beyond cap and cancel still-relevant prewarms before they finished.
_PREWARM_CAP = 12

# Currently-streaming key, set by the stream handler and consulted by the
# prewarm FIFO so it never cancels the playing track's prewarm task (in the
# unlikely case the player asks us to prewarm the track it's already on,
# e.g. after a network blip / track reload).
_active_stream_keys: set[str] = set()


def _prewarm_key(track_id: str, fmt: str, subsong: int = 0) -> str:
    return f"{track_id}::{fmt}::{subsong}"


async def _do_prewarm(
    track_id: str, file_path: Path, ext: str, subsong: int,
) -> None:
    """Run the format-appropriate cached render for one track in the
    background.  Mirrors the routing in ``stream_track`` so the cache key
    matches exactly what playback will request later."""
    from soniqboom.core.conversion_cache import get_or_render
    try:
        if ext in _SID_EXTS:
            # Honour HVSC per-tune duration so the prewarm caches under the
            # same key the streaming path uses.
            target_dur = settings.sid_default_duration
            track = await get_track(track_id)
            if track is not None:
                meta = track.__dict__ if hasattr(track, "__dict__") else {}
                lengths = meta.get("hvsc_lengths") or []
                if lengths and 0 <= subsong < len(lengths):
                    target_dur = int(round(float(lengths[subsong])))
                elif meta.get("duration") and float(meta["duration"]) > 0:
                    target_dur = int(round(float(meta["duration"])))
            target_dur = max(5, target_dur)
            await get_or_render(
                track_id=track_id, format_type="sid", subsong=subsong,
                duration=target_dur,
                render_fn=lambda: _render_sid(file_path, subsong=subsong, duration=target_dur),
            )
        elif ext in _MIDI_EXTS:
            from soniqboom.config import get_active_soundfont
            sf = get_active_soundfont()
            await get_or_render(
                track_id=track_id, format_type="midi", subsong=0,
                soundfont_path=str(sf) if sf else "",
                render_fn=lambda: _render_midi(file_path),
            )
        elif ext in _HVL_EXTS:
            await get_or_render(
                track_id=track_id, format_type="hvl", subsong=subsong,
                render_fn=lambda: _render_hvl(file_path, subsong=subsong),
            )
        elif ext in _UADE_EXTS:
            await get_or_render(
                track_id=track_id, format_type="uade", subsong=subsong,
                render_fn=lambda: _render_uade(file_path, subsong=subsong),
            )
        elif ext == ".imf":
            await get_or_render(
                track_id=track_id, format_type="imf", subsong=subsong,
                render_fn=lambda: _render_imf(file_path, subsong=subsong),
            )
        elif ext in _ADLIB_EXTS:
            await get_or_render(
                track_id=track_id, format_type="adlib", subsong=subsong,
                render_fn=lambda: _render_adlib(file_path, subsong=subsong),
            )
        elif ext in _TRACKER_EXTS:
            await get_or_render(
                track_id=track_id, format_type="tracker", subsong=subsong,
                render_fn=lambda: _render_tracker(file_path, subsong=subsong),
            )
        elif ext in _GME_EXTS_STREAM:
            await get_or_render(
                track_id=track_id, format_type="gme", subsong=subsong,
                render_fn=lambda: _render_gme(file_path, subsong=subsong),
            )
        elif ext in _DSD_EXTS:
            # Same in-flight WAV path the foreground stream uses — the
            # cache key MUST match exactly or the user-driven play hits
            # cold start while the prewarm fills a different slot.
            tr = await get_track(track_id)
            if tr is None:
                return
            from soniqboom.core.conversion_cache import get_cached as _gc
            cache_key = _inflight_cache_key(track_id, _DSD_OUTPUT_RATE)
            if await _gc(cache_key) is not None:
                return  # already cached — prewarm is a no-op
            inflight = await _get_or_start_inflight_wav(
                track_id=track_id, src_path=file_path, track=tr,
                target_rate=_DSD_OUTPUT_RATE, target_channels_hint=2,
            )
            await inflight["pump_task"]
        elif ext not in NATIVE:
            # Catch-all transcode (ALAC, AIFF, M4A-ALAC, WavPack, MPC, …).
            tr = await get_track(track_id)
            if tr is None:
                return
            from soniqboom.core.conversion_cache import get_cached as _gc
            cache_key = _inflight_cache_key(track_id, None)
            if await _gc(cache_key) is not None:
                return
            inflight = await _get_or_start_inflight_wav(
                track_id=track_id, src_path=file_path, track=tr,
                target_rate=None, target_channels_hint=None,
            )
            await inflight["pump_task"]
        # Native formats need no prewarm — the browser HTTP cache + our
        # range handler handle it; the original 256 KB-Range trick in the
        # frontend covers them.
    except asyncio.CancelledError:
        log.debug("Prewarm cancelled for %s", track_id)
        raise
    except Exception as exc:
        log.info("Prewarm failed for %s (%s) — will render on demand: %s",
                 track_id, ext, exc)


@router.post("/{track_id}/prewarm")
async def prewarm(
    track_id: str,
    subsong: int = Query(default=0, ge=0),
    file_path: str | None = Query(default=None, alias="path"),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    request: Request = None,
):
    """Speculatively prepare a track's cached render in the background.

    Used by the player's look-ahead — when the current track is N seconds
    from ending, the client asks us to prewarm N+1 (and N+2 if applicable)
    so the transition is instant.  Returns immediately with a status
    summary; the actual render happens off-request.
    """
    _require_stream_auth(request, sb_session, u, p, s, t)
    track = await get_track(track_id)
    if not track:
        if file_path:
            track = await _ingest_on_demand(track_id, file_path)
        if not track:
            raise HTTPException(404, "Track not found")

    path_str = track.path
    # Remote shares: skip — pulling a remote file to prewarm could saturate
    # the network for nothing if the user changes their mind.
    if path_str.startswith(("smb://", "ftp://", "http://", "https://")):
        return {"status": "skipped", "reason": "remote source"}
    path = Path(path_str)
    if not path.is_file():
        return {"status": "skipped", "reason": "file missing"}
    ext = path.suffix.lower()

    # Native formats need no server-side prewarm.
    if ext in NATIVE:
        return {"status": "skipped", "reason": "native (no transcode needed)"}

    key = _prewarm_key(track_id, ext, subsong)
    existing = _prewarm_tasks.get(key)
    if existing is not None and not existing.done():
        # Refresh recency — keep this task alive when capacity pressure hits.
        _prewarm_tasks.move_to_end(key)
        return {"status": "already_running", "key": key}

    task = asyncio.create_task(_do_prewarm(track_id, path, ext, subsong))
    _prewarm_tasks[key] = task

    def _on_done(t: asyncio.Task) -> None:
        # Identity check: only pop if the registry still points at *this*
        # task.  A fresh prewarm for the same key may have arrived between
        # this task's completion and the callback running — popping
        # unconditionally would remove the SUCCESSOR (orphaning it from
        # the cap accounting + shutdown cleanup).
        if _prewarm_tasks.get(key) is t:
            _prewarm_tasks.pop(key, None)
    task.add_done_callback(_on_done)

    # FIFO cap: cancel the oldest in-flight prewarm if we're over budget.
    # Skip any prewarm whose track_id is currently pinned in the cache
    # (i.e. recently played) — those represent work the user is likely
    # still consuming, and cancelling them would force the next play to
    # re-render.  Falls back to plain FIFO if every task is pinned.
    from soniqboom.core.conversion_cache import _pin_refs as _cache_pinned
    while len(_prewarm_tasks) > _PREWARM_CAP:
        evict_key: str | None = None
        evict_track_id: str = ""
        for k in _prewarm_tasks:
            # Prewarm key is ``"{track_id}::{ext}::{subsong}"``.
            # ``_pin_refs`` is a dict { cache_key -> refcount }; we iterate
            # the keys to mirror the legacy ``_pinned`` set semantics.
            tid = k.split("::", 1)[0]
            if not any(p.startswith(tid) for p in _cache_pinned):
                evict_key = k
                evict_track_id = tid
                break
        if evict_key is None:
            # Everything left is pinned — fall back to oldest.
            evict_key = next(iter(_prewarm_tasks))
            log.debug("Prewarm cap reached + all pinned — cancelling %s anyway", evict_key)
        old_task = _prewarm_tasks.pop(evict_key)
        if not old_task.done():
            old_task.cancel()
            log.debug("Prewarm cap reached — cancelled %s", evict_key)

    return {"status": "queued", "key": key, "in_flight": len(_prewarm_tasks)}


async def _ingest_on_demand(track_id: str, file_path: str):
    """Extract metadata for a single file and upsert to store on-the-fly.

    Called when the stream endpoint receives a track_id that isn't in the
    store yet, but a ``path`` query parameter was provided (e.g. from the
    fstree browser).  This lets users play files immediately without waiting
    for a full library scan.

    Security: TWO gates protect against arbitrary file access.
    1. The path must hash to the expected ``track_id`` (uuid5).  Defeats
       a casual ``?path=/etc/passwd&track_id=fake-uuid`` attack.
    2. The path must resolve under one of the configured scan dirs.
       Defeats the more sophisticated attack where the caller computes
       ``uuid5(NAMESPACE_URL, "/etc/passwd")`` themselves and supplies a
       matching ``track_id`` — uuid5 is deterministic, so step 1 alone
       can be bypassed by anyone who reads the source.
    """
    from soniqboom.core.data import list_scan_dirs, path_hash, upsert_track
    from soniqboom.core.metadata import extract
    from soniqboom.models.track import Track

    # Verify the path produces the expected track_id
    expected_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, file_path))
    if expected_id != track_id:
        log.warning("On-demand ingest: path hash mismatch for %s", track_id)
        return None

    # Containment check: resolve any symlinks, then ensure the resulting
    # path sits under one of the operator-configured scan roots.  ``::``
    # paths (zip-contained) are split on the outer archive first.  All
    # ``Path.resolve`` calls go through ``asyncio.to_thread`` — resolving
    # a path with a symlink chain on a slow share otherwise blocks the
    # event loop for 50-200 ms per call, and we make N+1 of them here.
    try:
        outer_path = file_path.split("::", 1)[0] if "::" in file_path else file_path
        resolved = await asyncio.to_thread(
            Path(outer_path).resolve, False,
        )
        roots = await list_scan_dirs()
        local_roots = [
            r for r in roots
            if not str(r.get("path", "")).startswith(
                ("smb://", "ftp://", "http://", "https://"),
            )
        ]

        def _resolve_roots() -> list[Path]:
            return [Path(r["path"]).resolve(strict=False) for r in local_roots]

        allowed_roots = await asyncio.to_thread(_resolve_roots)
        contained = any(
            resolved == root or root in resolved.parents
            for root in allowed_roots
        )
        if not contained:
            log.warning(
                "On-demand ingest rejected — path %s is outside any scan dir",
                outer_path,
            )
            return None
    except (OSError, ValueError) as exc:
        log.warning("On-demand ingest containment probe failed for %s: %s",
                    file_path, exc)
        return None

    loop = asyncio.get_running_loop()

    def _do_extract():
        """Synchronous extraction — runs in thread pool."""
        p = Path(file_path)
        if '::' in file_path:
            from soniqboom.core.scanner import _extract_from_zip
            meta = _extract_from_zip(file_path, track_id)
            actual = Path(file_path.split('::')[0])
        else:
            meta = extract(p, track_id)
            actual = p
        try:
            meta.mtime = actual.stat().st_mtime
        except OSError:
            pass
        return meta

    try:
        meta = await loop.run_in_executor(None, _do_extract)
    except Exception as exc:
        log.error("On-demand ingest extraction failed for %s: %s", file_path, exc)
        return None

    # Compute dir hash from parent directory
    if '::' in file_path:
        parent = str(Path(file_path.split('::')[0]).parent)
    else:
        parent = str(Path(file_path).parent)
    dir_h = path_hash(parent)

    # Find matching scan root (if any registered scan dir contains this path)
    root_h = ""
    try:
        scan_dirs = await list_scan_dirs()
        for sd in scan_dirs:
            sd_path = sd.get("path", "")
            if file_path.startswith(sd_path):
                root_h = path_hash(sd_path)
                break
    except Exception:
        pass

    meta_dict = meta.model_dump()
    meta_dict["dir_hash"] = dir_h
    meta_dict["scan_root_hash"] = root_h
    raw_art = meta_dict.pop("cover_art", None)
    meta_dict["cover_art"] = f"/api/art/{meta.id}" if raw_art else None

    try:
        track = Track(**meta_dict, embedding=[])
        await upsert_track(track)
        log.info("On-demand ingest: %s → %s", track.title or file_path, track_id[:12])
        return track
    except Exception as exc:
        log.error("On-demand ingest upsert failed for %s: %s", file_path, exc)
        return None


def _require_stream_auth(
    request: Request,
    sb_session: str | None,
    u: str | None,
    p: str | None,
    s: str | None = None,
    t: str | None = None,
) -> None:
    """Stream endpoint must be auth-gated: a track URL is otherwise a
    capability that anyone on the same network can exploit.  We accept

      • SoniqBoom session cookie       (browser SPA)
      • Subsonic-style ``?u=&p=``       (plain or ``enc:hex``)
      • Subsonic-style ``?u=&s=&t=``    (md5 token mode — Amperfy,
                                         DSub, Symfonium, play:Sub …)

    The Subsonic redirect path (``/rest/stream.view`` → ``/api/stream/{id}``)
    only works if every Subsonic auth mode the spec allows survives the
    307.  Before token mode was wired up here, Amperfy logged in fine
    against ``/rest/ping.view`` (handled inside subsonic.py with token
    support), then got 401 the moment it tried to actually stream a
    track — silent breakage from the user's perspective.

    **Cookie short-circuits first** — checking the session is a constant-
    time dict lookup; a typical Subsonic stream produces 8+ Range requests
    so calling scrypt on every one of them (~80 ms each) would block the
    event loop for nearly a second per track switch.  Pen-test #1 P0-2."""
    try:
        from soniqboom.core.users import get_user_store
        store = get_user_store()
    except Exception:
        return  # store not initialised — let through
    if not store.has_any():
        return  # fresh install, no users, no auth
    if sb_session:
        user = store.lookup_session(sb_session)
        if user and user.enabled:
            return
    if u and p:
        # Subsonic-style password.  ``enc:hex(plain)`` is the canonical
        # obfuscation; reject malformed hex with a clean 401 instead of
        # leaking a 500 + traceback (pen-test #2 P0-1).
        if p.startswith("enc:"):
            try:
                plain = bytes.fromhex(p[4:]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(401, "Malformed enc: password.")
        else:
            plain = p
        if store.authenticate(u, plain):
            return
        # Plain-mode fallback: if the user has only a Subsonic API
        # password configured (set via PUT /api/me/subsonic-password),
        # compare it directly.  Mirrors the logic in subsonic._resolve_user
        # so behaviour is consistent across /rest/* and /api/stream/.
        cand = store.get_by_username(u) if hasattr(store, "get_by_username") else None
        if cand and cand.subsonic_password:
            import hmac as _hmac
            if _hmac.compare_digest(plain, cand.subsonic_password):
                if cand.enabled:
                    return
    if u and s is not None and t is not None:
        # Subsonic token mode.  The token is md5(subsonic_password + salt);
        # we recompute and constant-time compare.  Same convention every
        # Subsonic-compatible server uses — see subsonic._resolve_user.
        import hashlib as _hashlib
        import hmac as _hmac
        cand = store.get_by_username(u) if hasattr(store, "get_by_username") else None
        if cand and cand.subsonic_password:
            expected = _hashlib.md5(
                (cand.subsonic_password + s).encode("utf-8")
            ).hexdigest()
            if _hmac.compare_digest(expected.lower(), t.lower()):
                if cand.enabled:
                    return
    raise HTTPException(401, "Sign in to stream tracks.")


@router.get("/{track_id}")
async def stream_track(
    track_id: str,
    request: Request,
    seek: float = Query(default=0.0, ge=0.0, description="Start position in seconds"),
    subsong: int = Query(default=0, ge=0, description="Sub-song index (SID/tracker)"),
    file_path: str | None = Query(default=None, alias="path",
                                  description="File path for on-demand ingestion"),
    # Per-request transcode hints from the OpenSubsonic transcoding extension
    # (or any client appending these to getStream).  Empty / 0 means "use the
    # server default" — preserves backward compatibility with old clients.
    target_format: str | None = Query(default=None, alias="format",
                                      max_length=16),
    max_bitrate_kbps: int = Query(default=0, alias="maxBitRate", ge=0, le=2_500_000),
    target_sample_rate: int = Query(default=0, alias="sampleRate", ge=0, le=384_000),
    # Force the on-demand transcode path even for ``NATIVE`` extensions
    # (.flac / .mp3 / .wav / .ogg / .opus).  Used by the client's
    # ``audio.error`` retry handler: when the browser bails with
    # ``MEDIA_ERR_SRC_NOT_SUPPORTED`` mid-stream on a FLAC with
    # corrupt-frame LOST_SYNC errors (or an MP3 with a bad MPEG header
    # somewhere in the middle), ffmpeg's libavcodec tolerates the bad
    # frames by resynchronising, so the transcoded WAV plays cleanly.
    # The query param is opt-in so healthy files keep the direct-byte-
    # range fast-path with zero overhead.
    force_transcode: bool = Query(default=False),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None, description="Subsonic auth username"),
    p: str | None = Query(default=None, description="Subsonic auth password"),
    s: str | None = Query(default=None, description="Subsonic token-mode salt"),
    t: str | None = Query(default=None, description="Subsonic token-mode hash"),
):
    # The cast byte-server (cast_stream.cast_stream) sets a ContextVar
    # AFTER it has validated the signed token in the URL path.  Reading
    # that ContextVar here lets us skip _require_stream_auth for the
    # anonymous cast path WITHOUT exposing a query-string toggle that
    # a malicious LAN client could append (the earlier "_internal_…"
    # kwarg approach was FastAPI-bindable as ?_internal_…=1, which
    # would have been an anonymous-stream bypass).
    if not _cast_internal_bypass_ctx.get():
        _require_stream_auth(request, sb_session, u, p, s, t)
    track = await get_track(track_id)
    if not track:
        # On-demand ingestion: if a file path was provided, extract metadata
        # and upsert to store so playback can proceed immediately.
        if file_path:
            track = await _ingest_on_demand(track_id, file_path)
        if not track:
            raise HTTPException(404, "Track not found")

    path_str = track.path
    _zip_tmp: Path | None = None  # temp file to clean up after streaming
    # ZIP-cache pin holder.  Initialise BEFORE the path-resolution branches
    # so the cleanup function (line ~2445) can reference it regardless of
    # which branch ran.  Without this default the remote (smb:// / ftp://)
    # branch never set the variable → UnboundLocalError → every remote
    # track 500'd on first byte (validation finding 2026-05-21).
    _zip_track_id_for_unpin: str | None = None

    if path_str.startswith(("smb://", "ftp://")) and "::" in path_str:
        # Remote ZIP member — ``ftp://host/share:/path/x.zip::member``.  Fetch
        # the OUTER archive to the local remote-cache, then extract the member
        # with the same machinery a local zip uses.  Handled before the generic
        # remote branch so the ``::member`` suffix isn't fetched as a literal
        # file name.
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        if not remote_path or "::" not in remote_path:
            raise HTTPException(400, "Remote archive path is malformed")
        source = get_source(scan_root)
        if source is None:
            raise HTTPException(503, "Network share unavailable — reconnect in Settings")
        zip_rel, _member = remote_path.split("::", 1)
        loop = asyncio.get_running_loop()
        try:
            _local_zip = await loop.run_in_executor(
                None, get_cache().fetch, scan_root, zip_rel, source,
            )
        except Exception as exc:
            if _is_file_not_found(exc):
                raise HTTPException(404, "Archive missing on source (rescan to refresh)")
            log.warning("Remote archive fetch failed for %s: %s", path_str, exc)
            raise HTTPException(502, "Could not fetch archive from network share")
        path = await _get_or_extract_zip_member(f"{_local_zip}::{_member}", track_id)
        if path is None:
            raise HTTPException(404, "Track missing inside the archive")
        _zip_pin(track_id)
        _zip_track_id_for_unpin = track_id
    elif path_str.startswith(("smb://", "ftp://")):
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        if not remote_path:
            raise HTTPException(400, "Remote path is malformed")
        source = get_source(scan_root)
        if source is None:
            raise HTTPException(503, "Network share unavailable — reconnect in Settings")

        # Try once; on failure ask the source to rebuild its connection and
        # retry ONE more time.  The source's own _connect already does
        # short inline retries, so this is the second tier: a brand-new
        # TCP session in case the pooled connection has been torn down
        # by the peer (FTP idle timeout, SMB session expire, router NAT flush).
        loop = asyncio.get_running_loop()
        try:
            path = await loop.run_in_executor(
                None, get_cache().fetch, scan_root, remote_path, source,
            )
        except Exception as exc:
            # File-not-found is a different class than "upstream broken":
            # it means the share is reachable and authenticated but the
            # specific path no longer exists (file moved/renamed/deleted
            # since the last scan).  Mapping it to 404 lets the player's
            # error toast say "Track or file missing on disk (rescan to
            # refresh)" instead of the misleading generic 502.  Reconnect
            # would be pointless — the file still won't be there.
            if _is_file_not_found(exc):
                log.info("Remote file missing for %s: %s", path_str, exc)
                # The track exists in our index but is gone on the source —
                # almost always means files were added/moved/deleted on
                # the share since the last walk.  Fire a background
                # freshness poll for this share NOW (the user is actively
                # trying to listen, they'll appreciate the immediate
                # refresh).  Fire-and-forget — the 404 response goes
                # back to the client without waiting on the scan.
                try:
                    from soniqboom.core import remote_freshness
                    asyncio.create_task(
                        remote_freshness.check_now(scan_root, reason="stream_404"),
                        name=f"freshness.stream_404[{scan_root}]",
                    )
                except Exception:
                    log.debug("freshness.check_now scheduling failed", exc_info=True)
                raise HTTPException(
                    404,
                    "File no longer at this path on the source. "
                    "Rescan the library to refresh.",
                )
            log.info(
                "Remote fetch failed for %s (%s: %s) — attempting reconnect",
                path_str, type(exc).__name__, exc,
            )
            # Cap the reconnect at 10 s so a genuinely-dead host doesn't
            # hold the request open for the full 46 s worst-case (3 attempts
            # × 15 s connect timeout + backoff).
            try:
                recovered = await asyncio.wait_for(
                    loop.run_in_executor(None, source.reconnect),
                    timeout=10.0,
                )
            except Exception:
                # TimeoutError (3.11+ aliased from asyncio.TimeoutError) plus
                # anything source.reconnect itself might raise — either way
                # the retry failed.
                recovered = False
            if recovered:
                try:
                    path = await loop.run_in_executor(
                        None, get_cache().fetch, scan_root, remote_path, source,
                    )
                    log.info("Remote fetch recovered after reconnect for %s", path_str)
                except Exception as exc2:
                    log.warning(
                        "Remote fetch failed after reconnect for %s: %s",
                        path_str, exc2,
                    )
                    if _is_file_not_found(exc2):
                        # Same trigger as above — second confirmation that the
                        # file is genuinely gone on the source warrants a poll.
                        try:
                            from soniqboom.core import remote_freshness
                            asyncio.create_task(
                                remote_freshness.check_now(scan_root, reason="stream_404"),
                                name=f"freshness.stream_404[{scan_root}]",
                            )
                        except Exception:
                            log.debug("freshness.check_now scheduling failed", exc_info=True)
                        raise HTTPException(
                            404,
                            "File no longer at this path on the source. "
                            "Rescan the library to refresh.",
                        )
                    raise HTTPException(502, f"Could not fetch remote file: {exc2}")
            else:
                log.warning("Remote fetch failed for %s: %s", path_str, exc)
                raise HTTPException(502, f"Could not fetch remote file: {exc}")
    elif '::' in path_str:
        # ZIP-contained file (supports nested zips via outer.zip::inner.zip::track.mod)
        #
        # Each HTTP Range request from a browser used to re-extract the
        # entire archive into a temp file and unlink it on response close.
        # On a 30 MB FLAC inside a ZIP that meant ~30 MB of disk I/O per
        # range — and Firefox / Chrome issue 5–20 range requests during
        # normal playback (preload, seek, mid-track buffer top-up).
        # Result: the player appeared to "buffer" constantly.
        #
        # Cache the extraction at a stable path keyed by track_id and
        # invalidate via the outer-zip mtime so a ZIP rebuild forces a
        # fresh extract.  Reused across every Range request for the
        # lifetime of the on-disk archive.
        path = await _get_or_extract_zip_member(path_str, track_id)
        if path is None:
            raise HTTPException(410, "ZIP archive not found or unreadable")
        # Pin the extract for the duration of the response so eviction
        # can't unlink a file we're mid-stream.  Unpin runs in the
        # response's BackgroundTask below.
        _zip_pin(track_id)
        _zip_track_id_for_unpin = track_id
    else:
        path = Path(path_str)
        if not path.exists():
            raise HTTPException(410, f"File not found on disk: {track.path}")

    ext = Path(path_str.split('::')[-1] if '::' in path_str else path_str).suffix.lower()

    def _cleanup_tmp():
        if _zip_tmp is not None:
            _zip_tmp.unlink(missing_ok=True)
        if _zip_track_id_for_unpin is not None:
            try: _zip_unpin(_zip_track_id_for_unpin)
            except Exception: pass

    # Single cleanup task for EVERY return branch below.  It unlinks any temp
    # AND — crucially — runs _zip_unpin so a zip-member extraction's pin is
    # released once the response finishes.  All branches (native AND the
    # rendered SID/MIDI/tracker/GME/HVL/UADE paths) pass background=_bg.  The
    # rendered paths previously used a _zip_bg that was always None, so every
    # play of a zip-contained rendered tune leaked its pin and the extraction
    # could never evict.
    _bg = BackgroundTask(_cleanup_tmp) if (_zip_tmp or _zip_track_id_for_unpin) else None

    # ── Rendered formats: SID / MIDI / Tracker ───────────────────────────────
    # These are cached as WAV files so repeat playback is instant.
    # On cache miss, the renderer runs and the result is stored for next time.
    from soniqboom.core.conversion_cache import get_or_render

    if ext in _SID_EXTS:
        from soniqboom.core.conversion_cache import (
            _cache_key, find_shorter_sid_entry,
            start_background_render, get_cached,
        )
        # Prefer per-track HVSC duration over the global default.  The
        # track record may carry ``hvsc_lengths`` (a list of per-subsong
        # durations) and/or a ``duration`` value already patched by the
        # HVSC rescan endpoint.  Fall back to the safety-cap default.
        target_dur = settings.sid_default_duration
        meta = track.__dict__ if hasattr(track, "__dict__") else {}
        hvsc_lengths = meta.get("hvsc_lengths") or []
        if hvsc_lengths and 0 <= subsong < len(hvsc_lengths):
            target_dur = int(round(float(hvsc_lengths[subsong])))
        elif meta.get("duration") and float(meta["duration"]) > 0:
            target_dur = int(round(float(meta["duration"])))
        # Clamp: extremely short or zero durations would produce empty
        # WAVs; cap to a minimum of 5s so we never feed sidplayfp -t0.
        target_dur = max(5, target_dur)

        full_key = _cache_key(track_id, "sid", subsong, duration=target_dur)

        # 1) Exact cache hit (correct duration)
        exact = await get_cached(full_key)
        if exact:
            return await _range_file_response(
                request, exact, media_type="audio/wav",
                headers={"X-Rendered": "sidplayfp", "X-Cache": "hit",
                         "X-SID-Target-Seconds": str(target_dur)},
                background=_bg,
            )

        # 2) Shorter version available — serve it now, render full in background
        shorter = await find_shorter_sid_entry(track_id, subsong, target_dur)
        if shorter:
            short_path, short_dur = shorter
            await start_background_render(
                full_key, "sid",
                lambda: _render_sid(path, subsong=subsong, duration=target_dur),
            )
            return await _range_file_response(
                request, short_path, media_type="audio/wav",
                headers={"X-Rendered": "sidplayfp", "X-Cache": "partial",
                         "X-SID-Cached-Seconds": str(short_dur),
                         "X-SID-Target-Seconds": str(target_dur)},
                background=_bg,
            )

        # 3) No cache at all — render synchronously
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="sid", subsong=subsong,
            duration=target_dur,
            render_fn=lambda: _render_sid(path, subsong=subsong, duration=target_dur),
        )
        return await _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "sidplayfp", "X-Cache": "hit" if hit else "miss",
                     "X-SID-Target-Seconds": str(target_dur)},
            background=_bg,
        )
    if ext in _MIDI_EXTS:
        from soniqboom.config import get_active_soundfont
        sf = get_active_soundfont()
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="midi", subsong=0,
            render_fn=lambda: _render_midi(path),
            soundfont_path=str(sf) if sf else "",
        )
        return await _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "fluidsynth", "X-Cache": "hit" if hit else "miss"},
            background=_bg,
        )
    # UADE goes BEFORE the tracker branch — both .ahx and .hvl are
    # technically listed in _TRACKER_EXTS for scanner-side detection,
    # but openmpt123 silently doesn't decode them.  Without this
    # priority, every .ahx play would 501 from inside _render_tracker.
    if ext in _HVL_EXTS:
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="hvl", subsong=subsong,
            render_fn=lambda: _render_hvl(path, subsong=subsong),
        )
        return await _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "hvl2wav", "X-Cache": "hit" if hit else "miss"},
            background=_bg,
        )
    if ext in _UADE_EXTS:
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="uade", subsong=subsong,
            render_fn=lambda: _render_uade(path, subsong=subsong),
        )
        return await _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "uade123", "X-Cache": "hit" if hit else "miss"},
            background=_bg,
        )
    # .imf is overloaded (Imago Orpheus tracker vs id/Apogee AdLib IMF) —
    # _render_imf disambiguates by content.  MUST come before _TRACKER_EXTS,
    # which still lists .imf for scanner-side detection.
    if ext == ".imf":
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="imf", subsong=subsong,
            render_fn=lambda: _render_imf(path, subsong=subsong),
        )
        return await _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "adplug/openmpt123", "X-Cache": "hit" if hit else "miss"},
            background=_bg,
        )
    if ext in _ADLIB_EXTS:
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="adlib", subsong=subsong,
            render_fn=lambda: _render_adlib(path, subsong=subsong),
        )
        return await _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "adplug", "X-Cache": "hit" if hit else "miss"},
            background=_bg,
        )
    if ext in _TRACKER_EXTS:
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="tracker", subsong=subsong,
            render_fn=lambda: _render_tracker(path, subsong=subsong),
        )
        return await _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "openmpt123", "X-Cache": "hit" if hit else "miss"},
            background=_bg,
        )
    if ext in _GME_EXTS_STREAM:
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="gme", subsong=subsong,
            render_fn=lambda: _render_gme(path, subsong=subsong),
        )
        return await _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "gme", "X-Cache": "hit" if hit else "miss"},
            background=_bg,
        )

    # ── Native: serve directly with Range support ─────────────────────────────
    # Skipped when:
    #   • ``force_transcode=1`` is on the URL — the client's ``audio.error``
    #     retry uses that to route the next attempt through ffmpeg, which
    #     tolerates corrupt-frame LOST_SYNC and produces a cleanly-demuxable
    #     WAV.  Healthy files still hit the fast path on the first attempt;
    #     only failing playbacks pay the transcode cost.
    #   • The client asked for a different codec via Subsonic's ``?format=``
    #     param (Amperfy/iOS always asks for ``format=mp3``, because iOS
    #     can decode MP3 from any AVPlayer URL; FLAC requires the file to
    #     either be served via the proper extension or routed through a
    #     framework component that's not always available on background
    #     threads).  Before this fan-out we'd hand Amperfy raw FLAC bytes
    #     labelled ``audio/flac`` regardless of its ``format=mp3`` request
    #     — Amperfy treated the resulting unintelligible stream as a
    #     zero-duration track and auto-advanced through the entire queue.
    #     Honour the explicit format hint and re-route through the
    #     transcoder for files whose source extension doesn't match.
    _src_codec = ext.lstrip(".")  # 'flac' / 'mp3' / 'wav' / 'ogg' / 'opus'
    _format_mismatch = bool(
        target_format
        and target_format.lower() in TRANSCODE_MIME
        and target_format.lower() != _src_codec
    )
    if ext in NATIVE and not force_transcode and not _format_mismatch:
        return await _range_file_response(
            request, path, media_type=NATIVE[ext],
            background=_bg,
        )

    # ── .m4a / .aac / .mp4 / .m4b / .m4r / .3gp: probe codec first ───────────
    # AAC in any MP4-family container → browsers can play it natively (serve
    # directly).  ALAC in .m4a/.mp4 → must transcode (Chrome/Firefox cannot
    # decode ALAC).  Probe result is reused in the transcode header to avoid
    # a second call.
    #
    # The container list was historically just (.m4a, .aac).  Real-world
    # libraries include .mp4 (Apple Books / podcasts), .m4b (audiobooks
    # specifically), .m4r (ringtones — surprisingly common in scraped
    # archives) and .3gp (mobile-origin recordings).  Treating these the
    # same as .m4a means an AAC-encoded audiobook plays without the
    # cold-start transcode penalty.
    detected_codec: str | None = None
    if ext in (".m4a", ".aac", ".mp4", ".m4b", ".m4r", ".3gp"):
        detected_codec = await _probe_codec(path)
        # Same format-mismatch guard as the NATIVE branch above — when a
        # Subsonic client (Amperfy, DSub, Symfonium) asks for ``format=mp3``
        # we must transcode, not serve raw AAC labelled as audio/mp4.
        _aac_mismatch = bool(
            target_format
            and target_format.lower() in TRANSCODE_MIME
            and target_format.lower() not in ("aac", "m4a")
        )
        if detected_codec == "aac" and not _aac_mismatch:
            return await _range_file_response(
                request, path, media_type="audio/mp4",
                background=_bg,
            )
        # Safari decodes ALAC natively; transcoding to FLAC would break it,
        # since Safari doesn't support raw audio/flac in <audio>.  Also
        # honour an explicit ``format=`` mismatch here so a client asking
        # for FLAC/MP3 from ALAC actually gets the requested codec.
        if detected_codec == "alac" and _is_safari(request) and not _aac_mismatch:
            return await _range_file_response(
                request, path, media_type="audio/mp4",
                background=_bg,
            )
        # ALAC on non-Safari, or unknown → fall through to transcode

    # Honour per-request transcode overrides from the OpenSubsonic transcoding
    # extension (or any caller appending ?format=&maxBitRate=&sampleRate=).
    # Empty / 0 falls back to the server-configured defaults, preserving
    # backward compatibility with old clients that never sent these.
    eff_codec = (target_format or settings.transcode_format).lower()
    if eff_codec not in TRANSCODE_MIME:
        eff_codec = settings.transcode_format
    eff_mime = TRANSCODE_MIME.get(eff_codec, "audio/flac")

    # ── Adaptive cold start (PERC-8) ─────────────────────────────────────────
    # Three states, in priority order:
    #
    #   1. Final cache hit  → serve the WAV from disk with Range.  ZERO
    #                          penalty, sub-50 ms first byte.
    #   2. In-flight render → attach to the growing WAV file already being
    #                          written by an earlier subscriber.  Headers
    #                          carry the FINAL Content-Length so the audio
    #                          element computes the correct duration and
    #                          seeks against any byte ≤ rendered-position.
    #                          Seeks beyond block briefly until ffmpeg
    #                          catches up — typical wait is < 1 s because
    #                          the render runs ~5–10× realtime.
    #   3. Cold start       → pre-write a 44-byte WAV header to the cache
    #                          file, spawn ffmpeg writing raw PCM, then
    #                          serve as state 2.  Audio starts as soon as
    #                          the first ~64 KB of PCM is on disk
    #                          (typically < 300 ms).
    #
    # Net effect: from the user's perspective the track plays "instantly"
    # whether it's cached or not.  The ~30 s wait that used to gate
    # cold DSD plays is gone.

    # Subsonic-style transcode hints can ask for a non-WAV codec.  When
    # they do, fall back to the legacy block-then-serve path because the
    # in-flight protocol only knows how to serve WAV (the only format
    # whose total byte count is computable up front without encoding).
    # In practice this branch fires only for Subsonic clients with the
    # transcodeOffload extension, ~5 % of plays.
    use_inflight = (target_format is None or target_format.lower() == "wav")

    if ext in _DSD_EXTS:
        # Client may downshift the DSD output rate (e.g. mobile asking
        # for 48 kHz).  Clamp to the DSD ceiling so we never *upsample*
        # past the native 96 kHz default.
        eff_rate = min(target_sample_rate or _DSD_OUTPUT_RATE, _DSD_OUTPUT_RATE)
        target_channels_hint = 2
        original_codec = "dsd"
    else:
        eff_rate = target_sample_rate or None
        target_channels_hint = None
        original_codec = detected_codec or ext.lstrip(".") or "unknown"

    if use_inflight:
        return await _serve_inflight_wav(
            request, track, path, track_id, eff_rate,
            target_channels_hint, original_codec, _bg,
        )

    # Subsonic transcodeOffload path — keep the legacy block-then-serve
    # for non-WAV codecs.  Old behaviour, no in-flight handling.
    cached_path, hit = await get_or_render(
        track_id=track_id, format_type="transcoded", subsong=0,
        codec=eff_codec, target_rate=eff_rate,
        render_fn=lambda: _render_to_transcoded_flac(
            path, target_rate=eff_rate, codec=eff_codec,
            bitrate_kbps=max_bitrate_kbps or None,
            progress_key=track_id,
            source_duration=float(getattr(track, "duration", 0) or 0) or None,
        ),
    )
    return await _range_file_response(
        request, cached_path, media_type=eff_mime,
        headers={"X-Transcoded": "1", "X-Original-Codec": original_codec,
                 "X-Target-Codec": eff_codec,
                 "X-Cache": "hit" if hit else "miss"},
        background=_bg,
    )
