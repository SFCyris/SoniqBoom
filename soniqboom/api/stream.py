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
import threading
import time
import uuid as _uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Body, Cookie, HTTPException, Query, Request
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
# 1 MB chunks: a 30 MB range is 30 os.pread hops through anyio's shared thread
# pool instead of 482 at 64 KB (16x fewer), and OS readahead makes the larger
# reads near-free.  Working set stays a few MB — trivial next to the track.
_RANGE_STREAMING_CHUNK = 1024 * 1024


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

    # Large slice → stream in ``_RANGE_STREAMING_CHUNK`` (1 MB) reads via os.pread
    # so we never hold the whole slice in RAM.  Five concurrent users seeking
    # around in 30 MB FLACs used to peak the worker at 150 MB of transient
    # buffers; chunked pread keeps the working set to a few MB.
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

# Formats ALL major browsers can decode natively (Chrome, Firefox, Safari) by
# extension alone.  ``.m4a``/``.aac`` are NOT here because the extension can't
# tell AAC (universal) from ALAC (Safari-only) — the stream handler probes the
# real codec (``_probe_codec``) and then direct-serves AAC to everyone and ALAC
# to Safari, transcoding only ALAC-on-non-Safari (see the "probe codec first"
# branch in ``stream_track``).  Ogg is native here but gated away from Safari
# < 18.4 at serve time (``_safari_lacks_ogg``), which can't decode Opus/Vorbis.
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

# ALL speculative/background renders — web N+1/N+2 prewarm AND the AdLib
# duration-probe batch — share this single low-priority gate so their COMBINED
# concurrency can never occupy the render slot a live play needs.  Capped one
# below the total: a background render holds this WHILE it waits for and holds
# ``_render_sem``, so at most ``_RENDER_SLOTS - 1`` render slots are ever held
# by background work in aggregate, leaving ≥1 permit free for the foreground
# stream path (which never touches ``_bg_render_sem``).  No deadlock: foreground
# never acquires ``_bg_render_sem``, so there is no circular wait.  ONE shared
# gate, not one-per-pool: two independent ``Semaphore(N-1)`` pools could each
# "leave 1 free" yet together take every slot, so the reservation must be global.
#
# KNOWN GAP: the Cast lookahead prewarm (core/cast_session.py) renders via
# ``conversion_cache.start_background_render`` and is NOT yet routed through this
# gate (gating it cleanly needs a conversion_cache change); those renders still
# acquire ``_render_sem`` so they're bounded by total slots, just not
# subordinated to foreground.  Cast is Beta.
#
# NOTE: an in-flight (DSD/transcode) prewarm that is FIFO-cap-cancelled releases
# this gate immediately, but its detached pump task keeps holding ``_render_sem``
# until the render completes (which populates the cache — the prewarm's goal),
# so the slot frees on natural completion; a benign transient, not a leak.
_bg_render_sem = asyncio.Semaphore(max(1, _RENDER_SLOTS - 1))


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
        # Capture stderr (was DEVNULL): a renderer's own diagnostics are the
        # only way to tell "the file isn't a valid module" (a clear 4xx the
        # listener can act on) apart from "the renderer/infra broke" (a 502).
        # ``communicate`` drains the pipe concurrently so a chatty renderer
        # (ffmpeg) can't deadlock on a full 64K stderr buffer.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stderr_data = b""
            try:
                _, stderr_data = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                Path(tmp_path).unlink(missing_ok=True)
                raise HTTPException(504, f"{kind} render timed out after {int(timeout)}s")
            if proc.returncode != 0:
                Path(tmp_path).unlink(missing_ok=True)
                err_text = (stderr_data or b"").decode("utf-8", "replace")
                # uade prints "module check failed" to stderr ONLY when the
                # input isn't a real Amiga module — e.g. a PC ``.dat``
                # misindexed as PaulRobotham by extension.  Surface that as a
                # clear 422 the frontend shows in the toast, instead of a
                # cryptic "uade renderer exited with status 1" 502.  Do NOT
                # match the generic "Can not play <name>" line — uade prints it
                # for a legit module whose companion sample half is missing too
                # ("score died"), which would be mislabelled as PC data.
                low = err_text.lower()
                if kind == "uade" and "module check failed" in low:
                    raise HTTPException(
                        422,
                        "This file isn't a playable Amiga module — it looks "
                        "like non-module data (e.g. a PC/DOS file) indexed by "
                        "mistake.",
                    )
                # A dynamic-LOADER failure: the binary is present but can't start
                # because a shared library was upgraded out from under it (e.g.
                # Homebrew bumped boost under a from-source zxtune123 → dyld
                # "Symbol not found").  An install/setup problem, not bad input —
                # give an actionable message, not a cryptic "exited with status -6".
                if any(m in low for m in (
                    "dyld", "symbol not found", "error while loading shared librar",
                    "image not found", "cannot open shared object",
                )):
                    log.error(
                        "%s renderer at %s FAILS TO LOAD (exit %s): %s — a system "
                        "library upgrade likely orphaned it; re-run install.sh",
                        kind, cmd[0], proc.returncode, err_text.strip()[:300])
                    raise HTTPException(
                        501,
                        f"The {kind} renderer is installed but can't run — a shared "
                        f"library was upgraded out from under it. Reinstall or "
                        f"rebuild the renderer to fix it.",
                    )
                if err_text.strip():
                    log.warning(
                        "%s renderer failed (exit %s): %s", kind,
                        proc.returncode, err_text.strip()[:500])
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

# sidplayfp's fixed WAV output format (verified: `sidplayfp -w` writes mono,
# 44100 Hz, 16-bit signed PCM).  Used to synthesise a full-length header for the
# progressive stream (sidplayfp itself only writes a header sized to what it has
# rendered so far, so its own header can't be streamed byte-0 for a growing file).
_SID_WAV_RATE = 44100
_SID_WAV_CHANNELS = 1
_SID_WAV_BITS = 16

# Strong refs to the detached progressive-SID finaliser tasks.  Each owns a
# render's reap+cache lifecycle independently of the response, so a client
# disconnect can't abort it; without a live ref asyncio may GC a bare task.
_SID_PROG_FINALISERS: set = set()

# Cap on concurrent progressive-SID render PROCESSES — live streams AND
# detached (abandoned-but-finishing, see below) renders share one pool, sized
# by the ``sid_render_parallel`` setting (default 3; each sidplayfp holds a CPU
# core at ~15x realtime plus a growing temp file).  Admission is live-first:
# a new play evicts the oldest detached render when the pool is full, and only
# falls back to the blocking path when every slot is a live stream.  A
# 1-element list so the generator / finaliser can mutate the count without a
# ``global`` declaration.  (A killed victim's slot is released synchronously at
# eviction; its finaliser's own release is idempotent.)
_SID_PROG_ACTIVE = [0]

# Detached renders: a listener skipped away mid-tune, but the render is left
# to FINISH and be cached — previously the temp was discarded on disconnect,
# so a tune you never played to the end could never become warm ("came back
# and it wasn't cached", measured live).  Keyed by cache key so (a) eviction
# can pick the oldest, (b) a comeback play for the same tune kills its own
# now-redundant duplicate instead of rendering twice.
# value: {"proc": Process, "release": slot-release fn, "t": monotonic start}
_SID_DETACHED: dict[str, dict] = {}

# Every in-flight progressive render tagged with a monotonic admission id, so a
# render can tell whether a NEWER render for the same key has superseded it — a
# browser seek aborts the old request and issues a new range GET near-
# simultaneously, and if the new admission runs before the old generator's
# disconnect handler, the old one would detach into a duplicate.  The old render
# instead sees it's no longer the latest for its key and bows out (terminates)
# rather than detaching.
_SID_PROG_GEN = [0]                        # monotonic admission counter
_SID_PROG_INFLIGHT: dict[str, int] = {}    # full_key → latest admission id


def _sid_render_cap() -> int:
    """The live+detached render-pool size (``sid_render_parallel``, min 1)."""
    try:
        return max(1, int(getattr(settings, "sid_render_parallel", 3)))
    except (TypeError, ValueError):
        return 3


def _kill_detached(entry: dict) -> None:
    """Kill one detached render and free its pool slot NOW.  Its finaliser
    still runs (reaps the proc, unlinks the temp — rc != 0 fails the cache
    gate) and its own slot release is an idempotent no-op."""
    try:
        if entry["proc"].returncode is None:
            entry["proc"].kill()
    except ProcessLookupError:
        pass
    entry["release"]()


def _evict_oldest_detached() -> bool:
    """Free a pool slot for a LIVE play by sacrificing the oldest detached
    render (a background cache-warm loses to a person listening, always).
    False when there is nothing detached to evict — every slot is live."""
    if not _SID_DETACHED:
        return False
    key = min(_SID_DETACHED, key=lambda k: _SID_DETACHED[k]["t"])
    _kill_detached(_SID_DETACHED.pop(key))
    return True


# The BLOCKING SID render path (Subsonic / DLNA / cast, and web plays that spill
# past the progressive pool) is used by callers that can't stream a
# still-rendering WAV, so it awaits the whole render.  It was globally
# UNBOUNDED: N distinct cold plays span N concurrent sidplayfp (get_or_render
# only dedups the SAME key).  Gate it behind a semaphore sized to
# ``sid_render_parallel`` so it can't spawn an unbounded fleet.  Separate from
# the progressive pool's live-first counter (that one needs non-blocking
# try/evict semantics), so the two SID render paths are each capped at the
# setting — worst case 2×sid_render_parallel audio renders + the VU pool.  Sized
# once on first use (a cap change takes effect on restart, like most settings).
_SID_BLOCKING_SEM: "asyncio.Semaphore | None" = None


def _sid_blocking_sem() -> "asyncio.Semaphore":
    global _SID_BLOCKING_SEM
    if _SID_BLOCKING_SEM is None:
        _SID_BLOCKING_SEM = asyncio.Semaphore(_sid_render_cap())
    return _SID_BLOCKING_SEM


# How long to wait for sidplayfp's first PCM bytes before either streaming
# (bytes appeared) or falling back to the blocking render (proc died first —
# so an immediate render failure surfaces as a real error, not silent silence).
_SID_PROG_FIRST_BYTE_TIMEOUT = 3.0


def _synth_wav_header(rate: int, channels: int, bits: int, data_bytes: int) -> bytes:
    """A canonical 44-byte PCM WAV header declaring ``data_bytes`` of audio."""
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    u16 = lambda v: int(v).to_bytes(2, "little")
    u32 = lambda v: int(v).to_bytes(4, "little")
    return (b"RIFF" + u32(36 + data_bytes) + b"WAVE"
            + b"fmt " + u32(16) + u16(1) + u16(channels) + u32(rate)
            + u32(byte_rate) + u16(block_align) + u16(bits)
            + b"data" + u32(data_bytes))


def _sid_render_cmd(binary: str, path: Path, subsong: int, dur: int, out_wav: str,
                    mute: "tuple[int, ...]" = ()) -> list[str]:
    """Build the sidplayfp argv shared by the blocking and progressive renders,
    so the two paths never drift on chip-model / filter / digiboost flags.

    ``mute`` is a tuple of 1-indexed voices to silence via ``-u<n>`` — used by
    the per-voice VU pass (sid_vu) to isolate one voice per render.

    SID chip-model / filter overrides (settings; defaults = no flags, i.e.
    sidplayfp honours the tune's own PSID header).  Flags verified against
    sidplayfp --help: -m<o|n>[f], -nf, --fcurve=<num>, --digiboost, -u<n> mute
    voice; no space between a short flag and its value."""
    cmd = [binary]
    if subsong > 0:
        cmd.append(f"-o{subsong}")
    for _v in mute:
        cmd.append(f"-u{int(_v)}")
    _model = (settings.sid_model or "auto").lower()
    if _model in ("6581", "8580"):
        _mflag = "-mo" if _model == "6581" else "-mn"
        if settings.sid_model_force:
            _mflag += "f"
        cmd.append(_mflag)
    if not settings.sid_filter:
        cmd.append("-nf")
    _curve = float(getattr(settings, "sid_filter_curve", -1.0))
    if 0.0 <= _curve <= 1.0:
        cmd.append(f"--fcurve={_curve:g}")
    if settings.sid_digiboost:
        cmd.append("--digiboost")
    cmd.extend([f"-t{int(dur)}", f"-w{out_wav}", str(path)])
    return cmd


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
    # Bound concurrent blocking renders (see _sid_blocking_sem).  Create the
    # temp INSIDE the semaphore so a CancelledError while WAITING to acquire
    # (all slots busy, client hangs up) can't leak a 0-byte temp — nothing is
    # created until we hold a permit.
    async with _sid_blocking_sem():
        tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_wav.close()
        cmd = _sid_render_cmd(binary, path, subsong, dur, tmp_wav.name)
        await _await_renderer(cmd, Path(tmp_wav.name), timeout=dur + 30, kind="SID")
    return Path(tmp_wav.name)


# ── SID per-voice VU (retro 3-voice meter, DeepSID-style) ────────────────────
# The meter is built from 3 extra "isolation" renders (one per SID voice, the
# other two muted).  That's ~3× a SID render, so it runs ONCE per tune in the
# background, best-effort, capped + semaphore-bounded, and the result is cached
# forever as a ``.vu`` sidecar next to the audio WAV — the same VUMR format the
# tracker/uade meters use, so the frontend needs no changes.
_SID_VU_MAX_DURATION = 600          # don't spend 3× on very long tunes
_SID_VU_INFLIGHT: set = set()       # full_keys queued or generating (deduped)
# One tune's VU gen at a time — each now runs its 3 isolation passes CONCURRENTLY
# (3 cores), so a semaphore of 1 already keeps ~3 sidplayfp busy without starving
# playback; more tunes queue behind it (bounded by _SID_VU_MAX_QUEUED).
_SID_VU_SEM = asyncio.Semaphore(1)
_SID_VU_MAX_QUEUED = 8              # bound the TOTAL backlog — a burst of many
                                    # distinct SIDs can't pile up unbounded VU
                                    # work; skipped tunes generate on a later play
# Client-VU offload: a browser plays a SID, renders its per-voice VU with the
# libsidplayfp+reSIDfp WASM core, and POSTs it to /api/tracks/{id}/vu.  The VU
# sidecar is consumed ONLY by the browser meter, so we hold the server's own
# 3-pass render for this grace window to let a capable client upload first
# (true offload).  If no upload lands (cast/Subsonic play, or a WASM-incapable
# browser), the server renders as the fallback.  The client upload and this
# render both short-circuit on the ".vu exists" check, so whoever finishes
# first wins and the other skips.  0 disables the delay (always render at once).
#
# 8s, not 30s: the grace window is a BET that a browser uploads first, and only
# Blink can win it.  Measured on the same 92s tune: Chromium finishes its WASM
# render in 8.2s (~13x realtime) and uploads; Firefox manages 900 of 2760 frames
# in 22s (~1.4x realtime) and would need ~67s — so on Gecko the server sat idle
# for 30s waiting for an upload that never came, then took ~23s more to render,
# leaving the listener on the FFT fallback for ~84s (measured: WAV cached 21:16,
# sidecar written 21:17:24).  8s still lets a fast client win the race while
# bounding the worst case to roughly the render itself.
#
# Known, accepted race: on tunes ≥ ~90 s even Blink needs longer than 8 s, so
# the server may start (and complete) a redundant 3-pass render alongside the
# client's upload.  Deliberate: a short flat grace optimises for the clients
# that can't upload at all (Gecko), where every extra grace-second is an extra
# second of FFT fallback; the loser of the race only wastes CPU, never
# correctness (the worker skips the write when a sidecar landed mid-render).
_SID_VU_SERVER_DELAY = 8.0


async def _sid_vu_worker(full_key: str, cached_wav: Path, sid_path: Path,
                         subsong: int, dur: int) -> None:
    """Render the 3 voice-isolation passes and write the per-voice VUMR sidecar
    next to ``cached_wav``.  Best-effort — any failure leaves the FFT fallback."""
    from soniqboom.core import sid_vu, openmpt_vu
    binary = _find_renderer(settings.sidplayfp_path, "sidplayfp")
    tmps: list[Path] = []
    try:
        if not binary:
            return
        async with _SID_VU_SEM:
            # Re-check under the semaphore — another play may have finished it
            # while we queued.
            if cached_wav.with_suffix(".vu").exists():
                return
            # Run the 3 voice-isolation renders CONCURRENTLY (not sequentially)
            # so total gen time is ~one render, not three — a 6-min tune drops
            # from ~68 s to ~23 s.  Each sidplayfp is single-threaded (one core),
            # so 3 in parallel just uses 3 cores; the semaphore bounds how many
            # tunes generate at once.
            voice_wavs: list[Path] = []
            render_coros = []
            for mute in sid_vu.VOICE_MUTES:
                tf = tempfile.NamedTemporaryFile(suffix=".wav", prefix="sidvu-", delete=False)
                tf.close()
                tp = Path(tf.name)
                tmps.append(tp)
                cmd = _sid_render_cmd(binary, sid_path, subsong, dur, tf.name, mute=mute)
                render_coros.append(_await_renderer(cmd, tp, timeout=dur + 30, kind="SID-VU"))
                voice_wavs.append(tp)
            await asyncio.gather(*render_coros)
            result = await asyncio.to_thread(sid_vu.build_vu, voice_wavs, float(dur))
            if result is not None:
                vu_path = cached_wav.with_suffix(".vu")
                # A client upload may have landed while our 3 passes rendered
                # (on tunes ≥ ~90 s a fast Blink client finishes AFTER the 8 s
                # grace, so both sides race deliberately — see
                # _SID_VU_SERVER_DELAY).  Client and server sidecars are
                # equivalent (0.97-1.0 measured correlation), so keep theirs
                # rather than overwrite.
                if vu_path.exists():
                    return
                # The shard dir may not exist: the sidecar can now be written
                # for a SID whose audio was never cached (see
                # ``ensure_sid_vu_sidecar``), so nothing else has created it.
                vu_path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(openmpt_vu.write_sidecar, vu_path, result)
                log.info("SID VU: wrote %d-voice sidecar for %s",
                         result.channels, cached_wav.name)
    except Exception:
        log.debug("SID VU pass failed for %s", sid_path, exc_info=True)
    finally:
        for tp in tmps:
            try:
                tp.unlink(missing_ok=True)
            except OSError:
                pass
        _SID_VU_INFLIGHT.discard(full_key)


def _spawn_sid_vu(full_key: str, cached_wav: Path, sid_path: Path,
                  subsong: int, dur: int) -> None:
    """Dedup + cap + fire-and-forget the 3-pass VU generation, writing the
    sidecar beside ``cached_wav``.

    ``cached_wav`` is only an ANCHOR for the sidecar's name — the render reads
    the source ``sid_path``, so the WAV need not exist (``ensure_sid_vu_sidecar``
    passes the cache key's would-be path for a SID whose audio isn't cached).
    No-op if too long, already present, or in flight."""
    if dur <= 0 or dur > _SID_VU_MAX_DURATION:
        return
    if full_key in _SID_VU_INFLIGHT:
        return
    if len(_SID_VU_INFLIGHT) >= _SID_VU_MAX_QUEUED:
        return                                  # backlog full — retry on a later play
    if cached_wav.with_suffix(".vu").exists():
        return
    _SID_VU_INFLIGHT.add(full_key)
    task = asyncio.create_task(
        _sid_vu_worker_delayed(full_key, cached_wav, sid_path, subsong, dur),
        name=f"sid_vu[{full_key}]",
    )
    _SID_PROG_FINALISERS.add(task)              # strong ref; reuse the ref set
    task.add_done_callback(_SID_PROG_FINALISERS.discard)


async def _sid_vu_worker_delayed(full_key: str, cached_wav: Path, sid_path: Path,
                                 subsong: int, dur: int) -> None:
    """Hold ``_SID_VU_SERVER_DELAY`` seconds so a browser client can upload its
    WASM-rendered sidecar first (offload), then generate server-side only if the
    ``.vu`` still doesn't exist.  Owns the inflight-slot release for the skip
    path; ``_sid_vu_worker`` releases it for the render path (both idempotent)."""
    try:
        if _SID_VU_SERVER_DELAY > 0:
            await asyncio.sleep(_SID_VU_SERVER_DELAY)
        if cached_wav.with_suffix(".vu").exists():
            return                              # client (or a prior play) won — skip the render
        await _sid_vu_worker(full_key, cached_wav, sid_path, subsong, dur)
    finally:
        _SID_VU_INFLIGHT.discard(full_key)      # idempotent; covers the skip/sleep-cancel paths


def ensure_sid_vu_sidecar(track_id: str, sid_path: Path, subsong: int, dur: int) -> None:
    """Fire-and-forget: ensure a per-voice VU sidecar exists for a SID.

    The VU pass renders the 3 voice-isolation passes from the SOURCE ``.sid``;
    the cached WAV was only ever used to derive the sidecar's NAME.  So resolve
    that name from the cache key directly and spawn regardless of whether the
    audio is cached.

    This used to bail out ("audio not cached yet — skip") on exactly the COLD
    play that needs the meter most, deferring it to a later play.  Combined with
    the progressive finaliser discarding the render whenever a listener skips
    away mid-tune (so no WAV is ever committed, and the "later play" is another
    cold play), that made the sidecar unreachable indefinitely — a permanent FFT
    fallback.  Writing it from the source breaks that loop: the meter is ready
    for the rest of THIS play and instant on every later one, cached or not.

    Safe to call from every SID play path — dedups on the cache key and
    short-circuits once the sidecar exists.
    """
    if dur <= 0 or dur > _SID_VU_MAX_DURATION:
        return
    from soniqboom.core.conversion_cache import _cache_key, _cache_path
    full_key = _cache_key(track_id, "sid", subsong, duration=dur)
    # The would-be WAV path: _spawn_sid_vu only reads ``.with_suffix(".vu")``
    # off it, so it need not exist.
    _spawn_sid_vu(full_key, _cache_path(full_key, "sid"), sid_path, subsong, dur)


async def _await_first_sid_output(proc, tmp: Path, timeout: float) -> bool:
    """Wait until sidplayfp has written its first PCM bytes (→ True) or exited
    without producing any (→ False).  Lets the caller detect an immediate render
    failure (bad tune, missing ROM) and fall back to the blocking path — which
    surfaces a real error — instead of streaming a silent full-length 200.

    On timeout with the process still alive we return True and stream anyway:
    the generator's own idle-timeout handles a genuinely stuck render."""
    waited = 0.0
    while waited < timeout:
        try:
            if os.path.getsize(tmp) > _WAV_HEADER_LEN:
                return True
        except OSError:
            pass
        if proc.returncode is not None:
            # Exited already — success only if it left real PCM on disk.
            try:
                return os.path.getsize(tmp) > _WAV_HEADER_LEN
            except OSError:
                return False
        await asyncio.sleep(_GROWING_POLL_INTERVAL)
        waited += _GROWING_POLL_INTERVAL
    # Timed out.  Fail over only if the proc already died producing nothing;
    # otherwise let the stream proceed.
    return proc.returncode is None


def _parse_audio_range(range_header: "str | None", total: int):
    """Parse a single HTTP ``Range: bytes=…`` header against a known ``total``
    size.  Returns ``(start, end_exclusive, is_range)``:
      • no/whitespace/multi-range or malformed header → ``(0, total, False)``
        (treat as a full-content request; 200).
      • a valid single range → ``(start, end_exclusive, True)`` (206).
      • an unsatisfiable range (start past the end) → ``None`` (caller 416s).
    Only the FIRST range of a multi-range request is honoured (browsers send
    single ranges for media); ``bytes=-N`` suffix ranges are supported."""
    rh = (range_header or "").strip().lower()
    if not rh.startswith("bytes="):
        return (0, total, False)
    spec = rh[len("bytes="):].split(",")[0].strip()   # first range only
    if "-" not in spec:
        return (0, total, False)
    a, _, b = spec.partition("-")
    try:
        if a == "":                                   # suffix: bytes=-N
            n = int(b)
            if n <= 0:
                return None
            start, end_excl = max(0, total - n), total
        else:
            start = int(a)
            end_excl = (int(b) + 1) if b else total
    except ValueError:
        return (0, total, False)
    end_excl = min(end_excl, total)
    if start < 0 or start >= total or start >= end_excl:
        return None
    return (start, end_excl, True)


async def _serve_sid_progressive(
    request: Request,
    sid_path: Path,
    subsong: int,
    duration: int,
    full_key: str,
    base_headers: dict[str, str],
    background,
) -> "Response | None":
    """Cold-play a SID with ~instant start: spawn sidplayfp and stream its
    STILL-RENDERING WAV instead of awaiting the whole render.

    sidplayfp renders ~15x realtime, so the first ~1 s of audio is on disk in
    ~0.2 s; we ship a synthesised full-length WAV header immediately, then relay
    sidplayfp's PCM (skipping its own 44-byte header) as the file grows.  On a
    clean, fully-streamed render the finished WAV is promoted to the conversion
    cache under ``full_key`` so the next play hits the instant Range fast-path.

    Returns ``None`` (having cleaned up) when the render fails to start — the
    caller must then use the blocking path so the error surfaces as a real 5xx
    rather than a silent full-length-silence 200.

    Answers Range/seek requests with a proper 206 + Content-Range against the
    known full length (``44 + data_bytes``), and serves header-only probes
    (Safari's ``bytes=0-1``) straight from the synthesised header with no render.
    Used for the local web-UI cold path only; cache hits and non-web clients
    (Subsonic/DLNA/Cast) stay on the existing blocking render."""
    from soniqboom.core.conversion_cache import get_cached, store_cached

    binary = _find_renderer(settings.sidplayfp_path, "sidplayfp")
    if not binary:
        raise HTTPException(501, "sidplayfp not installed")

    data_bytes = int(duration) * _SID_WAV_RATE * _SID_WAV_CHANNELS * (_SID_WAV_BITS // 8)
    header = _synth_wav_header(_SID_WAV_RATE, _SID_WAV_CHANNELS, _SID_WAV_BITS, data_bytes)
    total = _WAV_HEADER_LEN + data_bytes

    # The progressive WAV is EXACTLY ``total`` bytes even before it's rendered
    # (synthesised header + fixed-length PCM), so a Range/seek request can be
    # answered with a proper ``206`` + ``Content-Range`` against the known total.
    # This is what makes progressive SID safe in WebKit/Safari — which probes a
    # media element with ``Range: bytes=0-1`` and requires a 206 — and lets an
    # early seek stream from the requested offset instead of dropping to a full
    # blocking render.
    rng = _parse_audio_range(request.headers.get("range"), total)
    if rng is None:                                   # unsatisfiable range
        return Response(status_code=416, media_type="audio/wav",
                        headers={"Content-Range": f"bytes */{total}",
                                 "Accept-Ranges": "bytes"},
                        background=background)
    start, end_excl, is_range = rng

    # Header-only range (Safari's ``bytes=0-1`` probe, or any range fully inside
    # the 44-byte header) — serve straight from the synthesised header with NO
    # render and NO concurrency slot.  Satisfies the probe so WebKit learns the
    # length + range support, then issues the real playback request below.
    if end_excl <= _WAV_HEADER_LEN:
        body = header[start:end_excl]
        hdrs = dict(base_headers or {})
        hdrs["Accept-Ranges"] = "bytes"
        hdrs["X-Stream-Mode"] = "sid-progressive-head"
        hdrs["Content-Range"] = f"bytes {start}-{end_excl - 1}/{total}"
        hdrs["Content-Length"] = str(len(body))
        return Response(content=body, status_code=206, media_type="audio/wav",
                        headers=hdrs, background=background)

    # Data range → we must render.  Concurrency cap: reserve a slot in the SAME
    # synchronous block as the check so a simultaneous burst can't all pass
    # "< cap" before any of them increments (asyncio runs this prefix to the
    # first ``await`` uninterrupted, so the count is authoritative here).
    #
    # A COMEBACK play first retires its own detached duplicate: the fresh live
    # render supersedes it (two sidplayfp processes rendering the same tune is
    # pure waste), and killing it frees a slot before the cap check.
    dup = _SID_DETACHED.pop(full_key, None)
    if dup is not None:
        _kill_detached(dup)
    # Live-first admission: a full pool evicts the oldest DETACHED render (a
    # background cache-warm) to make room; only when every slot is a live
    # stream does the cold play fall back to the blocking render (None).
    if _SID_PROG_ACTIVE[0] >= _sid_render_cap() and not _evict_oldest_detached():
        return None
    _SID_PROG_ACTIVE[0] += 1
    _SID_PROG_GEN[0] += 1
    _my_gen = _SID_PROG_GEN[0]
    _SID_PROG_INFLIGHT[full_key] = _my_gen     # I am now the latest render for this key
    _slot_released = {"v": False}

    def _release_slot() -> None:
        if not _slot_released["v"]:
            _slot_released["v"] = True
            _SID_PROG_ACTIVE[0] -= 1

    def _drop_inflight() -> None:
        # Identity-guarded so we never remove a newer render's entry.
        if _SID_PROG_INFLIGHT.get(full_key) == _my_gen:
            _SID_PROG_INFLIGHT.pop(full_key, None)

    tmp: "Path | None" = None
    proc = None
    try:
        # Create the temp + spawn together; if the spawn itself raises (FD
        # exhaustion, binary vanished mid-flight) unlink the orphaned temp — the
        # finaliser that normally owns cleanup isn't created until after this.
        tmp = Path(tempfile.mkstemp(suffix=".wav", prefix="sidprog-")[1])
        cmd = _sid_render_cmd(binary, sid_path, subsong, int(duration), str(tmp))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )

        # Detect an immediate render failure before committing to the streaming
        # response.  If sidplayfp dies without writing PCM, reap it, drop the
        # temp, and signal the caller to fall back to the blocking render.
        if not await _await_first_sid_output(proc, tmp, _SID_PROG_FIRST_BYTE_TIMEOUT):
            try:
                if proc.returncode is None:
                    proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            tmp.unlink(missing_ok=True)
            log.info("progressive SID: no first-byte output (rc=%s) for %s — "
                     "falling back to blocking render", proc.returncode, full_key)
            _drop_inflight()          # finaliser (the usual popper) is never created here
            _release_slot()
            return None
    except BaseException:
        # Any failure before the finaliser exists (including CancelledError on
        # server shutdown mid-probe) must clean up itself — the finaliser that
        # normally owns the proc + temp isn't created until below.  ``kill`` is
        # synchronous so it runs even while the task is being cancelled; the
        # asyncio child watcher reaps the killed proc.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        _drop_inflight()              # finaliser never created on this path
        _release_slot()
        raise

    # Shared flags: the response generator flips ``streamed_all`` once it has
    # streamed the whole declared length, marks ``stalled`` when the render hung
    # (padded to length, not cacheable, not worth finishing), and parks the
    # registry entry in ``detached`` when a disconnect left the render finishing
    # in the background.  Read by the DETACHED finaliser below.  ``gen_done`` is
    # set by the generator's finally on EITHER completion or disconnect, so the
    # finaliser waits for the generator to reach its final state before reading
    # these (else a fast render whose proc exits before a slow client finishes
    # would be judged incomplete and its good WAV discarded).
    state = {"streamed_all": False, "stalled": False, "detached": None}
    gen_done = asyncio.Event()

    async def _finalise() -> None:
        # Owns the render's lifecycle INDEPENDENTLY of the response task, so a
        # client disconnect (which cancels the generator) can't abort the
        # reap/cache: awaiting ``proc.wait()`` inside the generator's finally
        # was being CancelledError'd on disconnect, leaking a zombie sidplayfp.
        try:
            # Wait for the generator's final state.  A response the SERVER NEVER
            # ITERATES (client vanished before the body streamed) never sets
            # gen_done, so give the generator a short window to START; if it
            # hasn't, don't pin a live pool slot for the whole render — with the
            # pool at 3, three such stuck slots would disable the progressive
            # path for everyone for ~duration s.  A generator that HAS started is
            # a genuinely slow client; wait the rest of the budget for it.
            try:
                await asyncio.wait_for(gen_done.wait(), timeout=15)
            except asyncio.TimeoutError:
                if state.get("started"):
                    try:
                        await asyncio.wait_for(gen_done.wait(),
                                               timeout=int(duration) + 45)
                    except asyncio.TimeoutError:
                        pass
                # else: never iterated → treat as abandoned, reap below.
            # If the client didn't consume the whole stream the render is
            # normally DETACHED to finish for the cache (see the generator's
            # finally).  Kill it only when nothing detached it: a stalled
            # render, an older same-key duplicate, or a response the server
            # never iterated (client vanished before the body streamed).
            detached = state.get("detached") is not None
            if not state["streamed_all"] and not detached and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            # A detached render legitimately keeps running for its remaining
            # render time (~duration/15 plus margin); a live-completed or
            # killed one reaps in seconds.
            _reap_s = int(duration) + 60 if detached else 15
            try:
                await asyncio.wait_for(proc.wait(), timeout=_reap_s)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            # Cache admission is RENDER integrity, nothing about the client:
            # exit 0 plus an essentially full-length temp (1 s of slack absorbs
            # sidplayfp's sub-second rounding; it normally renders slightly
            # PAST ``-t``).  What the CLIENT consumed is deliberately not a
            # condition: sidplayfp runs ~15x realtime, so most real skips
            # happen AFTER the render already finished — the first gate here
            # (``streamed_all or detached``) threw a COMPLETE rc-0 render away
            # in exactly that window (QA-reproduced live: a finished 26 MB
            # temp unlinked because the listener left at 6% consumed).  Every
            # kill path stays rejected by rc alone: evicted → -9, stalled and
            # terminated → -15, reap-timeout kill → nonzero.
            try:
                _tmp_size = os.path.getsize(tmp) if tmp.exists() else 0
            except OSError:
                _tmp_size = 0
            _one_sec = _SID_WAV_RATE * _SID_WAV_CHANNELS * (_SID_WAV_BITS // 8)
            _min_file = _WAV_HEADER_LEN + max(0, data_bytes - _one_sec)
            good = (proc.returncode == 0 and _tmp_size >= _min_file)
            if good:
                # Re-check: a concurrent render (another cold play, or a
                # blocking Subsonic play) may have cached this key first.  If
                # so, just drop our temp — store_cached is idempotent now, but
                # skipping avoids a redundant move + LRU touch.
                existing = await get_cached(full_key)
                if existing is not None:
                    tmp.unlink(missing_ok=True)
                    _spawn_sid_vu(full_key, existing, sid_path, subsong, int(duration))
                else:
                    dest = await store_cached(full_key, "sid", tmp)   # MOVES tmp into cache
                    log.info("progressive SID: cached %s (%d s)%s", full_key,
                             int(duration),
                             " — finished after the listener left" if detached else "")
                    # Kick off the retro per-voice VU meter in the background —
                    # ready for the next play; this one used the FFT fallback.
                    _spawn_sid_vu(full_key, dest, sid_path, subsong, int(duration))
            else:
                tmp.unlink(missing_ok=True)
        except asyncio.CancelledError:
            # Server shutdown cancels finalisers — don't orphan a (possibly
            # detached, minutes-long) sidplayfp past this process's lifetime.
            try:
                if proc.returncode is None:
                    proc.kill()
            except ProcessLookupError:
                pass
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except Exception:
            log.warning("progressive SID: finalise failed for %s", full_key, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        finally:
            # Drop OUR registry entries (a comeback/eviction/newer render may
            # already have replaced them — never pop someone else's).
            _ent = state.get("detached")
            if _ent is not None and _SID_DETACHED.get(full_key) is _ent:
                _SID_DETACHED.pop(full_key, None)
            _drop_inflight()
            _release_slot()          # idempotent — releases the concurrency slot
            _SID_PROG_FINALISERS.discard(fin_task)

    fin_task = asyncio.create_task(_finalise())
    _SID_PROG_FINALISERS.add(fin_task)          # strong ref so it isn't GC'd

    # Map the requested byte range [start, end_excl) onto the virtual file:
    # header region [h_lo, h_hi) served from the synthesised header; data region
    # [d_lo, d_hi) read from the growing temp at (pos - 44).  ``streamed_all``
    # (the range reached the file's end) only matters for the generator's
    # kill/detach decision — cache admission is decided by RENDER integrity in
    # the finaliser (rc 0 + full-length temp), so a mid-file range's or an
    # abandoned stream's completed render is promoted all the same.
    h_lo, h_hi = start, min(end_excl, _WAV_HEADER_LEN)
    d_lo, d_hi = max(start, _WAV_HEADER_LEN) - _WAV_HEADER_LEN, end_excl - _WAV_HEADER_LEN

    async def _gen():
        state["started"] = True     # the server is iterating us → not an abandoned response
        fd = None
        checked_header = False
        try:
            if h_lo < h_hi:
                yield header[h_lo:h_hi]
            pos = d_lo
            idle = 0.0
            while pos < d_hi:
                try:
                    cur = os.path.getsize(tmp)
                except OSError:
                    cur = 0
                if fd is None and cur > _WAV_HEADER_LEN:
                    fd = await asyncio.to_thread(os.open, str(tmp), os.O_RDONLY)
                if fd is not None:
                    if not checked_header:
                        # One-time defensive check: confirm sidplayfp really
                        # wrote a RIFF/WAVE header of the length we skip.  If a
                        # future binary emits a different container the offset-44
                        # skip would desync — log it rather than ship garbage.
                        magic = await asyncio.to_thread(os.pread, fd, 4, 0)
                        if magic != b"RIFF":
                            log.warning("progressive SID: temp header %r not RIFF "
                                        "for %s — streaming anyway", magic, full_key)
                        checked_header = True
                    want = min(_RANGE_STREAMING_CHUNK, d_hi - pos)
                    buf = await asyncio.to_thread(os.pread, fd, want, _WAV_HEADER_LEN + pos)
                    if buf:
                        yield buf
                        pos += len(buf)
                        idle = 0.0
                        continue
                # No new bytes right now.
                if proc.returncode is not None:
                    # Render finished: pad any rounding shortfall so the declared
                    # Content-Length is satisfied exactly.
                    pad = d_hi - pos
                    while pad > 0:
                        n = min(_RANGE_STREAMING_CHUNK, pad)
                        yield b"\x00" * n
                        pad -= n
                    state["streamed_all"] = (d_hi >= data_bytes)
                    return
                await asyncio.sleep(_GROWING_POLL_INTERVAL)
                idle += _GROWING_POLL_INTERVAL
                if idle >= _GROWING_READ_TIMEOUT:
                    # Render stalled.  Pad the remainder with silence so the
                    # client still gets a valid, declared-length WAV rather than
                    # a short read the <audio> element rejects — but leave
                    # ``streamed_all`` False so this stalled render is NOT cached.
                    log.warning("progressive SID: no new bytes in %.0fs for %s — "
                                "padding to declared length at %d/%d",
                                idle, full_key, pos, d_hi)
                    state["stalled"] = True
                    pad = d_hi - pos
                    while pad > 0:
                        n = min(_RANGE_STREAMING_CHUNK, pad)
                        yield b"\x00" * n
                        pad -= n
                    return
            state["streamed_all"] = (d_hi >= data_bytes)   # delivered the full range
        finally:
            # Sync-only cleanup (survives task cancellation): close our fd,
            # then decide the render's fate.  The detached ``_finalise`` reaps
            # it and decides caching — never blocked here.  Order matters:
            # close the fd BEFORE signalling ``gen_done`` so the finaliser
            # never moves/unlinks ``tmp`` while we still hold it open.
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            # NB: when the render ALREADY exited (returncode set) there is
            # nothing to kill or detach — the finaliser's integrity gate
            # (rc 0 + full-length) caches a finished temp regardless of how
            # much the client consumed.
            if not state["streamed_all"] and proc.returncode is None:
                # The listener skipped away mid-tune.  DETACH the healthy
                # render — let it finish and be cached — instead of killing it:
                # discarding an already-mostly-paid-for render meant a tune you
                # never played to the end could NEVER become warm (each replay
                # re-entered the same discard loop).  It keeps holding its pool
                # slot until its finaliser runs, so total sidplayfp processes
                # stay bounded by _sid_render_cap(); a live play can reclaim
                # the slot at admission (oldest-detached eviction).  Exceptions,
                # all "this render is redundant, don't detach a duplicate": a
                # STALLED render isn't worth finishing; an older detached render
                # of this key is already finishing; or a NEWER render for this
                # key was admitted (a seek superseded us).
                superseded = _SID_PROG_INFLIGHT.get(full_key) != _my_gen
                if state["stalled"] or full_key in _SID_DETACHED or superseded:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                else:
                    entry = {"proc": proc, "release": _release_slot,
                             "t": time.monotonic()}
                    _SID_DETACHED[full_key] = entry
                    state["detached"] = entry
                    log.info("progressive SID: listener left — finishing %s in "
                             "the background for the cache", full_key)
            gen_done.set()          # release the finaliser (both paths)

    extra = dict(base_headers or {})
    extra["Accept-Ranges"] = "bytes"
    extra["X-Stream-Mode"] = "sid-progressive"
    # We know the exact byte count we will deliver (the range is padded to fill
    # it), so always send a real Content-Length — WebKit/Safari media loading
    # prefers a known length over an open-ended chunked stream.  A Range request
    # gets 206 + Content-Range; a bare GET gets 200 with the full length.
    extra["Content-Length"] = str(end_excl - start)
    if is_range:
        extra["Content-Range"] = f"bytes {start}-{end_excl - 1}/{total}"
    return StreamingResponse(
        _gen(), status_code=206 if is_range else 200, media_type="audio/wav",
        headers=extra, background=background,
    )


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
    ".hsc", ".rix", ".a2m", ".adl", ".bam", ".ksm", ".amd",
}


def _render_ident(path_str: str) -> tuple[str, bool]:
    """Return ``(effective_ext, is_uade_named)`` for render routing.

    Keeps the renderer's uade-vs-AdLib decision in lockstep with what the
    scanner (``metadata.extract``) indexed:

      * A known AdLib extension is AdLib, never uade — even when the file
        NAME collides with a uade token.  AMUSIC ``star.amd`` files (Modland
        ``Ad Lib/…``) collide with uade's ProWizard ``star`` prefix; uade's
        ``-g`` rejects them ("module check failed") while AdPlug plays them.
      * Archive members reach us with a routing suffix appended
        (``STAR.AMD.star``); strip it when the stem is an AdLib file so the
        real ``.amd`` extension drives routing (mirrors
        ``scanner._extract_from_zip``).
    """
    from soniqboom.core.metadata import _UADE_SUFFIX_EXTS
    member = Path(path_str.split("::")[-1]).name
    ext = Path(member).suffix.lower()
    if "." in member:
        stem, _, last = member.rpartition(".")
        if (f".{last.lower()}" in _UADE_SUFFIX_EXTS
                and Path(stem).suffix.lower() in _ADLIB_EXTS):
            ext, member = Path(stem).suffix.lower(), stem
    if ext in _ADLIB_EXTS or ext == ".imf":
        return ext, False
    return ext, _uade_formats.classify(member) is not None
_ADLIB_DEFAULT_TIMEOUT_S = 8 * 60
# AdPlug OPL emulator core.  adplay defaults to "woody" (DOSBox WoodyOPL — fast
# but approximate); we pin "nuked" (Nuked OPL3, reverse-engineered from the
# YMF262 die) so the render is cycle-accurate to real OPL3 hardware.  ~3x slower
# than woody, but renders are cached so it's a one-time per-tune cost.  adplay's
# other cores: satoh, ken, woody, nuked.  See internal/OPL-ENHANCEMENT-OPTIONS.md.
_ADLIB_OPL_EMULATOR = "nuked"
# A rendered subsong shorter than this is treated as empty (an empty Westwood
# .adl subsong renders as ~0.01 s).  Kept LOW so a legitimately short tune / SFX
# still passes; the amplitude test below is what catches long-but-silent subsongs.
_ADLIB_MIN_AUDIO_S = 0.1
# Peak 16-bit sample at/below this (~ -60 dBFS) ⇒ the subsong is effectively
# silent.  Duration alone isn't enough: Westwood .adl files have subsongs that
# render LONG but silent (Dune II song 1 is 2.3 s at -91 dB) while the real
# theme is a later subsong — we must check actual amplitude.
_ADLIB_SILENCE_PEAK = 32
# Multi-song AdLib files can put the music well past subsong 0 (Dune II's theme
# is subsong 6); probe up to this many for the first that is AUDIBLE.
_ADLIB_MAX_SUBSONG_PROBE = 16
# When a loose AdLib tune's own directory has no companion bank, walk up this
# many parent dirs looking for one.  Collections keep a single standard.bnk at a
# root with the ROLs in subfolders (…/Visual Composer/standard.bnk with tunes
# under …/Visual Composer/OPLx/LARIX/).  Without the bank AdPlug can't even
# DETECT a ROL — it reports "unknown filetype", not a missing-bank error.
_ADLIB_BANK_PARENT_LEVELS = 4


async def _render_adlib_one(binary: str, path: Path, subsong: int) -> Path:
    """Render a single AdLib/OPL subsong to a fresh temp WAV (no audio check)."""
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    cmd = [binary, "-O", "disk", "-d", tmp_wav.name,
           "-e", _ADLIB_OPL_EMULATOR, "-f", "44100", "--stereo"]
    if subsong > 0:
        cmd += ["-s", str(subsong)]      # multi-song AdLib formats (RAD, .adl, …)
    cmd.append(str(path))
    await _await_renderer(
        cmd, Path(tmp_wav.name),
        timeout=_ADLIB_DEFAULT_TIMEOUT_S, kind="adlib",
    )
    return Path(tmp_wav.name)


def _wav_audio_seconds(wav_path: Path) -> float:
    """Real audio length of a WAV from its header (cheap); 0.0 if unreadable.

    Parses the RIFF chunks by hand rather than via the stdlib ``wave`` module:
    uade123 writes WAVE_FORMAT_EXTENSIBLE (format tag 0xFFFE / 65534), which
    ``wave.open`` rejects with "unknown format" — that would silently break the
    AHX/HVL duration backfill.  Duration = data-chunk bytes / average-bytes-per-
    second, which is format-tag-agnostic and works for plain PCM (adplay /
    sidplayfp / openmpt123) too.
    """
    import struct
    try:
        with open(wav_path, "rb") as f:
            riff = f.read(12)
            if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
                return 0.0
            avg_bps = rate = channels = bits = data_bytes = 0
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid, size = hdr[:4], struct.unpack("<I", hdr[4:8])[0]
                if cid == b"fmt ":
                    fmt = f.read(size)
                    if len(fmt) >= 16:
                        channels = struct.unpack("<H", fmt[2:4])[0]
                        rate = struct.unpack("<I", fmt[4:8])[0]
                        avg_bps = struct.unpack("<I", fmt[8:12])[0]
                        bits = struct.unpack("<H", fmt[14:16])[0]
                    if size % 2:
                        f.seek(1, 1)             # chunks are word-aligned
                elif cid == b"data":
                    pos = f.tell()
                    f.seek(0, 2)
                    avail = f.tell() - pos        # bytes actually on disk
                    data_bytes = size if 0 < size <= avail else avail
                    break
                else:
                    f.seek(size + (size & 1), 1)
            if data_bytes <= 0:
                return 0.0
            if avg_bps <= 0:                       # derive it if the writer left it 0
                if rate and channels and bits:
                    avg_bps = rate * channels * (bits // 8)
                else:
                    return 0.0
            return data_bytes / float(avg_bps)
    except Exception:
        return 0.0


def _wav_peak_amplitude(wav_path: Path) -> int:
    """Peak |sample| sampled across several ~1 s windows of a 16-bit WAV.

    Sampling 5 windows (not just start+middle) avoids a false "silent" verdict on
    a tune with a quiet intro AND a quiet midpoint but audio elsewhere.  Stops as
    soon as audibility is proven.  Returns 32767 (treat as audible) if the file
    isn't readable as 16-bit PCM — a probe heuristic must never suppress a tune we
    could otherwise play.
    """
    import wave, array, sys
    try:
        with wave.open(str(wav_path), "rb") as w:
            if w.getsampwidth() != 2:
                return 32767
            rate = w.getframerate() or 44100
            total = w.getnframes()
            if total <= 0:
                return 0
            win = min(total, rate)                 # ~1 s per window
            peak = 0
            for frac in (0.0, 0.2, 0.4, 0.6, 0.8):
                pos = min(max(0, int(total * frac)), max(0, total - win))
                w.setpos(pos)
                raw = w.readframes(win)
                if not raw:
                    continue
                a = array.array("h")
                a.frombytes(raw[: (len(raw) // 2) * 2])
                if sys.byteorder == "big":         # WAV PCM is little-endian; array uses host order
                    a.byteswap()
                if len(a):
                    peak = max(peak, max(a), -min(a))
                    if peak > _ADLIB_SILENCE_PEAK:  # proven audible — no need to scan further
                        break
            return peak
    except Exception:
        return 32767


def _wav_is_audible(wav_path: Path) -> bool:
    """True if a render has real, non-silent audio of meaningful length."""
    if _wav_audio_seconds(wav_path) < _ADLIB_MIN_AUDIO_S:
        return False
    return _wav_peak_amplitude(wav_path) > _ADLIB_SILENCE_PEAK


def _dro_is_v2(data: bytes) -> bool:
    """True if *data* is a DRO v2 capture (which adplay decodes natively)."""
    return len(data) >= 12 and data[:8] == b"DBRAWOPL" and data[8:12] == b"\x02\x00\x00\x00"


def _dro_v1_to_v2(data: bytes) -> bytes:
    """Rewrite a DRO v1 (DOSBox Raw OPL) capture as DRO v2.

    adplay/AdPlug auto-detect only recognises DRO **v2**; older v1 captures are
    rejected as "unknown filetype".  We re-encode the SAME OPL register-write
    stream into the v2 container (codemap + short/long delay codes), so this is a
    lossless transcode — the rendered audio is identical to the original capture.

    DRO v1 has two header variants: the early "no version field" layout
    (``DBRAWOPL`` + lengthMs + lengthBytes + hwType, data @17 — what melcom's
    captures use) and a later versioned layout (version + lengthMs + lengthBytes
    + hwType[+pad], data @21 or @24).  We pick whichever places
    ``data_start + lengthBytes`` exactly on EOF.
    """
    import struct
    if len(data) < 17 or data[:8] != b"DBRAWOPL":
        raise ValueError("not a DRO file")
    n = len(data)
    data_start = 17
    for lb_off, d_off in ((12, 17), (16, 21), (16, 24)):
        if lb_off + 4 <= n and d_off + struct.unpack_from("<I", data, lb_off)[0] == n:
            data_start = d_off
            break
    pos, end, bank = data_start, n, 0
    events: "list[tuple]" = []
    while pos < end:
        cmd = data[pos]; pos += 1
        if cmd == 0x00:                       # 1-byte delay
            if pos >= end: break
            events.append(("d", data[pos] + 1)); pos += 1
        elif cmd == 0x01:                     # 2-byte delay
            if pos + 1 >= end: break
            events.append(("d", struct.unpack_from("<H", data, pos)[0] + 1)); pos += 2
        elif cmd == 0x02:                     # low register bank
            bank = 0
        elif cmd == 0x03:                     # high register bank (OPL3)
            bank = 1
        elif cmd == 0x04:                     # escape: write to register 0x00-0x04
            if pos + 1 >= end: break
            events.append(("w", bank, data[pos], data[pos + 1])); pos += 2
        else:                                 # cmd is the register, next byte the value
            if pos >= end: break
            events.append(("w", bank, cmd, data[pos])); pos += 1

    regs: "list[int]" = []
    seen: "set[int]" = set()
    for e in events:
        if e[0] == "w" and e[2] not in seen:
            seen.add(e[2]); regs.append(e[2])
    if len(regs) > 126:                       # codes are 7-bit; leave room for 2 delay codes
        raise ValueError(f"too many distinct OPL registers ({len(regs)}) for a DRO v2 codemap")
    code = {r: i for i, r in enumerate(regs)}
    short_code, long_code = len(regs), len(regs) + 1

    body = bytearray(); pairs = 0; total_ms = 0
    for e in events:
        if e[0] == "d":
            d = e[1]; total_ms += d
            full = d // 256
            while full > 0:                   # long delay encodes (val+1)*256 ms
                chunk = min(256, full)
                body += bytes([long_code, chunk - 1]); pairs += 1; full -= chunk
            rem = d % 256
            if rem:                           # short delay encodes (val+1) ms
                body += bytes([short_code, rem - 1]); pairs += 1
        else:
            _, b, reg, val = e
            body += bytes([code[reg] | (0x80 if b else 0), val]); pairs += 1

    hdr = bytearray(b"DBRAWOPL")
    hdr += struct.pack("<HH", 2, 0)                       # version 2.0
    hdr += struct.pack("<I", pairs)                       # iLengthPairs
    hdr += struct.pack("<I", total_ms)                    # iLengthMS
    # hwType / format / compression / shortDelayCode / longDelayCode / codemapLen.
    # adplay derives OPL2-vs-OPL3 from the register stream, so hwType is moot.
    hdr += bytes([0, 0, 0, short_code, long_code, len(regs)])
    hdr += bytes(regs)                                    # codemap
    return bytes(hdr) + bytes(body)


async def _render_adlib(path: Path, subsong: int = 0) -> Path:
    """Render an AdLib / OPL2 FM tune to WAV via AdPlug's ``adplay`` disk writer.

    Output: 44.1 kHz / stereo / 16-bit signed LE — matches the other rendered
    formats so the cache + cast pipeline treat them uniformly.  adplay renders
    the tune once (AdPlug reports the song's end) then exits; the timeout bounds
    any endless / looping tune.

    Multi-song AdLib formats (notably Westwood ``.adl`` — Dune II, Kyrandia)
    have an EMPTY subsong 0, with the music in a later subsong.  When the caller
    doesn't pin a subsong we render subsong 0 and, if it's silent, probe the next
    few subsongs for the first that actually produces audio — otherwise the
    server would stream a ~0.01 s silent clip that "plays" but is useless.
    """
    binary = _find_renderer(settings.adplay_path, "adplay")
    if not binary:
        raise HTTPException(
            501,
            "adplay (AdPlug) not installed — AdLib/OPL formats (id IMF, ROL, "
            "CMF, D00, RAD, …) require it.  Install via 'brew install adplay' "
            "(macOS) or 'apt install adplug-utils' (Debian/Ubuntu).",
        )

    # DOSBox Raw OPL: adplay's auto-detect only handles DRO v2; the older DRO v1
    # (no version field) it rejects as "unknown filetype".  Losslessly rewrite a
    # v1 capture as v2 (OPL register stream preserved verbatim) and render that.
    render_path, dro_tmp = path, None
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        head = b""
    if head[:8] == b"DBRAWOPL" and not _dro_is_v2(head):
        try:
            v2 = _dro_v1_to_v2(path.read_bytes())   # may raise — no temp file created yet
            with tempfile.NamedTemporaryFile(suffix=".dro", delete=False) as t:
                dro_tmp = Path(t.name)              # register NOW so the finally always cleans up
                t.write(v2)
            render_path = dro_tmp
        except Exception as exc:        # noqa: BLE001 — fall back to raw file
            log.warning("DRO v1→v2 transcode failed for %s: %s", path, exc)

    try:
        # adplay exits 0 even when AdPlug can't decode the tune (e.g. a Sierra
        # .sci whose <prefix>patch.003 bank is missing → header-only ~44-byte WAV)
        # or when the requested subsong is empty (~2 KB / 0.01 s).  Both used to
        # slip past the old ``size < 1024`` guard or stream as silence; gate on
        # real audio LENGTH + amplitude instead.
        out = await _render_adlib_one(binary, render_path, subsong)
        if _wav_is_audible(out):
            return out

        size0 = 0
        try:
            size0 = out.stat().st_size
        except OSError:
            pass
        out.unlink(missing_ok=True)

        # Header-only (< 1 KB) ⇒ AdPlug decoded nothing at all (missing bank /
        # unsupported) — probing other subsongs won't help.  A tiny non-header
        # clip ⇒ an empty subsong of a multi-song file — probe the next few.
        if subsong == 0 and size0 >= 1024:
            for ss in range(1, _ADLIB_MAX_SUBSONG_PROBE + 1):
                cand = await _render_adlib_one(binary, render_path, ss)
                if _wav_is_audible(cand):
                    return cand
                cand.unlink(missing_ok=True)

        # Human-readable reason, distinguished by what adplay produced:
        #   size0 < 1 KB  → adplay decoded NOTHING (header-only WAV): a missing
        #                   companion bank (for bank formats) or an undecodable
        #                   / corrupt file (for the rest).
        #   size0 ≥ 1 KB  → adplay decoded the tune but it's silent/near-empty:
        #                   an empty or corrupt tune (or an all-empty multi-song).
        if size0 < 1024:
            if path.suffix.lower() in _ADLIB_COMPANION_GLOBS:
                detail = ("This file needs a companion instrument bank (e.g. "
                          "standard.bnk / patch.003 / insts.dat) that wasn't "
                          "found next to it in the archive or folder.")
            else:
                detail = ("This file couldn't be decoded — it looks corrupt or "
                          "is an unsupported AdLib variant.")
        else:
            detail = "This file is empty or corrupt — it contains no audio."
        raise HTTPException(422, detail)
    finally:
        if dro_tmp is not None:
            dro_tmp.unlink(missing_ok=True)


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


async def _backfill_rendered_duration(track_id: str, track, wav_path,
                                      placeholder: float | None = None) -> float | None:
    """Persist a render-only tune's REAL length once we've rendered it; return it.

    The scanner can't know a render-only format's length without rendering, so it
    stores a per-format placeholder and the library list shows e.g. "3:00"/"5:00".
    But the renderer runs to the song's natural end, so the served/probed WAV
    carries the true length — read it from the WAV header (cheap, header-only) and
    write it back via the store (AOF-journalled, so it survives restart; a rescan
    that resets the placeholder simply re-triggers this).

    Covers AdLib (180s placeholder), GME/chiptune — NSF/SPC/GBS/VGM/… —
    (``settings.sid_default_duration``), and tracker (pass ``placeholder=0`` so it
    only backfills when the scan's openmpt123 probe returned nothing).
    ``placeholder`` defaults to the AdLib 180s.  Gated on it, so the WRITE is a
    no-op once a real duration is stored (``stored<=0`` always backfills).
    Returns the real length in seconds, or None.  Best-effort — a duration
    cosmetic must never break playback.
    """
    try:
        if track is None:
            return None
        if placeholder is None:
            from soniqboom.core.metadata import _ADLIB_DEFAULT_DURATION
            placeholder = float(_ADLIB_DEFAULT_DURATION)
        meta = track.__dict__ if hasattr(track, "__dict__") else {}
        stored = float(meta.get("duration") or 0)
        if stored > 0 and abs(stored - float(placeholder)) > 0.01:
            return stored  # already carries a real, non-placeholder duration
        real = round(_wav_audio_seconds(wav_path), 2)
        if real <= 0:
            return None
        if abs(real - stored) >= 0.5:
            from soniqboom.core.store import get_store
            get_store().update_track_fields(track_id, {"duration": real})
            log.debug("AdLib duration backfilled: %s -> %.2fs", track_id, real)
        return real
    except Exception:
        return None


async def _resolve_adlib_local_path(track_id: str, path_str: str) -> Path | None:
    """Resolve a (possibly remote, possibly zip-member) AdLib track to a local
    renderable file path — the subset of ``stream_track``'s resolution that
    AdLib tunes need.  AdLib files are tiny, so fetching a remote one purely to
    probe its length is cheap (unlike big media, which prewarm deliberately
    skips).  Returns the local Path, or None if it couldn't be resolved.
    """
    loop = asyncio.get_running_loop()
    is_remote = path_str.startswith(("smb://", "ftp://", "http://", "https://"))
    if is_remote and "::" in path_str:
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        zip_rel, member = remote_path.split("::", 1)
        local_zip = await loop.run_in_executor(
            None, get_cache().fetch, scan_root, zip_rel, get_source(scan_root),
        )
        return await _get_or_extract_zip_member(
            f"{local_zip}::{member}", track_id,
            bank_fallback=_make_zip_bank_fallback(remote=(zip_rel, get_source(scan_root))),
        )
    if is_remote:
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        source = get_source(scan_root)
        # uade Amiga modules: fetch module + companion halves into one dir.
        if (source is not None
                and _uade_formats.classify(Path(remote_path).name) is not None):
            mat = await _materialize_loose_remote_uade(
                track_id, scan_root, remote_path, source,
            )
            if mat is not None:
                return mat
        globs = _ADLIB_COMPANION_GLOBS.get(Path(remote_path).suffix.lower())
        if globs and source is not None:
            mat = await _materialize_loose_remote_adlib(
                track_id, scan_root, remote_path, source, globs,
            )
            if mat is not None:
                return mat
        local = await loop.run_in_executor(
            None, get_cache().fetch, scan_root, remote_path, source,
        )
        return Path(local) if local else None
    if "::" in path_str:
        return await _get_or_extract_zip_member(
            path_str, track_id,
            bank_fallback=_make_zip_bank_fallback(local_zip=path_str.split("::")[0]),
        )
    return Path(path_str)


async def _materialize_loose_remote_uade(
    track_id: str, scan_root: str, remote_path: str, source,
) -> Path | None:
    """A loose uade Amiga module on a remote share, materialized WITH its
    companion halves (TFMX ``smpl.X`` etc.) in one local dir — the per-file
    remote cache would otherwise split the pair apart.  Same-directory,
    case-insensitive sibling matching (eagleplayers resolve companions
    case-insensitively themselves).  Returns None → caller falls back to a
    plain single-file fetch (fine for the many companion-less formats).
    """
    import posixpath
    loop = asyncio.get_running_loop()
    rp = remote_path.replace("\\", "/")
    tune_base = posixpath.basename(rp)
    rdir = posixpath.dirname(rp)
    wanted = {s.lower() for s in _uade_formats.companion_sibling_names(tune_base)}
    out_dir = _zip_extract_dir() / f"{track_id}.uade"
    tune_out = out_dir / tune_base
    try:
        st = await loop.run_in_executor(None, source.stat, remote_path)
        marker_val = f"{getattr(st, 'size', '')}:{getattr(st, 'mtime', '')}"
    except Exception:
        marker_val = ""
    lock = await _zip_lock_for(track_id)
    async with lock:
        marker = out_dir / ".loose_marker"
        if tune_out.exists() and marker.exists() and marker.read_text() == marker_val:
            _register_adlib_extract(track_id, out_dir)   # same budget/LRU pool
            return tune_out

        def _work() -> Path | None:
            import shutil
            try:
                entries = source.list_dir(rdir)
            except Exception:
                entries = []
            sibs = []
            for e in entries:
                if getattr(e, "is_dir", False):
                    continue
                base = posixpath.basename(
                    (getattr(e, "name", "") or "").replace("\\", "/"))
                if base and base.lower() in wanted:
                    sibs.append(
                        (base, getattr(e, "path", None) or posixpath.join(rdir, base)))
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            from soniqboom.core.remote_cache import get_cache
            try:
                local = get_cache().fetch(scan_root, remote_path, source)
                shutil.copyfile(local, tune_out)
            except Exception:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None
            for base, sib_path in sibs:
                try:
                    lp = get_cache().fetch(scan_root, sib_path, source)
                    shutil.copyfile(lp, out_dir / base)
                except Exception:
                    continue
            marker.write_text(marker_val)
            return tune_out

        tune = await loop.run_in_executor(None, _work)
        if tune is not None:
            _register_adlib_extract(track_id, out_dir)
            try:
                await asyncio.to_thread(_zip_evict_until_under_budget)
            except Exception:
                log.exception("UADE loose-remote eviction failed")
        return tune


async def _materialize_loose_remote_adlib(
    track_id: str, scan_root: str, remote_path: str, source, globs,
) -> Path | None:
    """A loose (non-zip) AdLib tune on a remote share needs its companion bank in
    the SAME directory (Sierra .sci → patch.003, ROL .bnk, KSM insts.dat), but
    the per-file remote cache pulls siblings to separate hash-keyed paths, so
    adplay can't find the bank.  Fetch the tune + every matching sibling bank
    from the remote directory into one local ``{track_id}.adlib`` dir and return
    the tune's path there.  Returns None (caller falls back to a plain fetch) if
    no matching companion sibling is present.
    """
    import fnmatch
    import posixpath
    loop = asyncio.get_running_loop()
    out_dir = _zip_extract_dir() / f"{track_id}.adlib"
    rp = remote_path.replace("\\", "/")
    tune_base = posixpath.basename(rp)
    rdir = posixpath.dirname(rp)
    tune_out = out_dir / tune_base
    try:
        st = await loop.run_in_executor(None, source.stat, remote_path)
        marker_val = f"{getattr(st, 'size', '')}:{getattr(st, 'mtime', '')}"
    except Exception:
        marker_val = ""
    lock = await _zip_lock_for(track_id)
    async with lock:
        marker = out_dir / ".loose_marker"
        if tune_out.exists() and marker.exists() and marker.read_text() == marker_val:
            # Cache hit — re-account (post-restart the in-memory budget is empty
            # though the dir persists) + bump LRU recency, mirroring the flat path.
            _register_adlib_extract(track_id, out_dir)
            return tune_out

        def _scan_companions(d: str, exclude_base: "str | None") -> list:
            try:
                entries = source.list_dir(d)
            except Exception:
                return []
            found = []
            for e in entries:
                if getattr(e, "is_dir", False):
                    continue
                base = posixpath.basename((getattr(e, "name", "") or "").replace("\\", "/"))
                if base and base != exclude_base and any(
                    fnmatch.fnmatch(base.lower(), g.lower()) for g in globs
                ):
                    found.append((base, getattr(e, "path", None) or posixpath.join(d, base)))
            return found

        def _work() -> Path | None:
            import shutil
            sibs = _scan_companions(rdir, tune_base)
            # If the tune's own dir has no companion bank, walk up a few parents
            # and use the closest one found — collections keep one standard.bnk at
            # a root with the ROLs in subfolders (see _ADLIB_BANK_PARENT_LEVELS).
            if not sibs:
                parent = rdir
                for _ in range(_ADLIB_BANK_PARENT_LEVELS):
                    parent = posixpath.dirname(parent)
                    if not parent or parent in ("/", "."):
                        break
                    sibs = _scan_companions(parent, None)
                    if sibs:
                        break
            if not sibs:
                return None
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            tune_out.write_bytes(source.read_file(remote_path))
            for base, sib_remote in sibs:
                try:
                    (out_dir / base).write_bytes(source.read_file(sib_remote))
                except Exception:
                    continue
            marker.write_text(marker_val)
            return tune_out

        tune = await loop.run_in_executor(None, _work)
        if tune is not None:
            _register_adlib_extract(track_id, out_dir)
            try:
                await asyncio.to_thread(_zip_evict_until_under_budget)
            except Exception:
                log.exception("AdLib loose-extract eviction failed")
        return tune


async def _probe_one_rendered_duration(track_id: str) -> float | None:
    """Render a render-only tune once, JUST to learn its length, persist it, and
    return it — the WAV is thrown away (a probe, not a play, so it never pollutes
    the conversion cache with multi-MB renders).  Covers AdLib/id-IMF and GME
    chiptunes (NSF/SPC/GBS/VGM/AY/KSS/…).  Returns None for other formats,
    already-known durations, or render failures (e.g. a missing companion bank →
    422, which just leaves the placeholder in place).
    """
    track = await get_track(track_id)
    if track is None:
        return None
    path_str = getattr(track, "path", "") or ""
    ext, _uade_named = _render_ident(path_str)
    is_adlib = ext in _ADLIB_EXTS or ext == ".imf"
    is_gme = ext in _GME_EXTS_STREAM
    is_uade = ext in _UADE_EXTS or _uade_named
    is_hvl = ext in _HVL_EXTS
    is_sc68 = ext in _SC68_EXTS
    # Bare .dsf might be a Dreamcast rip — decidable only once local.
    is_psf = ext in _PSF_STREAM_EXTS
    maybe_dreamcast = ext == ".dsf"
    # A bare .sid may be Amiga SidMon (no PSID magic) — probe-eligible, but
    # only decidable after the file is local; real C64 PSID bails below.
    maybe_sidmon = ext in _SID_EXTS and not is_uade
    if not (is_adlib or is_gme or is_uade or is_hvl or is_sc68 or is_psf
            or maybe_sidmon or maybe_dreamcast):
        return None
    if is_gme:
        placeholder = float(settings.sid_default_duration)
    elif (is_uade or is_hvl or is_sc68 or is_psf
            or maybe_sidmon or maybe_dreamcast):
        placeholder = 0.0   # render-only formats carry no scan-time duration
    else:
        from soniqboom.core.metadata import _ADLIB_DEFAULT_DURATION
        placeholder = float(_ADLIB_DEFAULT_DURATION)
    meta = track.__dict__ if hasattr(track, "__dict__") else {}
    stored = float(meta.get("duration") or 0)
    if stored > 0 and abs(stored - placeholder) > 0.01:
        return stored  # already a real duration — nothing to probe
    local = await _resolve_adlib_local_path(track_id, path_str)
    if local is None:
        return None
    if maybe_sidmon:
        if _is_c64_sid(local):
            return None       # real C64 — HVSC owns those durations
        is_uade = True
    if maybe_dreamcast and not is_psf:
        if not _dsf_is_dreamcast(local):
            return None       # Sony DSD stream — not render-only
        is_psf = True
    wav = None
    try:
        if is_gme:
            wav = await _render_gme(local)
        elif ext == ".imf":
            wav = await _render_imf(local)
        elif is_hvl:
            wav = await _render_hvl(local)
        elif is_psf:
            wav = await _render_psf(local)
        elif is_sc68:
            wav = await _render_sc68(local)
        elif is_uade:
            wav = await _render_uade(local, with_vu=False)
        else:
            wav = await _render_adlib(local)
        return await _backfill_rendered_duration(track_id, track, wav, placeholder)
    except HTTPException:
        return None      # undecodable / missing bank — leave the placeholder
    except Exception:
        return None
    finally:
        if wav is not None:
            try:
                Path(wav).unlink()
            except OSError:
                pass


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
from soniqboom.core import uade_formats as _uade_formats

# .ahx plus every registered uade suffix token (song.fc13, tune.dm2, …) and
# the archive layer's appended routing extensions (mdat.X → display X.mdat).
# Amiga PREFIX-form loose files (mdat.song) don't have a token extension —
# they're caught by ``_uade_formats.classify`` at the routing sites instead.
_UADE_EXTS = {".ahx"} | {f".{_t}" for _t in _uade_formats.new_suffix_tokens()}
_HVL_EXTS = {".hvl"}


def _is_c64_sid(path: Path) -> bool:
    """True if a ``.sid`` file is a real C64 tune (PSID/RSID magic).

    Modland stores Amiga SidMon modules as ``*.sid`` too — those carry no
    PSID header and must render via uade, not sidplayfp.  Unreadable files
    return True so the legacy sidplayfp path keeps ownership of errors.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in (b"PSID", b"RSID")
    except OSError:
        return True
_hvl2wav_bin: "Path | None" = None
# One lock per bundled build — two concurrent FIRST plays of different
# tracks could otherwise compile to the same output path simultaneously
# and exec a half-written binary (QA MN6).
_native_build_locks: dict[str, asyncio.Lock] = {}


def _native_build_lock(name: str) -> asyncio.Lock:
    lock = _native_build_locks.get(name)
    if lock is None:
        lock = _native_build_locks.setdefault(name, asyncio.Lock())
    return lock

# uade123 has no native "render exactly N seconds" mode — it relies on
# the player binary's end-detection.  Most AHX tunes are < 5 minutes;
# we cap at 8 to bound the worst case while leaving plenty of headroom
# for the rare longer arrangement.
_UADE_DEFAULT_TIMEOUT_S = 8 * 60


async def _render_uade(path: Path, subsong: int = 0, with_vu: bool = True) -> Path:
    """Render an AHX / Hively / Amiga-tracker module to WAV via uade123.

    Returns the temp-file path; caller (``conversion_cache.get_or_render``)
    moves it into the on-disk cache and unlinks the temp.

    Output spec: 44.1 kHz / stereo / 16-bit signed LE.  Matches what
    sidplayfp + openmpt123 produce so the downstream cast pipeline
    can treat all rendered formats uniformly.

    ``subsong`` is honoured for multi-tune containers (rare in AHX,
    common in HVL).  uade123 uses 0-indexed subsongs like the rest of
    SoniqBoom — no off-by-one translation needed.

    ``with_vu``: also capture uade's per-voice ``--write-audio`` dump in the
    SAME pass and convert it to a VUMR ``.vu`` sidecar next to the temp WAV —
    ``conversion_cache`` moves the pair together and the frontend's
    per-channel VU meters pick it up like a tracker module's.  Best-effort:
    a VU failure never fails the render.  The duration-probe path passes
    ``with_vu=False`` (its WAV is thrown away).
    """
    binary = _find_renderer(settings.uade123_path, "uade123")
    if not binary:
        raise HTTPException(
            501,
            "uade123 not installed — Amiga formats (AHX, TFMX, Future "
            "Composer, SidMon, …) require it. Install via 'brew install "
            "uade' (macOS) or 'apt install uade' (Debian/Ubuntu).",
        )

    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()

    # ``--filter=A1200`` picks the Amiga 1200 LED-filter model (the
    # default A500 sounds muffled on modern listeners).  ``--headphones``
    # adds a tiny stereo-widening effect that mimics what AHX players
    # commonly did at the time.  ``-e wav`` plus ``-f`` forces output
    # path (uade123 defaults to a temporary streaming sink).  ``-1``
    # (--one) is ESSENTIAL: without it uade plays the start subsong and
    # every FOLLOWING one concatenated into a single render (QA M1).
    cmd = [
        binary,
        "-1",
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
    if with_vu:
        await _uade_vu_pass(binary, path, subsong, Path(tmp_wav.name))
    return Path(tmp_wav.name)


# The Paula dump grows at ~2.5 MB per tune-second (measured); a runaway
# looping tune capped only by the 8-min kill would write ~1.2 GB per render
# slot (QA C3).  So the VU pass runs AFTER the main render, only when the
# now-known duration is within this cap, and only with disk headroom.
_UADE_VU_MAX_TUNE_S = 420
_UADE_VU_MIN_FREE_BYTES = 4 * 1024**3


async def _uade_vu_pass(
    binary: str, path: Path, subsong: int, wav_path: Path,
) -> None:
    """Best-effort per-voice VU sidecar via a second, dump-only uade run.

    Kept OUT of the main render so the dump size is bounded by the
    already-known tune duration and a disk-space check — a VU failure or
    skip never affects the audio render.
    """
    dump_tmp: Path | None = None
    try:
        duration = _wav_audio_seconds(wav_path)
        if not (0 < duration <= _UADE_VU_MAX_TUNE_S):
            log.debug("UADE VU skipped for %s: duration %.0fs out of range",
                      path.name, duration)
            return
        import shutil as _sh
        if _sh.disk_usage(tempfile.gettempdir()).free < (
                _UADE_VU_MIN_FREE_BYTES + int(duration * 3 * 1024 * 1024)):
            log.info("UADE VU skipped for %s: low disk", path.name)
            return
        _d = tempfile.NamedTemporaryFile(suffix=".uadedump", delete=False)
        _d.close()
        dump_tmp = Path(_d.name)
        cmd = [binary, "-1", "--filter=A1200", "--headphones",
               f"--write-audio={dump_tmp}", "-e", "wav", "-f", os.devnull]
        if subsong > 0:
            cmd += [f"--subsong={subsong}"]
        cmd += ["--", str(path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=min(duration * 2 + 60, 600))
        except asyncio.TimeoutError:
            proc.kill()
            return
        from soniqboom.core import uade_vu
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: uade_vu.parse_dump(dump_tmp, duration),
        )
        if result is not None and result.frames > 0:
            from soniqboom.core import openmpt_vu
            vu_path = wav_path.with_suffix(".vu")
            await loop.run_in_executor(
                None, openmpt_vu.write_sidecar, vu_path, result,
            )
            log.info("UADE VU sidecar for %s: %d ch × %d frames @ %d Hz",
                     path.name, result.channels, result.frames,
                     result.sample_rate)
    except Exception:
        log.warning("UADE VU extraction failed for %s", path, exc_info=True)
    finally:
        if dump_tmp is not None:
            try:
                dump_tmp.unlink(missing_ok=True)
            except OSError:
                pass


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
    # A pre-built hvl2wav on PATH (e.g. baked into a multi-stage Docker image by
    # the builder stage) wins — no C compiler is needed at runtime.
    _pre = shutil.which("hvl2wav")
    if _pre:
        _hvl2wav_bin = Path(_pre)
        return _hvl2wav_bin
    async with _native_build_lock("hvl2wav"):
        if _hvl2wav_bin and _hvl2wav_bin.exists():
            return _hvl2wav_bin
        return await _build_hvl2wav()


async def _build_hvl2wav() -> "Path | None":
    global _hvl2wav_bin
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
        _tmp_out = binp.with_suffix(".building")
        proc = await asyncio.create_subprocess_exec(
            cc, "-O2", "-w", str(csrc[0]), str(csrc[1]), "-o", str(_tmp_out), "-lm",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("HVL: hvl2wav build failed to launch: %s", exc)
        return None
    if proc.returncode != 0 or not _tmp_out.exists():
        log.warning("HVL: hvl2wav build failed: %s", (err or b"").decode("utf-8", "replace")[:300])
        _tmp_out.unlink(missing_ok=True)
        return None
    os.replace(_tmp_out, binp)     # atomic — no truncated binary on interrupt
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


# ── PSF console-music family (PSF/PSF2/USF/GSF/2SF/SSF/DSF/NCSF) ──────────
# Rendered via zxtune123 — the only cross-format CLI that bundles the
# reference cores (Highly Experimental, Highly Theoretical, lazyusf2, mGBA,
# vio2sf).  Linux: prebuilt from storage.zxtune.ru; macOS: built from source
# (see install.sh).  Absent binary → clear 501.

_PSF_STREAM_EXTS = {
    ".psf", ".minipsf", ".psf2", ".minipsf2", ".usf", ".miniusf",
    ".gsf", ".minigsf", ".2sf", ".mini2sf", ".ssf", ".minissf",
    ".minidsf", ".ncsf", ".minincsf",
}


def _dsf_is_dreamcast(path: Path) -> bool:
    """Content sniff for the ``.dsf`` extension collision: 'PSF\\x12' =
    Sega Dreamcast rip (sequenced); 'DSD ' = Sony DSD audio stream."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"PSF\x12"
    except OSError:
        return False


async def _render_psf(path: Path, subsong: int = 0) -> Path:
    """Render a PSF-family file to WAV via zxtune123.

    PSF rips are one-track-per-file (minipsf per song, shared *lib beside
    it) — ``subsong`` is accepted for signature parity but unused.  zxtune
    honours the embedded length/fade tags for the stop point.
    """
    binary = _find_renderer(settings.zxtune123_path, "zxtune123")
    if not binary:
        raise HTTPException(
            501,
            "zxtune123 not installed — console music rips (PSF/USF/GSF/2SF/"
            "SSF/DSF) require it. Re-run install.sh, or set "
            "renderers.zxtune123_path.",
        )
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    # zxtune REFUSES to overwrite an existing file yet still exits 0
    # ("File already exists", rc=0 — verified), which cached the pre-created
    # empty temp as a "successful" render.  Unlink the placeholder first.
    Path(tmp_wav.name).unlink(missing_ok=True)
    # File-based WAV backend: ``--wav filename=<out>``.  No display needed.
    cmd = [binary, "--silent", "--wav", f"filename={tmp_wav.name}", str(path)]
    await _await_renderer(
        cmd, Path(tmp_wav.name), timeout=600, kind="PSF")
    return Path(tmp_wav.name)


# ── Atari ST renderers (SNDH / YM / SC68) ─────────────────────────────────
# Three engines, chosen for accuracy (2026-07 research + head-to-head tests):
#   .sndh → psgplay   (modern 68000+YM2149+MFP+STE-DMA emulation; rendered
#                      10/10 test files incl. every one brew's sc68 2.2.1
#                      rejects; built from source by install.sh)
#   .ym   → StSound   (Arnaud Carré's reference engine — he CREATED the YM
#                      format; BSD, vendored under soniqboom/native/stsound
#                      and compiled on first use like hvl2wav; handles the
#                      LHA wrapper + every YM variant; mono 44.1 kHz out)
#   .sc68 → sc68      (only available player for native .sc68 disks; 2.2.1
#                      CLI quirks handled: options AFTER the filename, raw
#                      PCM on stdout, config via an isolated SC68_HOME)

_SNDH_EXTS = {".sndh"}
_YM_EXTS = {".ym"}
_SC68_EXTS = {".sc68"}
_ATARI_DEFAULT_S = 180        # SNDH TIME tag missing/0 → render this long
_ym2wav_bin: "Path | None" = None


async def _ensure_ym2wav() -> "Path | None":
    """Return a built StSound ``ym2wav`` path, compiling once if needed."""
    global _ym2wav_bin
    if _ym2wav_bin and _ym2wav_bin.exists():
        return _ym2wav_bin
    # A pre-built ym2wav on PATH (e.g. baked into a multi-stage Docker image by
    # the builder stage) wins — no C++ compiler is needed at runtime.
    _pre = shutil.which("ym2wav")
    if _pre:
        _ym2wav_bin = Path(_pre)
        return _ym2wav_bin
    async with _native_build_lock("ym2wav"):
        if _ym2wav_bin and _ym2wav_bin.exists():
            return _ym2wav_bin
        return await _build_ym2wav()


async def _build_ym2wav() -> "Path | None":
    global _ym2wav_bin
    src_dir = Path(__file__).resolve().parent.parent / "native" / "stsound"
    main_cpp = src_dir / "Ym2Wav" / "Ym2Wav.cpp"
    lib_dir = src_dir / "StSoundLibrary"
    if not main_cpp.exists() or not lib_dir.is_dir():
        log.warning("YM: vendored StSound source missing under %s", src_dir)
        return None
    srcs = [main_cpp] + sorted(lib_dir.glob("*.cpp")) + sorted(
        (lib_dir / "LZH").glob("*.cpp"))
    from soniqboom.config import get_data_dir
    out_dir = get_data_dir() / "native"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    binp = out_dir / "ym2wav"
    newest = max(p.stat().st_mtime for p in srcs)
    if binp.exists() and binp.stat().st_mtime >= newest:
        _ym2wav_bin = binp
        return binp
    cxx = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")
    if not cxx:
        log.warning("YM: no C++ compiler — cannot build StSound ym2wav")
        return None
    _tmp_out = binp.with_suffix(".building")
    cmd = [cxx, "-O2", "-w", "-o", str(_tmp_out),
           *[str(p) for p in srcs],
           "-I", str(lib_dir), "-I", str(lib_dir / "LZH")]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("YM: ym2wav build failed to launch: %s", exc)
        return None
    if proc.returncode != 0 or not _tmp_out.exists():
        log.warning("YM: ym2wav build failed: %s",
                    (err or b"").decode("utf-8", "replace")[:300])
        _tmp_out.unlink(missing_ok=True)
        return None
    os.replace(_tmp_out, binp)     # atomic — no truncated binary on interrupt
    try:
        binp.chmod(0o755)
    except OSError:
        pass
    log.info("YM: built StSound ym2wav at %s", binp)
    _ym2wav_bin = binp
    return binp


# Raw YM register-dump magics StSound's YmMusic::ymDecode accepts (see
# soniqboom/native/stsound/StSoundLibrary/Ymload.cpp).  Anything else with a
# .ym extension is either a foreign Atari format mislabelled .ym (e.g. the
# Dyter-07 "YMST" native-module dumps) or a corrupt file — no bundled engine
# (StSound, zxtune123, sc68, openmpt123) can decode them.
# The decodability pre-flight (``ym_is_decodable`` + ``_YM_RAW_MAGICS``) lives in
# core.metadata — shared with the scanner, which uses the SAME predicate to stamp
# ``defect="corrupt"`` at scan, so the badge appears iff play returns 415.


async def _render_ym(path: Path, subsong: int = 0) -> Path:
    """Render an Atari ST ``.ym`` register dump via StSound's Ym2Wav.

    YM files are single-tune (no subsongs).  Output is mono 16-bit
    44.1 kHz WAV — browsers and the transcode pipeline handle mono fine.
    """
    from soniqboom.core.metadata import ym_is_decodable
    if not ym_is_decodable(path):
        raise HTTPException(
            415,
            "This .ym file isn't a YM register dump StSound can decode — it's "
            "either a foreign Atari format mislabelled .ym or a corrupt "
            "LHA-wrapped file. No available engine can render it.",
        )
    binary = await _ensure_ym2wav()
    if not binary:
        raise HTTPException(
            501,
            "YM decoder unavailable — a C++ compiler is required to build "
            "the bundled StSound engine.",
        )
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    cmd = [str(binary), str(path), tmp_wav.name]     # exactly two args
    await _await_renderer(cmd, Path(tmp_wav.name), timeout=300, kind="YM")
    return Path(tmp_wav.name)


def _sndh_info(path: Path) -> tuple[int, dict[int, int]]:
    """(default_track, {track: seconds}) from ``psgplay -i`` tags (``!#`` /
    ``TIME``).  default_track falls back to 1."""
    binary = _find_renderer(settings.psgplay_path, "psgplay")
    default_track, times = 1, {}
    if not binary:
        return default_track, times
    import subprocess as _sp
    try:
        r = _sp.run([binary, "-i", str(path)], capture_output=True,
                    text=True, timeout=20)
    except (_sp.TimeoutExpired, OSError):
        return default_track, times
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts[:2] != ["tag", "field"] or len(parts) < 4:
            continue
        if parts[2] == "!#":
            try:
                default_track = max(1, int(parts[3]))
            except ValueError:
                pass
        elif parts[2] == "TIME" and len(parts) >= 5:
            try:
                times[int(parts[3])] = max(0, int(parts[4]))
            except ValueError:
                continue
    return default_track, times


async def _render_sndh(path: Path, subsong: int = 0) -> Path:
    """Render an Atari ST SNDH file via psgplay (stereo 16-bit 44.1 kHz).

    Subsong semantics mirror SID: the param is the 1-BASED track number,
    0 = the tune's own default track (SNDH ``!#`` tag).  psgplay
    hard-errors on ``--stop=auto`` when a tune declares no TIME tag, so a
    ``--length`` is ALWAYS passed: the tag's duration when present, else
    the Atari default cap.
    """
    binary = _find_renderer(settings.psgplay_path, "psgplay")
    if not binary:
        raise HTTPException(
            501,
            "psgplay not installed — Atari ST SNDH requires it. Re-run "
            "install.sh (it builds psgplay from source) or set "
            "renderers.psgplay_path.",
        )
    loop = asyncio.get_running_loop()
    default_track, times = await loop.run_in_executor(None, _sndh_info, path)
    track = subsong if subsong > 0 else default_track
    secs = times.get(track, 0)
    # Hard-cap the length: an untrusted TIME tag ("TIME 1 999999999") would
    # otherwise drive an unbounded render + timeout — filling the disk and
    # pinning a render slot forever (QA C1, 2026-07-02).
    length = min(secs if secs > 0 else _ATARI_DEFAULT_S, 3600)
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    cmd = [binary, "-t", str(track), "-f", "44100",
           f"--length={length}", "-o", tmp_wav.name, str(path)]
    await _await_renderer(
        cmd, Path(tmp_wav.name), timeout=max(60, length + 60), kind="SNDH")
    return Path(tmp_wav.name)


def _sc68_home() -> Path:
    """An isolated SC68_HOME with our config (44.1 kHz), created once.

    sc68 2.2.1 has no sample-rate CLI flag — the rate lives in
    ``config.txt``.  A private home keeps us off the user's ~/.sc68 and
    pins the output format the WAV wrapper below assumes.
    """
    from soniqboom.config import get_data_dir
    home = get_data_dir() / "sc68_home"
    d = home / ".sc68"
    d.mkdir(parents=True, exist_ok=True)
    conf = d / "config.txt"
    if not conf.exists():
        conf.write_text(
            "# SoniqBoom-managed sc68 config\n"
            "sampling_rate=44100\n"
            f"default_time={_ATARI_DEFAULT_S}\n"
        )
    return home


async def _render_sc68(path: Path, subsong: int = 0) -> Path:
    """Render a native ``.sc68`` disk via the sc68 CLI.

    sc68 2.2.1 quirks (verified empirically): options must come AFTER the
    filename; output is RAW stereo signed 16-bit machine-endian PCM on
    stdout at the config-file sample rate — wrapped into a WAV here.
    Embedded per-track durations are honoured by sc68 itself.
    """
    binary = _find_renderer(settings.sc68_path, "sc68")
    if not binary:
        raise HTTPException(
            501,
            "sc68 not installed — native .sc68 files require it. "
            "Install via 'brew install sc68' (macOS) or from sc68.atari.org.",
        )
    # Subsong semantics mirror SID: param = 1-based track, 0 = default (sc68
    # itself picks the disk's default when --track is omitted... but 2.2.1's
    # "0 = all tracks" would concatenate, so resolve 0 → track 1 explicitly).
    track = subsong if subsong > 0 else 1
    tmp_raw = tempfile.NamedTemporaryFile(suffix=".raw", delete=False)
    tmp_raw.close()
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    home = await asyncio.get_running_loop().run_in_executor(None, _sc68_home)
    import os as _os
    env = dict(_os.environ, SC68_HOME=str(home), HOME=str(home))
    ok = False
    try:
        # NOTE: no --quiet — sc68 2.2.1's option parser rejects it (verified);
        # info chatter goes to stderr anyway, PCM alone arrives on stdout.
        with open(tmp_raw.name, "wb") as _raw_out:      # QA MN3: close the fd
            proc = await asyncio.create_subprocess_exec(
                binary, str(path), f"--track={track}",
                stdout=_raw_out, stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)  # reap
                except asyncio.TimeoutError:
                    pass
                raise HTTPException(504, "sc68 render timed out")
        raw_size = Path(tmp_raw.name).stat().st_size
        if proc.returncode != 0 or raw_size < 8820:   # <0.05 s → failed
            raise HTTPException(
                422,
                "This .sc68 file couldn't be decoded — it may be corrupt or "
                "use an unsupported variant.",
            )
        # RIFF sizes are uint32 — a runaway/looping disk that outrendered the
        # timeout budget would overflow struct.pack into an unclean 500
        # (QA C1).  2 GB ≈ 3.4 h of audio: nothing legitimate.
        if raw_size > 2 * 1024**3:
            raise HTTPException(
                422, "This .sc68 file rendered implausibly long output — "
                     "it looks like an endless loop.")
        # Wrap raw stereo s16le PCM into a WAV container.  (sc68's output is
        # machine-endian; every supported host is little-endian, matching
        # the LE header written here.)
        import struct as _struct
        with open(tmp_wav.name, "wb") as w:
            w.write(b"RIFF" + _struct.pack("<I", 36 + raw_size) + b"WAVE")
            w.write(b"fmt " + _struct.pack("<IHHIIHH", 16, 1, 2, 44100,
                                           44100 * 4, 4, 16))
            w.write(b"data" + _struct.pack("<I", raw_size))
            with open(tmp_raw.name, "rb") as r:
                shutil.copyfileobj(r, w, 1024 * 1024)
        ok = True
        return Path(tmp_wav.name)
    finally:
        Path(tmp_raw.name).unlink(missing_ok=True)
        if not ok:
            # QA C2: every failure path (504/422/wrap error) previously
            # orphaned the pre-created output temp file.
            Path(tmp_wav.name).unlink(missing_ok=True)


def _is_safari(request: Request) -> bool:
    """True for desktop/iOS Safari but not Chrome, Edge, or other Chromium UAs.

    Chrome's UA also contains "Safari"; Edge contains "Edg/"; Chromium forks
    add "Chrome" or their own token. Require "Safari" and absence of those.
    """
    ua = request.headers.get("user-agent", "")
    if "Safari" not in ua:
        return False
    return not any(t in ua for t in ("Chrome", "Chromium", "Edg/", "OPR/"))


def _safari_lacks_ogg(request: Request) -> bool:
    """True for Safari older than 18.4.  WebKit only added Opus/Vorbis-in-Ogg
    ``<audio>`` playback in Safari 18.4 (2025); serving native ``.ogg``/``.opus``
    to an older Safari fails silently (the element just never plays).  Such
    clients are routed through the transcoder to WAV instead.  Non-Safari and
    Safari >= 18.4 return False.  Version is parsed from the UA's ``Version/x.y``
    token (no regex); a Safari UA with no parseable version is treated as old
    (the conservative choice — transcoding always plays)."""
    if not _is_safari(request):
        return False
    ua = request.headers.get("user-agent", "")
    tok = "Version/"
    i = ua.find(tok)
    if i < 0:
        return True
    ver = ua[i + len(tok):].split()[0]          # e.g. "18.3.1"
    parts = ver.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return True
    return (major, minor) < (18, 4)


def _client_caps(request: Request) -> "set[str] | None":
    """Parse the ``sb_caps`` cookie the web UI sets from the browser's own
    ``HTMLMediaElement.canPlayType`` probe — a dot-separated list of codec
    tokens the browser reported it can decode (e.g. ``aac.alac.opus.flac``).

    Returns the set of client-playable codecs, or ``None`` when the cookie is
    absent (Subsonic/DLNA/Cast, or a web client that hasn't booted the probe
    yet) → the caller falls back to UA heuristics.  This is authoritative and
    FUTURE-PROOF: when a browser gains a codec, its ``canPlayType`` starts
    reporting it and the server direct-serves it with no code change."""
    raw = request.cookies.get("sb_caps")
    if raw is None:
        return None
    caps = {t for t in raw.split(".") if t}
    # An empty set (probe glitch, or a browser that reported nothing) is treated
    # as "undeclared" → UA fallback, never "supports nothing" (which would
    # needlessly transcode for everyone).
    return caps or None


def _client_supports(codec: str, request: Request) -> "bool | None":
    """True/False when the client has DECLARED capabilities covering ``codec``;
    ``None`` when it hasn't declared any (caller uses a UA fallback)."""
    caps = _client_caps(request)
    if caps is None:
        return None
    return codec in caps


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

# Guards every mutation of the four shared structures above + below
# (_ZIP_EXTRACT_CACHE / _ZIP_EXTRACT_TOTAL_BYTES / _zip_pin_refs /
# _zip_pending_purge).  Must be a *threading* lock, not asyncio: eviction runs
# in a worker thread (``to_thread(_zip_evict_until_under_budget)``) concurrently
# with the event-loop mutators (extract / pin / unpin / clear).  CRITICAL: never
# hold it across an ``await`` — that would block the whole loop thread on any
# other coroutine's acquire.  Every critical section here is a tiny synchronous
# block; file I/O (unlink) is always done AFTER releasing the lock.
_zip_state_lock = threading.Lock()


def _zip_pin(track_id: str) -> None:
    with _zip_state_lock:
        _zip_pin_refs[track_id] = _zip_pin_refs.get(track_id, 0) + 1


def _zip_unpin(track_id: str) -> None:
    pending = None
    with _zip_state_lock:
        cur = _zip_pin_refs.get(track_id, 0)
        if cur <= 1:
            _zip_pin_refs.pop(track_id, None)
            # If eviction queued an unlink while pinned, take it to run below.
            pending = _zip_pending_purge.pop(track_id, None)
        else:
            _zip_pin_refs[track_id] = cur - 1
    if pending:                              # remove outside the lock (file or dir)
        _zip_drop_path(pending)


def _zip_evict_until_under_budget() -> None:
    """LRU evict ZIP-extract entries until under the configured budget.

    Pinned entries (currently being streamed) defer their unlink until
    the last reader unpins — the file is removed from the in-memory
    cache immediately so a new extraction takes over the cache slot,
    but its on-disk bytes survive until the active stream finishes.

    Runs in a worker thread.  The index/counter walk happens under
    ``_zip_state_lock``; the actual unlinks are collected and performed
    after the lock is released so file I/O never blocks other mutators.
    """
    global _ZIP_EXTRACT_TOTAL_BYTES
    max_bytes = _zip_extract_max_bytes()
    to_unlink: list[str] = []
    with _zip_state_lock:
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
                to_unlink.append(path_to_drop)
    for p in to_unlink:                      # remove outside the lock (file or dir)
        _zip_drop_path(p)


def _zip_drop_path(p: str) -> None:
    """Remove a cache entry's on-disk artifact — a flat extracted FILE or an
    AdLib ``.adlib`` companion DIRECTORY (tune + bank).  Silent, best-effort;
    the plain ``unlink`` the budget paths used before would raise on a dir."""
    import shutil
    try:
        path = Path(p)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _register_adlib_extract(track_id: str, out_dir: Path) -> None:
    """Account a materialized ``.adlib`` directory (tune + companion bank) in the
    SAME LRU byte budget as flat extracts, so it's bounded + LRU-evictable
    instead of accumulating until restart.  Idempotent: replaces any prior entry
    for this track_id (the dir is rewritten in place on a fresh materialization).
    """
    global _ZIP_EXTRACT_TOTAL_BYTES
    try:
        # ``rglob`` (not ``iterdir``) so a nested payload counts too — Sonix
        # keeps its bulk in an ``Instruments/`` subdir; a flat ``iterdir`` would
        # register only the top-level module + a few companions and let the LRU
        # byte budget overshoot ~365× before eviction fires.
        size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    except OSError:
        size = 0
    with _zip_state_lock:
        prev = _ZIP_EXTRACT_CACHE.get(track_id)
        if prev:
            _ZIP_EXTRACT_TOTAL_BYTES = max(0, _ZIP_EXTRACT_TOTAL_BYTES - prev.get("size", 0))
        _ZIP_EXTRACT_CACHE[track_id] = {
            "path": str(out_dir),
            "extracted_at": time.time(),
            "size": size,
        }
        _ZIP_EXTRACT_TOTAL_BYTES += size


async def clear_zip_extract_cache() -> dict:
    """Clear the extracted-from-ZIP audio cache wholesale.

    Owns both halves of the cache: the in-memory index (``_ZIP_EXTRACT_CACHE``
    + ``_ZIP_EXTRACT_TOTAL_BYTES``) and the on-disk files under
    ``data_dir/zip-extracts/``.  Honours read pins exactly like LRU eviction —
    a member currently being streamed has its on-disk bytes deferred until the
    last reader unpins (tracked in ``_zip_pending_purge``), so clearing the
    cache never yanks an in-flight Range read out from under a player.

    Returns ``{"cleared", "deferred", "path"}`` (+ ``"failed"`` /
    ``"failed_samples"`` when some files resisted removal).
    """
    import shutil
    global _ZIP_EXTRACT_TOTAL_BYTES
    cleared = 0
    deferred = 0
    errors: list[str] = []
    to_unlink: list[str] = []
    extract_dir = _zip_extract_dir()

    # 1. Drain the in-memory index under _zip_state_lock (atomic vs the
    #    worker-thread evictor).  Collect non-pinned files to unlink after the
    #    lock drops; pinned tracks defer their unlink to _zip_unpin.  No await
    #    runs anywhere in this function, so no other coroutine (extract / unpin)
    #    can interleave between the drain and the unlinks — the clear is atomic
    #    against the event loop too.
    with _zip_state_lock:
        for tid in list(_ZIP_EXTRACT_CACHE.keys()):
            entry = _ZIP_EXTRACT_CACHE.pop(tid, None)
            if not entry:
                continue
            path_to_drop = entry.get("path")
            if tid in _zip_pin_refs:
                if path_to_drop:
                    _zip_pending_purge[tid] = path_to_drop
                deferred += 1
                continue
            if path_to_drop:
                to_unlink.append(path_to_drop)
        _ZIP_EXTRACT_TOTAL_BYTES = 0
        protected = set(_zip_pending_purge.values())
        # Also protect the .adlib dir of any track currently pinned (mid-render),
        # even if it isn't a registered/deferred cache entry — so a clear can't
        # rmtree a companion bank out from under an in-flight adplay render.
        for tid in list(_zip_pin_refs):
            protected.add(str(extract_dir / f"{tid}.adlib"))

    for p in to_unlink:
        try:
            pp = Path(p)
            if pp.is_dir():                  # an AdLib .adlib companion dir
                shutil.rmtree(pp)
            else:
                pp.unlink(missing_ok=True)
            cleared += 1
        except OSError as exc:
            errors.append(f"{Path(p).name}: {exc.strerror or 'error'}")

    # 2. Sweep any orphan files left on disk (entries already evicted from the
    #    index, partial extracts, …) — but never touch a file an active stream
    #    still owns (pinned → in `protected`).
    try:
        for de in os.scandir(extract_dir):
            if de.path in protected:
                continue
            try:
                # AdLib companion materialization makes a "<track_id>.adlib" DIRECTORY (the
                # tune + its bank/patch); everything else is a flat file.
                if de.is_dir(follow_symlinks=False):
                    shutil.rmtree(de.path, ignore_errors=True)
                elif de.is_file(follow_symlinks=False):
                    os.unlink(de.path)
                else:
                    continue
                cleared += 1
            except OSError as exc:
                errors.append(f"{de.name}: {exc.strerror or 'error'}")
    except (FileNotFoundError, PermissionError, OSError):
        pass

    out: dict = {"cleared": cleared, "deferred": deferred, "path": str(extract_dir)}
    if errors:
        out["failed"] = len(errors)
        out["failed_samples"] = errors[:5]
        log.warning(
            "clear-zip-extract: %d files could not be removed (e.g. %s)",
            len(errors), "; ".join(errors[:5]),
        )
    return out


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
                if child.is_dir():           # AdLib "<track_id>.adlib" extract dir
                    import shutil
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# AdLib / OPL2 formats whose AdPlug player needs companion instrument-bank /
# patch files in the SAME directory as the tune.  Map: extension -> filename
# globs (case-insensitive) to materialize alongside it.  Confirmed empirically
# against adplay (AdPlug) 1.9 — without these, adplay exits 0 but writes a
# silent, header-only WAV.  Every other AdLib format AdPlug handles is
# self-contained (.laa/.d00/.cmf/.rad/.hsc/.a2m/.bam/.dro/.rix/...).
_ADLIB_COMPANION_GLOBS = {
    ".sci": ("*patch.003",),   # Sierra On-Line  (kq1patch.003, icepatch.003, ...)
    ".rol": ("*.bnk",),        # AdLib Visual Composer  (standard.bnk)
    ".ksm": ("insts.dat",),    # Ken Silverman's Music Format
    # PSF family: mini files reference shared driver/sample libs via _lib
    # tags; the near-universal rip convention keeps them in the same dir.
    # Materializing every same-family lib is a safe superset (multi-_lib
    # sets exist).  NOTE ``.dsf`` (Dreamcast minis) is deliberately absent —
    # the extension collides with Sony DSD; its zip/remote lib fetch is
    # handled by the ``.minidsf`` entry + local-loose sibling presence, and
    # a big DSD .dsf must never be routed through companion extraction.
    ".psf": ("*.psflib",), ".minipsf": ("*.psflib",),
    ".psf2": ("*.psf2lib",), ".minipsf2": ("*.psf2lib",),
    ".usf": ("*.usflib",), ".miniusf": ("*.usflib",),
    ".gsf": ("*.gsflib",), ".minigsf": ("*.gsflib",),
    ".2sf": ("*.2sflib",), ".mini2sf": ("*.2sflib",),
    ".ssf": ("*.ssflib",), ".minissf": ("*.ssflib",),
    ".minidsf": ("*.dsflib",),
    ".ncsf": ("*.ncsflib",), ".minincsf": ("*.ncsflib",),
}


def _adlib_companion_names(path_str: str, member_dir: str, globs) -> list[str]:
    """Member names (as stored, ready to read back) in ``member_dir`` of the
    INNERMOST archive on ``path_str`` matching any of ``globs`` — the bank/patch
    siblings an AdLib tune needs beside it.

    Names are pulled from the SAME reader the extractor reads them back with —
    ``archive.list_members`` for a single-level local archive (handles ZIP *and*
    LHA/LZH), the inner zip's own namelist when nested — so the returned name
    always resolves via ``_read_from_zip_path``, even for DOS backslash
    separators or an LHA container.  Matching is on a slash-normalised copy; the
    ORIGINAL stored name is returned.
    """
    import fnmatch
    import io
    import os as _os
    import zipfile
    found: list[str] = []
    parts = path_str.split("::")
    if len(parts) < 2:
        return found
    try:
        if len(parts) == 2:
            # Single-level local archive — format-aware (ZIP + LHA/LZH).  Use the
            # UNFILTERED namelist (list_members drops non-playable banks); the
            # names it yields round-trip back through archive.read_member.
            from soniqboom.core import archive as _archive
            names = _archive.raw_namelist(parts[0])
        else:
            # Nested: read the innermost archive's bytes with the same walker the
            # extractor uses, then list it (inner archives are zips in practice).
            from soniqboom.core.scanner import _read_from_zip_path
            inner_bytes, _ = _read_from_zip_path("::".join(parts[:-1]))
            with zipfile.ZipFile(io.BytesIO(inner_bytes)) as zf:
                names = zf.namelist()
        # Group matching banks by their dir inside the archive, then take the
        # member's own dir — else the CLOSEST ancestor dir that holds one (a
        # zipped collection may keep a single standard.bnk at a root with the
        # tunes in subfolders, mirroring the loose-file parent-walk).
        by_dir: "dict[str, list[str]]" = {}
        for n in names:
            norm = n.replace("\\", "/")        # match on a normalised copy …
            base = _os.path.basename(norm)
            if any(fnmatch.fnmatch(base.lower(), g.lower()) for g in globs):
                by_dir.setdefault(_os.path.dirname(norm), []).append(n)  # keep ORIGINAL
        d = member_dir
        seen: "set[str]" = set()
        while d not in seen:
            seen.add(d)
            if d in by_dir:
                for n in by_dir[d]:
                    if n not in found:
                        found.append(n)
                break
            nd = _os.path.dirname(d)
            if nd == d:
                break
            d = nd
    except Exception:
        pass
    return found


def _dir_has_companion(d: Path, globs) -> bool:
    """True if directory *d* already holds a file matching any of *globs*."""
    import fnmatch
    try:
        return any(f.is_file() and any(fnmatch.fnmatch(f.name.lower(), g.lower()) for g in globs)
                   for f in d.iterdir())
    except OSError:
        return False


def _make_zip_bank_fallback(*, remote=None, local_zip=None):
    """Build a sync ``fn(out_dir, globs)`` for a bank-dependent AdLib tune zipped
    WITHOUT its bank: if *out_dir* has no companion bank, walk the .zip's
    container dir + a few parents on the share/FS for a loose one and drop it in.
    ``remote`` is ``(zip_rel, source)``; ``local_zip`` is the on-disk .zip path.
    Returns None if neither is supplied (nowhere to look)."""
    import fnmatch, os as _os, posixpath
    if remote is not None:
        zip_rel, source = remote
        start = posixpath.dirname(str(zip_rel).replace("\\", "/"))
        def _list(d):
            try:
                entries = source.list_dir(d)
            except Exception:
                return []
            out = []
            for e in entries:
                if getattr(e, "is_dir", False):
                    continue
                b = posixpath.basename((getattr(e, "name", "") or "").replace("\\", "/"))
                if b:
                    out.append((b, getattr(e, "path", None) or posixpath.join(d, b)))
            return out
        def _read(ref):
            return source.read_file(ref)
        _up = posixpath.dirname
    elif local_zip is not None:
        start = _os.path.dirname(str(local_zip))
        def _list(d):
            try:
                names = _os.listdir(d)
            except OSError:
                return []
            return [(n, _os.path.join(d, n)) for n in names
                    if _os.path.isfile(_os.path.join(d, n))]
        def _read(ref):
            with open(ref, "rb") as fh:        # explicit close, not GC-dependent
                return fh.read()
        _up = _os.path.dirname
    else:
        return None

    def _fb(out_dir: Path, globs) -> None:
        if _dir_has_companion(out_dir, globs):
            return                             # the zip already supplied a bank
        # Walk the .zip's container dir + a few parents; materialize EVERY bank in
        # the FIRST dir that has one (AdPlug needs the exact bank NAME it derives,
        # so drop them all and let it pick the right one), then stop.  Never lists
        # the share root — the upward walk halts before "" / "/" / ".".
        d, seen = start, set()
        for _ in range(_ADLIB_BANK_PARENT_LEVELS + 1):
            if not d or d in seen or d in ("/", "."):
                break
            seen.add(d)
            wrote = False
            for base, ref in _list(d):
                if any(fnmatch.fnmatch(base.lower(), g.lower()) for g in globs):
                    try:
                        (out_dir / base).write_bytes(_read(ref))
                        wrote = True
                    except Exception:
                        continue
            if wrote:
                return
            d = _up(d)
    return _fb


async def _extract_adlib_with_companions(
    path_str: str, track_id: str, outer_zip: Path, globs, bank_fallback=None,
) -> Path | None:
    """Materialize an AdLib tune that needs companion bank/patch files so adplay
    can decode it (Sierra ``.sci`` -> ``patch.003``, ROL ``.bnk``, KSM
    ``insts.dat``).  The generic extractor pulls the tune out alone under a
    track-id name, so AdPlug can't find its bank and silently writes a
    header-only WAV.  Here we drop the tune (under its ORIGINAL name) plus every
    matching companion into a per-track directory.  Idempotent; re-extracts if
    the archive changed.
    """
    import os as _os
    from soniqboom.core.scanner import _read_from_zip_path
    parts = path_str.split("::")
    member = parts[-1].replace("\\", "/")          # DOS-era zips can use backslashes
    music_base = _os.path.basename(member)
    member_dir = _os.path.dirname(member)
    out_dir = _zip_extract_dir() / f"{track_id}.adlib"
    music_out = out_dir / music_base
    try:
        zip_mtime = str(outer_zip.stat().st_mtime)
    except OSError:
        return None
    lock = await _zip_lock_for(track_id)
    async with lock:
        marker = out_dir / ".zip_mtime"
        if (music_out.exists() and marker.exists() and marker.read_text() == zip_mtime
                and _dir_has_companion(out_dir, globs)):
            # Cache hit WITH the bank present — re-account (post-restart the
            # in-memory budget is empty though the dir persists) + bump LRU recency.
            # If a prior extraction cached the tune BANKLESS (the bank was
            # unreachable then), fall through and re-extract so the in-zip walk /
            # cross-zip fallback gets another chance instead of latching broken.
            _register_adlib_extract(track_id, out_dir)
            return music_out

        def _extract() -> Path | None:
            import shutil
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                music_bytes, _ = _read_from_zip_path(path_str)
            except Exception:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None
            music_out.write_bytes(music_bytes)
            # Drop EVERY matching companion beside the tune so adplay finds the
            # exact bank/patch name AdPlug derives (names vary per format/title).
            # ``comp_member`` is the FULL stored name (incl. dir + native
            # separators), so it reads back across DOS-backslash / LHA archives;
            # the on-disk filename is its clean basename.
            for comp_member in _adlib_companion_names(path_str, member_dir, globs):
                comp_vpath = "::".join(parts[:-1] + [comp_member])
                try:
                    comp_bytes, _ = _read_from_zip_path(comp_vpath)
                except Exception:
                    continue
                out_name = _os.path.basename(comp_member.replace("\\", "/"))
                (out_dir / out_name).write_bytes(comp_bytes)
            # Tune zipped WITHOUT its bank → pull a loose one from the share/FS.
            if bank_fallback is not None:
                try:
                    bank_fallback(out_dir, globs)
                except Exception:
                    log.debug("AdLib zip bank fallback failed", exc_info=True)
            marker.write_text(zip_mtime)
            return music_out

        music = await asyncio.get_running_loop().run_in_executor(None, _extract)
        if music is not None:
            _register_adlib_extract(track_id, out_dir)
            try:
                await asyncio.to_thread(_zip_evict_until_under_budget)
            except Exception:
                log.exception("AdLib companion-extract eviction failed")
        return music


def _uade_member_real_name(member_base: str) -> str | None:
    """The uade-detectable filename for an archive member, or None.

    The archive layer appends a routing extension to prefix-named members
    (``mdat.song`` is listed as ``mdat.song.mdat``) so the suffix-keyed
    pipeline recognises them — strip it back off for uade itself, whose
    detection needs the ORIGINAL Amiga name.

    ORDER MATTERS (QA C2, 2026-07-02): the strip check must run FIRST.
    ``classify("mdat.acieed1.mdat")`` matches via the *prefix* token, so a
    classify-first implementation never stripped — uade then derived the
    companion as ``smpl.acieed1.mdat`` (nonexistent) and every archived
    prefix-form TFMX/RJP module failed to play.  Strip when the last
    segment is itself a uade routing extension AND the stripped name still
    classifies.
    """
    if "." in member_base:
        stem, _, last = member_base.rpartition(".")
        if (f".{last.lower()}" in _UADE_EXTS
                and _uade_formats.classify(stem) is not None):
            return stem
    if _uade_formats.classify(member_base) is not None:
        return member_base
    return None


# ── Sonix Music Driver instrument-subdir handling ──────────────────────────
# Aegis Sonix (.smus/.snx) modules keep their samples in a sibling
# ``Instruments/`` SUBDIRECTORY, referenced by arbitrary names embedded in the
# module's INS1 chunks — NOT by the name-transform rule the TFMX/RJP companion
# logic encodes.  uade's SonixMusicDriver requests ``Instruments/<name>.instr``
# (and ``Instruments/<name>.ss`` synth-sound halves) at InitPlayer time, and a
# SINGLE missing instrument makes it abort fatally ("ExtLoad failed → score
# died" → our 502).  So we must (a) pull the whole sibling Instruments/ subdir
# next to the module and (b) synthesize a silent 8SVX stub for any instrument
# the module references but the rip omits.  ``SONIX_PLAYERS`` and
# ``sonix_instrument_names`` live in core.uade_formats — shared with the scanner
# so the play-time stub and the scan-time ``partial`` badge agree on "missing".


def _svx8_silence(nbytes: int = 32) -> bytes:
    """A minimal valid IFF 8SVX one-shot sample of ``nbytes`` silence.

    Used as a stand-in for a Sonix instrument the rip is missing so uade's
    SonixMusicDriver loads it (silent) instead of aborting the whole score.
    Verified: substituting this for a genuinely-absent instrument lets an
    otherwise-fatal .smus render full-length audio.
    """
    import struct
    body = b"\x00" * nbytes
    vhdr = struct.pack(">IIIHBBI", nbytes, 0, 0, 8000, 1, 0, 0x10000)

    def _chunk(cid: bytes, data: bytes) -> bytes:
        out = cid + struct.pack(">I", len(data)) + data
        return out + b"\x00" if (len(data) & 1) else out

    inner = b"8SVX" + _chunk(b"VHDR", vhdr) + _chunk(b"BODY", body)
    return b"FORM" + struct.pack(">I", len(inner)) + inner


def _extract_sonix_instruments(
    parts: list[str], member_dir: str, outer_zip: Path,
    out_dir: Path, music_out: Path,
) -> list[str]:
    """Populate ``out_dir/Instruments`` for a Sonix module extracted to
    ``music_out``: pull every sibling ``<member_dir>/Instruments/*`` member,
    then stub any instrument the module references but the archive lacks.
    Returns the list of instrument names that had to be stubbed (silent) — the
    caller backfills a ``partial`` defect from it.  Best-effort — a failure
    here just leaves the module to fail the render as before, never raises.
    """
    import os as _os
    from soniqboom.core import archive as _archive
    from soniqboom.core.scanner import _read_from_zip_path
    try:
        instr_dir_prefix = (
            f"{member_dir}/Instruments/" if member_dir else "Instruments/")
        instr_out = out_dir / "Instruments"
        instr_out.mkdir(parents=True, exist_ok=True)
        have: set[str] = set()
        for raw in _archive.raw_namelist(outer_zip):
            clean = raw.replace("\\", "/")
            low = clean.lower()
            if not low.startswith(instr_dir_prefix.lower()):
                continue
            base = _os.path.basename(clean)
            if not base:            # directory entry
                continue
            comp_vpath = "::".join(parts[:-1] + [raw])
            try:
                comp_bytes, _ = _read_from_zip_path(comp_vpath)
                (instr_out / base).write_bytes(comp_bytes)
                have.add(base.lower())
            except Exception:
                continue
        # Stub instruments the .smus references but the rip omits — one
        # missing file otherwise aborts the whole SonixMusicDriver score.
        try:
            names = _uade_formats.sonix_instrument_names(music_out.read_bytes())
        except Exception:
            names = []
        stubbed: list[str] = []
        for nm in names:
            # SECURITY: the INS1 name is module-controlled.  uade only ever
            # requests ``Instruments/<basename>.instr`` and legit Sonix names
            # are already bare, so reduce to a basename before building the
            # path — otherwise a crafted name like ``/etc/cron.d/x`` or
            # ``../../x`` would escape ``instr_out`` and write the silent stub
            # to an arbitrary location.  Mirrors the basename the sibling
            # extraction loop above already applies (``have`` is basename-keyed).
            safe = _os.path.basename(nm.replace("\\", "/")).strip()
            if not safe or safe in (".", ".."):
                continue
            fn = f"{safe}.instr"
            if fn.lower() not in have:
                try:
                    (instr_out / fn).write_bytes(_svx8_silence())
                    have.add(fn.lower())
                    stubbed.append(safe)
                except Exception:
                    continue
        return stubbed
    except Exception:
        log.warning("Sonix instrument extraction failed for %s",
                    music_out, exc_info=True)
        return []


async def _extract_uade_with_companions(
    path_str: str, track_id: str, outer_zip: Path, uade_name: str,
) -> Path | None:
    """Materialize a uade Amiga module + its companion halves from an archive.

    TFMX (``mdat.X`` + ``smpl.X``), Richard Joseph (``X.sng`` + ``X.ins``) and
    friends resolve their sample file by NAME in the module's own directory —
    so the flat per-track extraction (track-id filename, no siblings) can
    never play them.  Drops the module under its REAL Amiga name plus any
    same-body companion siblings into a per-track dir.  Companion matching is
    case-insensitive against the archive's raw member list (Amiga rips mix
    SMPL./smpl.).  Sonix modules additionally get their sibling
    ``Instruments/`` subdir (see ``_extract_sonix_instruments``).  Mirrors
    ``_extract_adlib_with_companions``.
    """
    import os as _os
    from soniqboom.core import archive as _archive
    from soniqboom.core.scanner import _read_from_zip_path
    parts = path_str.split("::")
    member = parts[-1].replace("\\", "/")
    member_dir = _os.path.dirname(member)
    out_dir = _zip_extract_dir() / f"{track_id}.uade"
    music_out = out_dir / uade_name
    try:
        zip_mtime = str(outer_zip.stat().st_mtime)
    except OSError:
        return None
    lock = await _zip_lock_for(track_id)
    async with lock:
        wanted = {s.lower() for s in
                  _uade_formats.companion_sibling_names(uade_name)}
        _player = (_uade_formats.classify(uade_name) or (None,))[0]
        _is_sonix = _player in _uade_formats.SONIX_PLAYERS
        marker = out_dir / ".zip_mtime"
        if (music_out.exists() and marker.exists()
                and marker.read_text() == zip_mtime
                # Sonix caches from before the Instruments/ fix have the
                # module but no samples — force a re-extract for those.
                and not (_is_sonix and not (out_dir / "Instruments").exists())):
            # QA m1: don't latch a companion-LESS extract forever.  If the
            # archive holds a wanted sibling that the cached dir lacks
            # (earlier partial read), fall through and re-extract.
            try:
                from soniqboom.core import archive as _arc
                _in_zip = {
                    _os.path.basename(r.replace("\\", "/")).lower()
                    for r in _arc.raw_namelist(outer_zip)
                    if _os.path.dirname(r.replace("\\", "/")) == member_dir
                }
                _have = {p.name.lower() for p in out_dir.glob("*")}
                _missing = (wanted & _in_zip) - _have
            except Exception:
                _missing = set()
            if not _missing:
                _register_adlib_extract(track_id, out_dir)  # same budget/LRU pool
                return music_out

        _sonix_stubbed: list[str] = []   # instruments silently substituted (Sonix)

        def _extract() -> Path | None:
            import shutil
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                music_bytes, _ = _read_from_zip_path(path_str)
            except Exception:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None
            music_out.write_bytes(music_bytes)
            # Case-insensitive same-dir sibling match against RAW member names
            # (companion halves are filtered out of the playable map, so the
            # display-name map never lists them).
            for raw in _archive.raw_namelist(outer_zip):
                clean = raw.replace("\\", "/")
                if _os.path.dirname(clean) != member_dir:
                    continue
                base = _os.path.basename(clean)
                if base.lower() in wanted:
                    comp_vpath = "::".join(parts[:-1] + [raw])
                    try:
                        comp_bytes, _ = _read_from_zip_path(comp_vpath)
                        (out_dir / base).write_bytes(comp_bytes)
                    except Exception:
                        continue
            # Sonix keeps its samples in a sibling Instruments/ subdir keyed
            # by arbitrary INS1 names (invisible to the companion-sibling
            # rule above) — pull the whole subdir + stub any missing halves.
            if _is_sonix:
                _sonix_stubbed[:] = _extract_sonix_instruments(
                    parts, member_dir, outer_zip, out_dir, music_out) or []
            marker.write_text(zip_mtime)
            return music_out

        music = await asyncio.get_running_loop().run_in_executor(None, _extract)
        if music is not None:
            _register_adlib_extract(track_id, out_dir)
            # Backfill a ``partial`` defect for the existing library — the scan
            # sets this for freshly-scanned Sonix modules, but tracks indexed
            # before the feature only learn they're degraded when first played.
            # Idempotent; only fires on a cold extract that actually stubbed.
            if _sonix_stubbed:
                try:
                    from soniqboom.core.store import get_store
                    _n = len(_sonix_stubbed)
                    _shown = ", ".join(_sonix_stubbed[:3]) + ("…" if _n > 3 else "")
                    get_store().update_track_fields(track_id, {
                        "defect": "partial",
                        "defect_detail": (
                            f"{_n} instrument{'s' if _n != 1 else ''} "
                            f"substituted (silent): {_shown}"),
                    })
                except Exception:
                    log.debug("Sonix defect backfill failed for %s", track_id,
                              exc_info=True)
            try:
                await asyncio.to_thread(_zip_evict_until_under_budget)
            except Exception:
                log.exception("UADE companion-extract eviction failed")
        return music


async def _get_or_extract_zip_member(path_str: str, track_id: str, bank_fallback=None) -> Path | None:
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
    # uade Amiga modules may need companion halves + their REAL name — route
    # them to the dedicated materializer (before the flat path renames them).
    _member_base = Path(parts[-1].replace("\\", "/")).name
    _uade_real = _uade_member_real_name(_member_base)
    if _uade_real is not None:
        return await _extract_uade_with_companions(
            path_str, track_id, outer_zip, _uade_real,
        )
    # Some AdLib formats (Sierra .sci, ROL .bnk, KSM insts.dat) need a companion
    # instrument-bank/patch file in the same dir, which the flat per-track
    # extraction below can't provide — route them to the dedicated materializer.
    _adlib_ext = Path(parts[-1]).suffix.lower()
    if _adlib_ext in _ADLIB_COMPANION_GLOBS:
        return await _extract_adlib_with_companions(
            path_str, track_id, outer_zip, _ADLIB_COMPANION_GLOBS[_adlib_ext],
            bank_fallback=bank_fallback,
        )
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
            with _zip_state_lock:
                _ZIP_EXTRACT_TOTAL_BYTES = max(
                    0, _ZIP_EXTRACT_TOTAL_BYTES - entry.get("size", 0),
                )
                _ZIP_EXTRACT_CACHE.pop(track_id, None)
            try: cached_path.unlink()        # I/O outside the lock
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
        with _zip_state_lock:
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


def _compose_backgrounds(*tasks) -> BackgroundTask | None:
    """Combine several Starlette ``BackgroundTask`` objects (None-safe) into one
    runnable that AWAITS each.

    Needed because ``BackgroundTask.__call__`` is a coroutine — calling it
    synchronously (e.g. the old ``prior_task()``) only creates an un-awaited
    coroutine, so the wrapped func never runs.  Returns the single task when
    there's only one, a wrapper ``BackgroundTask`` that awaits each (isolated, so
    one failing doesn't strand the rest) for several, or ``None`` when empty.
    """
    present = [t for t in tasks if t is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]

    # Run each task isolated: starlette's BackgroundTasks has NO per-task
    # try/except, so a raise in one would strand the rest (e.g. the inflight
    # unpin failing would skip the zip-pin cleanup).  Awaiting each under its own
    # guard makes exactly-once cleanup independent of order or raise-safety.
    async def _run_all():
        for t in present:
            try:
                await t()
            except Exception:
                log.debug("composed background task failed", exc_info=True)

    return BackgroundTask(_run_all)


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
        # Compose the inflight-cache unpin AND the caller's zip-extract cleanup
        # (``prior_task``, e.g. the zip-pin _bg) so BOTH run, awaited, on response
        # close.  The old ``prior_task()`` ran a BackgroundTask synchronously,
        # which only made an un-awaited coroutine — so the zip pin leaked.
        return _compose_backgrounds(BackgroundTask(_do_unpin), prior_task)

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
            background_task=background_task,
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
        background_task=background_task,
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
    background_task=None,
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
        # The inflight-cache unpin runs in _stream_pcm's finally (_release_pin_once);
        # this releases the caller's zip-extract pin when the response closes.
        background=background_task,
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
    background_task=None,
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
        # Run BOTH the inflight-cache unpin and the caller's zip-extract cleanup
        # (background_task) when the response closes — each awaited + isolated.
        background=_compose_backgrounds(bg, background_task),
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
        is_cache_ready, _cache_key, find_shorter_sid_entry, sid_warm_eligible,
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
    target_dur = max(5, min(int(target_dur), 3600))  # clamp: HVSC/meta values are semi-trusted (QA residual #1)

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
        "warm_eligible": sid_warm_eligible(),
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
    """Background-render one track under the low-priority prewarm gate.

    ``_bg_render_sem`` caps ALL background renders (this prewarm + the AdLib
    probe batch) at ``_RENDER_SLOTS - 1`` in aggregate and is held across the
    inner render (which itself acquires ``_render_sem``), so a speculative
    render never starves the foreground stream path of its reserved slot.  A
    FIFO-cap cancellation that fires while we're still waiting on the gate
    raises ``CancelledError`` straight out of the ``async with`` — the inner
    render never starts."""
    async with _bg_render_sem:
        await _do_prewarm_render(track_id, file_path, ext, subsong)


async def _do_prewarm_render(
    track_id: str, file_path: Path, ext: str, subsong: int,
) -> None:
    """Run the format-appropriate cached render for one track in the
    background.  Mirrors the routing in ``stream_track`` so the cache key
    matches exactly what playback will request later."""
    from soniqboom.core.conversion_cache import get_or_render
    try:
        if ext in _SID_EXTS and _is_c64_sid(file_path):
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
            target_dur = max(5, min(int(target_dur), 3600))  # clamp: HVSC/meta values are semi-trusted (QA residual #1)
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
        elif ext in _PSF_STREAM_EXTS or (
                ext == ".dsf" and _dsf_is_dreamcast(file_path)):
            await get_or_render(
                track_id=track_id, format_type="psf", subsong=0,
                render_fn=lambda: _render_psf(file_path),
            )
        elif ext in _SNDH_EXTS:
            await get_or_render(
                track_id=track_id, format_type="sndh", subsong=subsong,
                render_fn=lambda: _render_sndh(file_path, subsong=subsong),
            )
        elif ext in _YM_EXTS:
            await get_or_render(
                track_id=track_id, format_type="ym", subsong=0,
                render_fn=lambda: _render_ym(file_path),
            )
        elif ext in _SC68_EXTS:
            await get_or_render(
                track_id=track_id, format_type="sc68", subsong=subsong,
                render_fn=lambda: _render_sc68(file_path, subsong=subsong),
            )
        elif (ext not in _ADLIB_EXTS and ext != ".imf"
                and (ext in _UADE_EXTS or ext in _SID_EXTS
                     or _uade_formats.classify(file_path.name) is not None)):
            # uade family: suffix tokens, Amiga prefix-form names, and
            # magic-less .sid (SidMon — real C64 PSID returned above).  AdLib
            # extensions are excluded so an AMUSIC ``star.amd`` (uade ``star``
            # prefix collision) prewarms via AdPlug, matching playback.
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


@router.post("/probe-durations")
async def probe_durations(
    payload: dict = Body(...),
    sb_session: str | None = Cookie(default=None),
    request: Request = None,
):
    """Fill in real lengths for render-only tracks shown in the UI that still
    carry their default-duration placeholder — AdLib/id-IMF (180s) and GME
    chiptunes (NSF/SPC/GBS/VGM/… at sid_default_duration) — WITHOUT the user
    having to play them.

    The library calls this in the background for the placeholder rows currently
    on screen (any view — folder, search, smart, galaxy).  Each tune is rendered
    once (throwaway) to learn its length, which is persisted; the real seconds
    are returned as ``{track_id: seconds}`` so the client patches the rows in
    place.  Concurrency-limited so a big result set can't spike CPU, and naturally
    one-time (a probed/played track is no longer a placeholder, so it's skipped).
    """
    _require_stream_auth(request, sb_session, None, None)
    ids = payload.get("track_ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        return {}
    ids = [str(x) for x in ids][:200]          # cap the batch
    # Share the single low-priority background gate with web prewarm so probes
    # + prewarms in AGGREGATE never occupy the render slot reserved for a live
    # play (``_bg_render_sem`` = _RENDER_SLOTS-1; foreground never acquires it).
    async def _one(tid: str):
        async with _bg_render_sem:
            try:
                return tid, await _probe_one_rendered_duration(tid)
            except Exception:
                return tid, None

    out = await asyncio.gather(*[_one(t) for t in ids])
    return {tid: dur for tid, dur in out if dur and dur > 0}


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
        path = await _get_or_extract_zip_member(
            f"{_local_zip}::{_member}", track_id,
            bank_fallback=_make_zip_bank_fallback(remote=(zip_rel, source)),
        )
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
        path = await _get_or_extract_zip_member(
            path_str, track_id,
            bank_fallback=_make_zip_bank_fallback(local_zip=path_str.split("::")[0]),
        )
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

    # Amiga prefix-form names (mdat.song) carry no token extension — detect by
    # name so they route to uade below.  ``_render_ident`` also keeps AdLib
    # (AMUSIC ``star.amd`` etc.) out of the uade path — see its docstring.
    ext, _uade_named = _render_ident(path_str)

    # Loose (non-zip) uade modules on a remote share: materialize module +
    # companion halves (TFMX smpl.X …) into one dir, mirroring AdLib below.
    if (path_str.startswith(("smb://", "ftp://")) and "::" not in path_str
            and (_uade_named or ext in _UADE_EXTS) and ext != ".ahx"):
        try:
            from soniqboom.core.filesource import get_source, parse_remote_path
            _sr, _rp = parse_remote_path(path_str)
            _src = get_source(_sr)
            if _src is not None:
                _mat = await _materialize_loose_remote_uade(track_id, _sr, _rp, _src)
                if _mat is not None:
                    path = _mat
                    _zip_pin(track_id)
                    _zip_track_id_for_unpin = track_id
        except Exception as exc:
            log.info("Loose UADE companion materialize failed for %s: %s", path_str, exc)

    # Loose (non-zip) AdLib tunes on a remote share need their companion bank
    # materialized in the same dir; the per-file fetch above split them apart.
    # Re-point ``path`` to a dir holding tune + bank (no-op if no bank sibling).
    if (path_str.startswith(("smb://", "ftp://")) and "::" not in path_str
            and ext in _ADLIB_COMPANION_GLOBS):
        try:
            from soniqboom.core.filesource import get_source, parse_remote_path
            _sr, _rp = parse_remote_path(path_str)
            _src = get_source(_sr)
            if _src is not None:
                _mat = await _materialize_loose_remote_adlib(
                    track_id, _sr, _rp, _src, _ADLIB_COMPANION_GLOBS[ext],
                )
                if _mat is not None:
                    path = _mat
                    # Pin so a concurrent clear/eviction can't rmtree the bank
                    # mid-render (mirrors the zip-member play paths).
                    _zip_pin(track_id)
                    _zip_track_id_for_unpin = track_id
        except Exception as exc:
            log.info("Loose AdLib companion materialize failed for %s: %s", path_str, exc)

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

    # Release the pin/temp on ANY failure path: if a renderer raises
    # (422 missing-bank, 501/502/504, or asyncio.CancelledError) before a
    # response carrying background=_bg is built, _bg never runs — so without
    # this the zip-member / .adlib extract pin would leak permanently and the
    # extract could never evict or be cleared.  The success path is untouched:
    # each branch returns a response with background=_bg, so the pin is held
    # for the whole stream and unpinned by _bg AFTER it finishes (never early).
    try:

        # ── Rendered formats: SID / MIDI / Tracker ───────────────────────────────
        # These are cached as WAV files so repeat playback is instant.
        # On cache miss, the renderer runs and the result is stored for next time.
        from soniqboom.core.conversion_cache import get_or_render

        if ext in _SID_EXTS and _is_c64_sid(path):
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
            target_dur = max(5, min(int(target_dur), 3600))  # clamp: HVSC/meta values are semi-trusted (QA residual #1)

            full_key = _cache_key(track_id, "sid", subsong, duration=target_dur)

            # 1) Exact cache hit (correct duration)
            exact = await get_cached(full_key)
            if exact:
                # Ensure the retro per-voice VU meter exists (background, dedup'd)
                # — covers SIDs cached before this feature and cache-hit replays,
                # which never reach the render-path triggers below.
                _spawn_sid_vu(full_key, exact, path, subsong, target_dur)
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

            # 3) No cache at all.  For the LOCAL web UI, stream sidplayfp's
            # still-rendering WAV so playback starts in ~0.2 s instead of blocking
            # on the full render (~tune_len/15).  ``_serve_sid_progressive`` now
            # answers Range/seek requests with a proper 206 against the known
            # full length, so ANY web-session GET (bare, probe, or seek) can take
            # it — Subsonic/DLNA/Cast still fall through to the blocking path.
            # The gate:
            #   • GET only — a HEAD 405s at the router; never spawns a render.
            #   • a session cookie AND no Subsonic auth params (u/s/t) — a
            #     Subsonic/DLNA client carrying a stray cookie stays blocking.
            #   • not the anonymous cast byte-server path.
            # ``_serve_sid_progressive`` returns None (→ fall through to blocking)
            # when over the concurrency cap or on an immediate render failure (so
            # the error surfaces as a real 5xx, not silent silence).
            # Retro per-voice VU meter — spawned HERE, before the progressive
            # branch below can return.  Two reasons this must not sit further
            # down (where it used to, after ``get_or_render``):
            #   • the progressive path returns early, so a normal WEB play never
            #     reached it at all — the meter was generated only for the
            #     blocking (Subsonic/DLNA/cast) callers;
            #   • the VU pass renders from the SOURCE .sid and writes to the
            #     cache key's deterministic ``.vu`` path, so it does NOT depend
            #     on the audio ever being cached.  That matters because a
            #     listener who skips away mid-tune leaves NOTHING cached (the
            #     progressive finaliser discards the temp unless the client
            #     consumed every byte) — under the old placement the sidecar
            #     could then never be generated, on this play or any later one.
            # Idempotent + dedup'd on the cache key, so calling it on every play
            # (warm or cold) is free once the sidecar exists.
            ensure_sid_vu_sidecar(track_id, path, subsong, target_dur)

            _web_session = (request.method == "GET"
                            and bool(sb_session)
                            and not (u or s or t)
                            and not _cast_internal_bypass_ctx.get())
            if _web_session:
                _resp = await _serve_sid_progressive(
                    request, path, subsong, target_dur, full_key,
                    base_headers={"X-Rendered": "sidplayfp", "X-Cache": "miss-progressive",
                                  "X-SID-Target-Seconds": str(target_dur)},
                    background=_bg,
                )
                if _resp is not None:
                    return _resp
                # else: over cap or immediate render failure — fall through to
                # the blocking path.

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
        # UADE / HVL go BEFORE the tracker branch — .ahx and .hvl appear in
        # the *scanner's* tracker set (metadata.py) for library detection, but
        # openmpt123 silently doesn't decode them.  (This file's own
        # _TRACKER_EXTS deliberately excludes both.)  Without this priority a
        # .ahx/.hvl play would 501 from inside _render_tracker.
        if ext in _HVL_EXTS:
            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="hvl", subsong=subsong,
                render_fn=lambda: _render_hvl(path, subsong=subsong),
            )
            # .hvl is in the scanner's tracker set for detection, but openmpt123
            # can't decode it, so the scan stored duration 0 — hvl2wav renders to the
            # tune's natural end, so persist the WAV's real length (placeholder=0
            # no-ops if a real duration was somehow already stored).
            await _backfill_rendered_duration(track_id, track, cached_path, 0.0)
            return await _range_file_response(
                request, cached_path, media_type="audio/wav",
                headers={"X-Rendered": "hvl2wav", "X-Cache": "hit" if hit else "miss"},
                background=_bg,
            )
        if ext in _PSF_STREAM_EXTS or (ext == ".dsf" and _dsf_is_dreamcast(path)):
            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="psf", subsong=0,
                render_fn=lambda: _render_psf(path),
            )
            # Duration normally comes from the length/fade tags at scan; rips
            # without tags stored 0 — persist the rendered WAV's real length.
            await _backfill_rendered_duration(track_id, track, cached_path, 0.0)
            return await _range_file_response(
                request, cached_path, media_type="audio/wav",
                headers={"X-Rendered": "zxtune", "X-Cache": "hit" if hit else "miss"},
                background=_bg,
            )
        if ext in _SNDH_EXTS:
            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="sndh", subsong=subsong,
                render_fn=lambda: _render_sndh(path, subsong=subsong),
            )
            # SNDH TIME tags are frequently absent; the scan stored the Atari
            # default cap in that case — backfill only refines a 0 duration.
            await _backfill_rendered_duration(track_id, track, cached_path, 0.0)
            return await _range_file_response(
                request, cached_path, media_type="audio/wav",
                headers={"X-Rendered": "psgplay", "X-Cache": "hit" if hit else "miss"},
                background=_bg,
            )
        if ext in _YM_EXTS:
            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="ym", subsong=0,
                render_fn=lambda: _render_ym(path),
            )
            return await _range_file_response(
                request, cached_path, media_type="audio/wav",
                headers={"X-Rendered": "stsound", "X-Cache": "hit" if hit else "miss"},
                background=_bg,
            )
        if ext in _SC68_EXTS:
            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="sc68", subsong=subsong,
                render_fn=lambda: _render_sc68(path, subsong=subsong),
            )
            # sc68 durations are embedded and honoured by the renderer; the
            # scan stored 0 — persist the WAV's real length.
            await _backfill_rendered_duration(track_id, track, cached_path, 0.0)
            return await _range_file_response(
                request, cached_path, media_type="audio/wav",
                headers={"X-Rendered": "sc68", "X-Cache": "hit" if hit else "miss"},
                background=_bg,
            )
        if ext in _UADE_EXTS or _uade_named or ext in _SID_EXTS:
            # Everything uade renders: .ahx, ~350 suffix tokens (song.fc13),
            # Amiga prefix-form names (mdat.song), and magic-less .sid files
            # (Amiga SidMon — real C64 PSID/RSID returned above already).
            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="uade", subsong=subsong,
                render_fn=lambda: _render_uade(path, subsong=subsong),
            )
            # uade formats are render-only: the scan stored duration 0 — uade123
            # renders to the tune's natural end, so persist the WAV's real length
            # (placeholder=0 no-ops if a real duration was somehow already stored).
            await _backfill_rendered_duration(track_id, track, cached_path, 0.0)
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
            # id/Apogee AdLib IMF stores the same 180s placeholder and renders to
            # natural end via _render_adlib, so backfill its real length too.  The
            # placeholder gate no-ops for IM10 (Imago Orpheus) .imf, which already
            # carries a real tracker duration.
            await _backfill_rendered_duration(track_id, track, cached_path)
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
            # The scanner stored a 180s placeholder; the WAV we just made/cached
            # carries the real length — persist it so the list stops showing "3:00".
            await _backfill_rendered_duration(track_id, track, cached_path)
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
            # Tracker length normally comes from openmpt123 --info at scan, but
            # that falls back to 0 when the binary is unavailable — backfill from
            # the rendered WAV in that case (placeholder=0 no-ops when scan already
            # stored a real duration).
            await _backfill_rendered_duration(track_id, track, cached_path, 0.0)
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
            # GME chiptunes (NSF/SPC/GBS/…) store the sid_default_duration
            # placeholder; libgme renders to the track's natural end so the WAV
            # carries the real length — persist it so the list/modal stop showing
            # the default (e.g. "5:00").
            await _backfill_rendered_duration(
                track_id, track, cached_path, float(settings.sid_default_duration))
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
        # Ogg/Opus: some clients (Safari < 18.4) can't decode it — route those to
        # the transcoder (→ WAV, which every browser plays) instead of a native
        # .ogg/.opus that fails silently.  Prefer the client's DECLARED capability
        # (sb_caps), keyed by extension — ``.opus`` is Opus; ``.ogg`` is usually
        # Vorbis but may be Opus, so accept EITHER (uses the vorbis cap too).
        # Fall back to the Safari-version UA heuristic when nothing was declared.
        if ext == ".opus":
            _ogg_sup = _client_supports("opus", request)
        elif ext == ".ogg":
            _o = _client_supports("opus", request)
            _v = _client_supports("vorbis", request)
            _ogg_sup = None if (_o is None and _v is None) else (bool(_o) or bool(_v))
        else:
            _ogg_sup = None
        _old_safari_ogg = ext in (".ogg", ".opus") and (
            (_ogg_sup is False) if _ogg_sup is not None else _safari_lacks_ogg(request))
        if (ext in NATIVE and not force_transcode and not _format_mismatch
                and not _old_safari_ogg):
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
            # AAC plays natively almost everywhere → direct-serve, UNLESS the
            # client explicitly declared it can't (a rare codec-stripped
            # Chromium / Linux-Firefox-without-an-OS-AAC-decoder) via sb_caps.
            if (detected_codec == "aac" and not _aac_mismatch
                    and _client_supports("aac", request) is not False):
                return await _range_file_response(
                    request, path, media_type="audio/mp4",
                    background=_bg,
                )
            # ALAC (Apple Lossless): direct-serve to any client that can decode
            # it — the client's DECLARED capability (sb_caps) when present, else
            # the Safari UA heuristic.  Chrome/Firefox fail silently on ALAC, so
            # they fall through to the FLAC/WAV transcode.  (Transcoding to raw
            # audio/flac would itself break Safari, which is why ALAC-to-Safari
            # must stay direct.)
            _alac_sup = _client_supports("alac", request)
            _alac_ok = _alac_sup if _alac_sup is not None else _is_safari(request)
            if detected_codec == "alac" and _alac_ok and not _aac_mismatch:
                return await _range_file_response(
                    request, path, media_type="audio/mp4",
                    background=_bg,
                )
            # ALAC on a client that can't decode it, or unknown → transcode

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
    except BaseException:
        _cleanup_tmp()
        raise
