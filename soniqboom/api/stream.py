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
import contextlib
import contextvars
import functools
import heapq
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid as _uuid
import zipfile
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Body, Cookie, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask


# ── Internal auth-bypass context ─────────────────────────────────────────────
# Set to True by ``cast_stream.cast_stream`` AFTER it has verified the
# signed token in the URL path, and by the Subsonic byte routes
# (``subsonic._stream_preauthed``) after their own auth — which spares a
# ``p=`` client a second scrypt per request.  ``stream_track`` reads this,
# skips its own _require_stream_auth and serves a still-rendering file only
# when its length is exact up front (SID, SNDH — never the web player's
# provisional or stored-length ones; another codec asked for is encoded from
# any growing render).  Critically, this CANNOT be set by any
# external request — FastAPI does NOT bind module-level ContextVars to
# query / header / body / cookie inputs, so the previous "bool kwarg"
# approach (which FastAPI happily exposed as a query parameter, opening
# a trivial anonymous-stream bypass) is replaced.
_cast_internal_bypass_ctx: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "soniqboom_cast_internal_bypass_auth", default=False,
)


# Subsonic ``estimateContentLength=true`` for a live transcode: None (the
# default) = no estimate, answer chunked; otherwise the source's length in
# seconds (0 = unknown — a rendered WAV's own header then says).  Set only by
# in-process callers (``estimated_content_length``), never from a query.
_estimate_len_ctx: "contextvars.ContextVar[float | None]" = contextvars.ContextVar(
    "soniqboom_estimate_content_length", default=None,
)


@contextlib.contextmanager
def estimated_content_length(duration_s: float | None):
    """Around an in-process ``stream_track`` call (the /rest stream route) for
    a client that sent ``estimateContentLength=true``: a live MP3 transcode is
    then answered with an estimated ``Content-Length`` (and exactly that many
    bytes) instead of chunked.  ``duration_s`` is the source's length in
    seconds (a subsong's own when known); 0/None = unknown."""
    tok = _estimate_len_ctx.set(max(0.0, float(duration_s or 0.0)))
    try:
        yield
    finally:
        _estimate_len_ctx.reset(tok)


def _set_cast_internal_bypass(value: bool):
    """Used ONLY by in-process callers that authenticated the request
    themselves (cast_stream.py, subsonic.py) — set the bypass flag in the
    current Task's context.  Returns the token so the caller can reset it."""
    return _cast_internal_bypass_ctx.set(bool(value))


def _reset_cast_internal_bypass(token) -> None:
    try:
        _cast_internal_bypass_ctx.reset(token)
    except (LookupError, ValueError):
        pass


from soniqboom.config import settings
from soniqboom.core import forksafe
from soniqboom.core.conversion_cache import _cache_key as _ck
from soniqboom.core.silence import AudibilityMeter as _AudibilityMeter
from soniqboom.core.data import get_track
from soniqboom.core import remote_zip as _remote_zip
from soniqboom.core.filesource import is_remote_path

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
from soniqboom.core.metadata import SID_EXTS as _SID_EXTS   # .sid / .psid / .rsid
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
# The Cast lookahead prewarm (core/cast_session.py ``_start_gated_render``)
# takes this gate too — before its render registers as in flight, so a
# foreground play of that track never waits on a render still queued here.
#
# NOTE: a prewarm or probe cancelled while its render runs (FIFO cap,
# /prewarm/retain, a client leaving) keeps its slot here until that render ends
# (``_hold_until_done``): the render — detached by ``get_or_render``, or the
# shared DSD/transcode pump — goes on to fill the cache anyway, and releasing
# the slot early let background work take every render slot.  One still
# queued for a slot is cancelled at once.
class _PriorityGate:
    """A counting semaphore that hands a freed slot to the most URGENT waiter
    (lowest ``priority``; FIFO within one priority) instead of the oldest.

    Background renders are not equally useful: the track that plays NEXT must
    not queue behind a folder's duration probes or a VU-meter pass.  ``async
    with gate:`` takes the default priority; ``async with gate.slot(p):``
    picks one."""

    def __init__(self, slots: int) -> None:
        self._free = slots
        self._waiters: list = []          # heap of (priority, seq, future)
        self._seq = 0

    async def acquire(self, priority: int = 1) -> None:
        if self._free > 0 and not any(not f.done() for _, _, f in self._waiters):
            self._free -= 1
            return
        fut = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._waiters, (priority, self._seq, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()            # granted just as we were cancelled — pass it on
            else:
                fut.cancel()              # lazily skipped by release()
            raise

    def release(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(True)
                return
        self._free += 1

    def waiting(self, max_priority: int | None = None) -> int:
        """Waiters at or above ``max_priority`` urgency (all when None)."""
        return sum(1 for p, _, f in self._waiters
                   if not f.done() and (max_priority is None or p <= max_priority))

    def slot(self, priority: int = 1):
        gate = self

        class _Slot:
            async def __aenter__(self_inner):
                await gate.acquire(priority)

            async def __aexit__(self_inner, *exc):
                gate.release()
        return _Slot()

    async def __aenter__(self):
        await self.acquire(1)

    async def __aexit__(self, *exc):
        self.release()


# Background priorities (lower = sooner).
PRIO_NEXT = 0      # the track that plays next (queue N+1)
PRIO_AHEAD = 1     # further lookahead (N+2 / N+3) and hover-to-click
PRIO_PROBE = 2     # duration probes for rows on screen
PRIO_VU = 3        # per-voice VU sidecars (cosmetic)

_bg_render_sem = _PriorityGate(max(1, _RENDER_SLOTS - 1))


async def _hold_until_done(aw):
    """Await ``aw`` (a coroutine or future) so that cancelling the caller
    neither stops it nor returns before it has ended: the caller's
    ``CancelledError`` is re-raised only once ``aw`` is done.

    For background work under a ``_bg_render_sem`` slot whose render goes on
    after a cancel anyway (a render detached by ``get_or_render``, a shared
    pump, a download in a worker thread): the slot — or a per-share download
    turn — is then held for exactly as long as that work really runs."""
    inner = asyncio.ensure_future(aw)
    inner.add_done_callback(lambda t: t.cancelled() or t.exception())
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await asyncio.wait({inner})
        raise


def _renderer_failure(kind: str, binary: str, returncode, err_text: str, *,
                      module_path: "Path | None" = None) -> HTTPException:
    """Map a renderer's non-zero exit + stderr to the HTTP error the player shows.

    uade prints "module check failed" to stderr ONLY when the input isn't a
    real Amiga module — e.g. a PC ``.dat`` misindexed as PaulRobotham by
    extension.  Surface that as a clear 422 the frontend shows in the toast,
    instead of a cryptic "uade renderer exited with status 1" 502.  Do NOT
    match the generic "Can not play <name>" line — uade prints it for a legit
    module whose companion sample half is missing too ("score died"), which
    would be mislabelled as PC data.

    ``module_path`` (uade only; the file uade was given): when the module
    "died" and none of its companion halves (``smpl.X`` / ``smp.X`` …) is
    beside it, say that the sample file is probably missing (422) — checked
    only on this failure path (one directory listing).
    """
    low = (err_text or "").lower()
    if kind == "uade" and "please depack" in low:
        # uade names the packer ("The file is SQSH packed") — a packed file
        # the server couldn't unpack before handing it over.
        return HTTPException(
            422,
            "This Amiga module is packed with a cruncher that can't be "
            "unpacked here — unpack it first (e.g. with amigadepacker).",
        )
    if kind == "uade" and "module check failed" in low:
        return HTTPException(
            422,
            "This file isn't a playable Amiga module — it looks "
            "like non-module data (e.g. a PC/DOS file) indexed by "
            "mistake.",
        )
    if (kind == "uade" and module_path is not None
            and ("score died" in low or "can not play" in low)):
        missing = _missing_companion_names(Path(module_path))
        if missing is not None:
            log.info("uade render of %s failed without its companion file "
                     "(exit %s): %s", Path(module_path).name, returncode,
                     (err_text or "").strip()[:300])
            eg = f" (e.g. {' / '.join(missing)})" if missing else ""
            return HTTPException(
                422,
                "This Amiga module couldn't be played — it probably needs a "
                f"companion sample file{eg} that isn't next to it.",
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
            kind, binary, returncode, (err_text or "").strip()[:300])
        return HTTPException(
            501,
            f"The {kind} renderer is installed but can't run — a shared "
            f"library was upgraded out from under it. Reinstall or "
            f"rebuild the renderer to fix it.",
        )
    if (err_text or "").strip():
        log.warning(
            "%s renderer failed (exit %s): %s", kind,
            returncode, err_text.strip()[:500])
    return HTTPException(502, f"{kind} renderer exited with status {returncode}")


def _missing_companion_names(module_path: Path) -> "list[str] | None":
    """For a module whose player needs a separate sample half (TFMX
    ``mdat.X`` + ``smpl.X``, RJP, UFO, …) when NO companion candidate — nor
    a shared ``smp.set`` / ``mdtest.ssd`` bank — is in its folder: the usual
    names of that half (possibly empty).  None otherwise (a single-file
    player, or a companion is there).  One directory listing; an unreadable
    folder counts as empty."""
    name = module_path.name
    expected = _uade_formats.expected_companions(name)
    if expected is None:
        return None
    try:
        here = {n.lower() for n in os.listdir(module_path.parent)}
    except OSError:
        here = set()
    if any(c.lower() in here for c in _uade_formats.companion_sibling_names(name)):
        return None
    return list(expected)


def _render_has_audio(path: Path) -> bool:
    """Does a renderer's output file hold any audio: for a RIFF/WAVE file, a
    byte past its ``data`` chunk header (a renderer that failed quietly can
    leave a header with no frames — openmpt's is 88 bytes); for anything else
    (RF64, FLAC, MP3 …) only a size check, more than 44 bytes — a header-only
    file of a larger format passes.  A missing file holds none."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return size > _WAV_HEADER_LEN
    pos = 12
    while pos + 8 <= len(head):
        chunk, length = head[pos:pos + 4], int.from_bytes(head[pos + 4:pos + 8], "little")
        if chunk == b"data":
            return size > pos + 8
        pos += 8 + length + (length & 1)
    return size > len(head)               # a data chunk past 4 KB of header


async def _await_renderer(
    cmd: list[str], tmp_path: Path, *, timeout: float, kind: str,
    require_audio: bool = True,
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

    An exit 0 with no audio in ``tmp_path`` (``_render_has_audio``) is a 422
    too — unless ``require_audio`` is False, for a caller that inspects an
    empty result itself (AdLib tells a missing bank from a corrupt file).
    """
    async with _render_sem:
        # Capture stderr (was DEVNULL): a renderer's own diagnostics are the
        # only way to tell "the file isn't a valid module" (a clear 4xx the
        # listener can act on) apart from "the renderer/infra broke" (a 502).
        # ``communicate`` drains the pipe concurrently so a chatty renderer
        # (ffmpeg) can't deadlock on a full 64K stderr buffer.
        proc = await forksafe.spawn(
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
                raise _renderer_failure(kind, cmd[0], proc.returncode, err_text)
            # Exit 0 is not proof of audio: zxtune exits 0 without writing
            # anything when a PSF's library is missing.  No output (or a bare
            # WAV header) is a failure the listener is told about, not a
            # missing temp file the cache trips over later.
            if require_audio and not _render_has_audio(Path(tmp_path)):
                Path(tmp_path).unlink(missing_ok=True)
                err_text = (stderr_data or b"").decode("utf-8", "replace").strip()
                log.warning("%s renderer exited 0 without audio for %s: %s",
                            kind, cmd[-1], err_text[:300])
                raise HTTPException(
                    422, "The renderer finished but produced no audio for this file.")
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
# Set once a progressive render's result is settled (cached, or discarded):
# lets the waveform request ATTACH to a progressive render and /render-status
# report it, exactly like a blocking render's in-flight event.  Keyed by
# full_key; the entry is dropped by its finaliser.
_SID_PROG_DONE: dict[str, asyncio.Event] = {}
# Progressive SID renders in progress, by full_key: the render's shared state
# (temp, process, verdict, readers…).  A second GET for the same tune — a DLNA
# renderer's second connection, a ``Range: bytes=N-`` reconnect after a seek,
# a cast receiver while the web UI plays it — attaches to it as another reader
# instead of starting another sidplayfp (or killing an 80 %-done render to
# start again).  Dropped by the render's finaliser.
_SID_PROG_RENDERS: dict[str, dict] = {}
# SID prewarms rendering right now: full_key → the render task
# (``get_or_render``'s detached one).  A play of that tune retires it before
# its own progressive render starts (``_retire_sid_prewarm``), so a click
# during a prewarm never runs a second sidplayfp beside it.
_SID_PREWARM_RENDERS: dict[str, asyncio.Task] = {}


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


# How long a progressive SID for our web UI waits for sidplayfp's first PCM
# bytes, and then for its first AUDIBLE ones, before it streams anyway
# (``_await_sid_audible``) — every other caller waits for sound or the end of
# the render: a render that dies first falls back to the blocking render (so
# an immediate failure surfaces as a real error), one that ends silent is the
# cache's 422.
_SID_PROG_FIRST_BYTE_TIMEOUT = 3.0
_SID_PROG_AUDIBLE_WAIT = 20.0


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


# ── Multi-tune files: wire index → tune ─────────────────────────────────────
# ``?subsong=`` carries the WIRE index, shared with the web player (utils.js
# ``subsongWireToTune``, the WASM worker) and the Subsonic tune ids
# (``subsonic_index.wire_tune``): wire 0 is the file's DEFAULT tune — its
# header's start song ``s`` (1-based) — and, when that isn't tune 1, wire
# ``s-1`` is tune 1; every other wire ``w`` is tune ``w+1``.  A start song
# outside 1..count counts as 1.  So every tune has exactly one wire, and the
# bare id plays what the file plays by default.

def sid_wire_tune(wire: int, start: int = 1, count: int = 0) -> int:
    """The 1-based tune wire ``wire`` selects in a file whose default tune is
    ``start`` (1-based) of ``count`` (0 = unknown) — see above."""
    try:
        s = int(start)
    except (TypeError, ValueError):
        s = 1
    if not (s >= 1 and (int(count or 0) <= 0 or s <= int(count))):
        s = 1
    w = max(0, int(wire or 0))
    if w == 0:
        return s
    if s != 1 and w == s - 1:
        return 1
    return w + 1


def _psid_count_start(path) -> tuple[int, int]:
    """(song count, 1-based start song) from a PSID/RSID header; ``(0, 1)``
    when the file is unreadable or not a C64 SID.  Blocking (18 bytes)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x12)
    except OSError:
        return 0, 1
    if len(head) < 0x12 or head[:4] not in (b"PSID", b"RSID"):
        return 0, 1
    count = int.from_bytes(head[0x0E:0x10], "big")
    start = int.from_bytes(head[0x10:0x12], "big")
    return count, (start if 1 <= start and (count <= 0 or start <= count) else 1)


async def _psid_start_song(path) -> int:
    """The 1-based start song of the local SID ``path`` (1 when unknown), read
    in a worker thread."""
    try:
        _count, start = await asyncio.wait_for(
            asyncio.to_thread(_psid_count_start, path), timeout=5.0)
    except Exception:
        return 1
    return start


# Track id → the 1-based start song read from that SID file's header, for the
# SID paths that only have the track (the render length / cache key must use
# the same tune as the renderer).  Filled wherever the file itself is at hand
# (a play, a prewarm, the track-info request); bounded.
_SID_START_SONG: "OrderedDict[str, int]" = OrderedDict()
_SID_START_SONG_MAX = 8192


def _note_sid_start_song(track_id: str, start: int) -> None:
    _SID_START_SONG[track_id] = int(start)
    _SID_START_SONG.move_to_end(track_id)
    while len(_SID_START_SONG) > _SID_START_SONG_MAX:
        _SID_START_SONG.popitem(last=False)


# Start songs learnt from file headers, waiting to be written back to their
# track records (track id → the fields to write) — batched, so a listener
# skipping through a SID collection costs one store write (and one catalogue
# refresh) per ``_START_SONG_FLUSH_S``, not one per file.
_START_SONG_PENDING: dict[str, dict] = {}
_START_SONG_FLUSH_S = 15.0
_start_song_flush: "asyncio.TimerHandle | None" = None


def _persist_start_song(track_id: str, start: int, count: int) -> None:
    """Write a multi-tune file's header start song (1-based ``start``) back to
    its track record as the scan's 0-based ``start_subsong`` — with the
    record's ``duration`` set to that tune's HVSC length when it has one, as a
    fresh scan records it — so listings (Subsonic tune numbering and lengths)
    are right without the file.  Only for a record that carries no start
    song yet, and only when ``start`` is above 1 and within both the header's
    ``count`` and the record's ``subsongs``: a missing value already means
    tune 1.  Event loop only (the store isn't thread-safe);
    O(1) — the write itself is batched (``_flush_start_songs``)."""
    global _start_song_flush
    try:
        start, count = int(start), int(count)
        if not (count > 1 and 1 < start <= count) or track_id in _START_SONG_PENDING:
            return
        from soniqboom.core.store import get_store
        rec = get_store().get_track(track_id)
        if (not isinstance(rec, dict) or rec.get("start_subsong") is not None
                or not isinstance(rec.get("subsongs"), int)
                or not 1 < start <= rec["subsongs"]):
            return
        fields: dict = {"start_subsong": start - 1}
        lengths = rec.get("hvsc_lengths")
        if isinstance(lengths, list) and start - 1 < len(lengths) and lengths[start - 1]:
            fields["duration"] = lengths[start - 1]
        _START_SONG_PENDING[track_id] = fields
        if _start_song_flush is None:
            try:
                _start_song_flush = asyncio.get_running_loop().call_later(
                    _START_SONG_FLUSH_S, _flush_start_songs)
            except RuntimeError:           # no loop (a sync caller): write now
                _flush_start_songs()
    except Exception:
        log.debug("start song write-back skipped for %s", track_id, exc_info=True)


def _flush_start_songs() -> None:
    """Write the pending start songs in one batched store update."""
    global _start_song_flush
    _start_song_flush = None
    items = list(_START_SONG_PENDING.items())
    _START_SONG_PENDING.clear()
    if not items:
        return
    try:
        from soniqboom.core.store import get_store
        get_store().update_track_fields_batch(items)
    except Exception:
        log.debug("start song write-back failed", exc_info=True)


def _recorded_start_song(track) -> int | None:
    """The 1-based start song the track record carries (the scan's 0-based
    ``start_subsong``), else None."""
    meta = track if isinstance(track, dict) else getattr(track, "__dict__", {}) or {}
    z = meta.get("start_subsong")
    if z is None and not isinstance(track, dict):
        z = getattr(track, "start_subsong", None)
    if isinstance(z, int) and not isinstance(z, bool) and z >= 0:
        return z + 1
    return None


def sid_start_song_known(track_id: str, track) -> int:
    """The SID start song (1-based) as known without touching the file: the
    track record's, else one read from the file earlier, else 1.  O(1)."""
    rec = _recorded_start_song(track) if track is not None else None
    if rec is not None:
        return rec
    return _SID_START_SONG.get(track_id, 1)


async def sid_start_song(track_id: str, track, path: "Path | None" = None) -> int:
    """``sid_start_song_known``, reading (and remembering) the header of the
    local file ``path`` — or of the track's own path when that is a plain
    local file — when neither the record nor an earlier read knows it."""
    rec = _recorded_start_song(track) if track is not None else None
    if rec is not None:
        return rec
    got = _SID_START_SONG.get(track_id)
    if got is not None:
        return got
    if path is None:
        p = str(getattr(track, "path", "") or "") if track is not None else ""
        if not p or "::" in p or "://" in p:
            return 1
        path = Path(p)
    try:
        count, start = await asyncio.wait_for(
            asyncio.to_thread(_psid_count_start, path), timeout=5.0)
    except Exception:
        return 1
    if count > 0:                       # a real header: remember it
        _note_sid_start_song(track_id, start)
        _persist_start_song(track_id, start, count)
    return start


# ── Default tune of the other multi-tune files ──────────────────────────────
# SID and SNDH name their default tune in the header (above) and their
# renderers map the wire themselves.  Every other renderer takes the tune
# INDEX, and for them the wire IS the index: ``?subsong=N`` / ``<id>~N`` /
# a playlist's ``{id, subsong: N}`` always play tune N + 1, so stored ids
# never change meaning.  Cache keys, VU sidecars and waveforms are keyed by
# that index (uade: 0 = its default "cur" tune = its first, N = the module's
# first subsong + N).
#
# Only a BARE play — no tune asked for: the bare id on the web, Subsonic or a
# cast receiver, a prewarm of it — picks a tune itself.  For the families
# whose first tune is often an empty stub (uade custom players: an init or
# sound-effect slot; libgme rips; sc68 disks) that is the FIRST TUNE THAT
# ISN'T EMPTY: at least ``silence.MIN_TUNE_SECONDS`` with audible sound,
# learnt once by short probe renders (``ensure_default_tune``) and recorded
# as the track's ``default_subsong`` (0 when the first tune is fine, so
# "probed" is remembered too).  The track's stored ``duration`` describes
# that tune.  ``tune_index`` turns what a request named into the index
# (O(1)); entry points that have the file run ``ensure_default_tune`` first
# for a bare play.

_PROBE_FAMILIES = frozenset({"uade", "gme", "sc68"})
# track id → (file version, 0-based default tune) decided in this process — a
# fallback when the record can't take the write; the version
# (``_file_version``: the record's mtime and size) ties it to the file it was
# learnt from.  Bounded, oldest dropped.
_DEFAULT_TUNE: "OrderedDict[str, tuple[tuple, int]]" = OrderedDict()
_DEFAULT_TUNE_MAX = 8192
# track id → an UNDECIDED probe's state for one file version (never written to
# the record): {"v": version, "next": the first candidate not judged yet —
# every one before it was judged not to be a tune (a definite verdict, kept:
# the next probe resumes there), "pick": the tune a bare play plays
# meanwhile, "retry": monotonic time before which no new probe starts (after
# a candidate that couldn't be told or no render slot; 0 = go on at once)}.
# A bare play waits for a file's probe once; while its state stands, later
# bare plays play the pick at once and the probe goes on in the background
# (``_start_default_probe``).  Bounded, oldest dropped.
_DEFAULT_PROBE_STATE: "OrderedDict[str, dict]" = OrderedDict()
_DEFAULT_TUNE_RETRY_S = 120.0       # after an undecided candidate / no slot
_DEFAULT_PROBES: "dict[str, asyncio.Future]" = {}
_DEFAULT_PROBE_MAX_TUNES = 16       # candidates tried, in order
_DEFAULT_PROBE_BUDGET_S = 8.0       # wall time per probe run, from the render slot on
_DEFAULT_PROBE_SLOT_WAIT_S = 30.0   # at most this long waiting for that slot
_PROBE_MAX_AUDIO_S = 30.0           # audio judged per candidate (uade ends silence at 20 s)
_PROBE_WALL_S = 10.0                # one candidate's renderer run


def _tfield(track, name: str):
    """Field ``name`` of a track model or a store record dict (None if absent)."""
    if track is None:
        return None
    if isinstance(track, dict):
        return track.get(name)
    return getattr(track, name, None)


def _header_tuned(track) -> bool:
    """Do this track's renderer and record map the wire with the file's own
    start song (C64 SID, Atari SNDH)?  Then a request's wire is passed on as
    it is, and a bare play is wire 0."""
    fam = str(_tfield(track, "format") or "").split("/")[0].strip().upper()
    return fam in ("SID", "SNDH")


def _tune_count(track) -> int:
    n = _tfield(track, "subsongs")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 1 else 0


def _file_version(track) -> tuple:
    """What ties an in-process default-tune memo to one version of the file:
    the record's mtime and size (a file replaced keeping its mtime has
    another size)."""
    return (_tfield(track, "mtime"), _tfield(track, "file_size"))


def explicit_wire(subsong: "int | None", request: "Request | None" = None) -> "int | None":
    """The tune a request names, or None for a bare play.  ``?subsong=N``
    names tune N — also ``?subsong=0`` (the web player's pick of the first
    tune); an in-process caller's 0 (Subsonic's bare id, the cast byte
    server) is a bare play unless its own request carries ``subsong``."""
    if subsong is None:
        return None
    s = max(0, int(subsong))
    if s > 0:
        return s
    try:
        named = request is not None and "subsong" in request.query_params
    except Exception:
        named = False
    return 0 if named else None


def _default_tune_decided(track_id: str, track) -> "int | None":
    """The default tune a probe DECIDED for this file version: the record's
    ``default_subsong``, else this process's memo (``_DEFAULT_TUNE``) — or
    None.  O(1)."""
    if track is None or _header_tuned(track):
        return None
    n = _tune_count(track)
    z = _tfield(track, "default_subsong")
    if isinstance(z, int) and not isinstance(z, bool) and 0 <= z and (not n or z < n):
        return z
    got = _DEFAULT_TUNE.get(track_id)
    if got is not None and got[0] == _file_version(track):
        return got[1]
    return None


def _probe_state(track_id: str, track) -> "dict | None":
    """This file version's undecided-probe state (``_DEFAULT_PROBE_STATE``)."""
    st = _DEFAULT_PROBE_STATE.get(track_id)
    if st is not None and st["v"] != _file_version(track):
        return None
    return st


def default_tune_known(track_id: str, track) -> "int | None":
    """The 0-based tune a bare play of a multi-tune file whose renderer takes
    a tune index plays, as known without touching the file: the decided
    default (``_default_tune_decided``), else an undecided probe's pick for
    this file version (``_DEFAULT_PROBE_STATE``) — or None (not probed yet).
    O(1)."""
    got = _default_tune_decided(track_id, track)
    if got is not None or track is None or _header_tuned(track):
        return got
    st = _probe_state(track_id, track)
    return st["pick"] if st is not None else None


def tune_index(track_id: str, track, wire: "int | None") -> int:
    """The tune index a request plays: the wire it named (SID / SNDH: the
    wire, which their renderers map), or for a bare play (``None``) the
    file's default tune — 0 for SID / SNDH and while it isn't known.  O(1)."""
    if wire is not None:
        return max(0, int(wire))
    return default_tune_known(track_id, track) or 0


def default_tune_index(track_id: str, track) -> int:
    """The index of the tune a track's stored ``duration`` describes: the
    decided default tune (what a bare play renders, ``tune_index(…, None)``)
    — 0 while undecided: an undecided probe's pick plays, but its length is
    neither promised from the record nor written to it."""
    return _default_tune_decided(track_id, track) or 0


def _probe_family(ext: str, uade_named: bool, path: "Path | None" = None, *,
                  c64: "bool | None" = None) -> "str | None":
    """Which default-tune probe applies to a play routed like this:
    ``"uade"`` / ``"gme"`` / ``"sc68"``, else None — in the order playback
    routes (a verified Amiga module goes to uade whatever its suffix).
    ``c64``: the PSID/RSID verdict for a ``.sid`` when already known (else
    read from ``path``; with neither, a ``.sid`` is undecided — None)."""
    if ext in _UADE_EXTS or uade_named:
        return "uade"
    if ext in _GME_EXTS_STREAM:
        return "gme"
    if ext in _SC68_EXTS:
        return "sc68"
    if ext in _SID_EXTS:
        if c64 is None:
            if path is None:
                return None
            c64 = _is_c64_sid(path)
        return None if c64 else "uade"
    return None


def _set_probe_state(track_id: str, track, nxt: int, pick: int, retry: float) -> None:
    _DEFAULT_PROBE_STATE[track_id] = {"v": _file_version(track), "next": int(nxt),
                                      "pick": int(pick), "retry": float(retry)}
    _DEFAULT_PROBE_STATE.move_to_end(track_id)
    while len(_DEFAULT_PROBE_STATE) > _DEFAULT_TUNE_MAX:
        _DEFAULT_PROBE_STATE.popitem(last=False)


def _note_default_tune(track_id: str, track, index: int, family: str) -> None:
    """Record the probed default tune on the track at once (event loop only).
    A default past the first tune also resets the stored length — it was the
    first tune's — to what the scan stores for the family, so the next bare
    render measures it, and blanks the stored scrubber waveform (recomputed
    from that render)."""
    _DEFAULT_PROBE_STATE.pop(track_id, None)
    _DEFAULT_TUNE[track_id] = (_file_version(track), int(index))
    _DEFAULT_TUNE.move_to_end(track_id)
    while len(_DEFAULT_TUNE) > _DEFAULT_TUNE_MAX:
        _DEFAULT_TUNE.popitem(last=False)
    fields: dict = {"default_subsong": int(index)}
    if index:
        fields["duration"] = (float(settings.sid_default_duration)
                              if family == "gme" else 0.0)
    try:
        from soniqboom.core.store import get_store
        store = get_store()
        rec = store.get_track(track_id)
        if rec is None or _file_version(rec) != _file_version(track):
            return                       # gone, or another file version by now
        store.update_track_fields(track_id, fields)
        if index and store.get_waveform(track_id):
            store.store_waveform(track_id, [])
    except Exception:
        log.debug("default tune write-back skipped for %s", track_id, exc_info=True)


async def _pcm_probe(cmd: list[str], *, cwd: "str | None" = None,
                     env: "dict | None" = None, wav_header: bool) -> "bool | None":
    """Run a renderer that writes 44.1 kHz stereo s16 PCM to stdout (behind a
    WAV header when ``wav_header``) and judge the tune: True once it has
    played ``MIN_TUNE_SECONDS`` and was audible; False when it ended shorter
    or stayed silent through ``_PROBE_MAX_AUDIO_S``; None when the run
    failed to start or took longer than ``_PROBE_WALL_S`` (undecided).  The
    process is killed as soon as the verdict is in."""
    from soniqboom.core.silence import MIN_TUNE_SECONDS
    try:
        proc = await forksafe.spawn(*cmd, stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.DEVNULL, cwd=cwd, env=env)
    except OSError:
        return None
    meter = _AudibilityMeter(2, 44100)

    async def _judge() -> bool:
        head = b""
        header_done = not wav_header
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return meter.audible and meter.seconds >= MIN_TUNE_SECONDS
            if not header_done:
                head += chunk
                i = head.find(b"data")
                if i < 0 or len(head) < i + 8:
                    if len(head) > 4096:
                        return False
                    continue
                chunk, header_done = head[i + 8:], True
            meter.feed(chunk)
            if meter.audible and meter.seconds >= MIN_TUNE_SECONDS:
                return True
            if meter.seconds >= _PROBE_MAX_AUDIO_S:
                return meter.audible

    try:
        verdict = await asyncio.wait_for(_judge(), timeout=_PROBE_WALL_S)
    except asyncio.TimeoutError:
        verdict = None
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass
    return verdict


async def _tune_audible(family: str, path: Path, index: int, *, base: int = 0,
                        gme_data: "bytes | None" = None) -> "bool | None":
    """Is 0-based tune ``index`` of the local file ``path`` a real tune
    (``_pcm_probe``)?  None when it can't be told (the caller then keeps it)
    — also when the file is gone by then (a temporary copy a later probe run
    outlived: the renderer's silence is then no verdict).  The caller holds
    the render slot (``_probe_default_tune``)."""
    verdict = await _tune_audible_run(family, path, index, base=base, gme_data=gme_data)
    if (verdict is False and family != "gme"
            and not await asyncio.to_thread(os.path.exists, str(path))):
        return None
    return verdict


async def _tune_audible_run(family: str, path: Path, index: int, *, base: int = 0,
                            gme_data: "bytes | None" = None) -> "bool | None":
    if family == "gme":
        from soniqboom.core import gme_render
        if not gme_render.is_available() or gme_data is None:
            return None
        return await asyncio.to_thread(gme_render.tune_audible, gme_data, index,
                                       max_seconds=_PROBE_MAX_AUDIO_S)
    if family == "uade":
        binary = _find_renderer(settings.uade123_path, "uade123")
        if not binary:
            return None
        try:
            async with _xpk_unpacked(path) as src:
                return await _pcm_probe(
                    [binary, "-1", "-c", *_uade_subsong_arg(index, base), "--", src.name],
                    cwd=_uade_cwd(src), wav_header=True)
        except HTTPException:
            return None
    if family == "sc68":
        binary = _find_renderer(settings.sc68_path, "sc68")
        if not binary:
            return None
        home = await asyncio.get_running_loop().run_in_executor(None, _sc68_home)
        env = dict(os.environ, SC68_HOME=str(home), HOME=str(home))
        return await _pcm_probe([binary, str(path), f"--track={index + 1}"],
                                env=env, wav_header=False)
    return None


async def _cached_default_ok(track_id: str, family: str) -> bool:
    """Is the first tune's render already cached, long enough and audible —
    a real tune, no probe needed?  (A render cached before silent renders
    were refused may be silence, so it is judged here too: one read of the
    file, stopping at its first audible block.)"""
    from soniqboom.core.conversion_cache import get_cached
    from soniqboom.core.silence import MIN_TUNE_SECONDS, wav_audible
    key = (uade_cache_key(track_id, 0) if family == "uade"
           else _ck(track_id, family, subsong=0))
    wav = await get_cached(key)
    if wav is None:
        return False

    def _judge() -> bool:
        return (_wav_audio_seconds(wav) >= MIN_TUNE_SECONDS
                and wav_audible(wav) is not False)
    try:
        return await asyncio.to_thread(_judge)
    except Exception:
        return False


async def _probe_default_tune(track_id: str, track, path: Path, family: str) -> int:
    """Find (and record) a file's default tune: the first tune, in order,
    that is a real tune — of the first ``_DEFAULT_PROBE_MAX_TUNES``.  One
    render slot is held per run (a renderer runs per candidate); the run's
    budget (``_DEFAULT_PROBE_BUDGET_S``) starts once it holds the slot —
    waiting for one (all busy rendering) costs no candidate — and that wait
    is bounded (``_DEFAULT_PROBE_SLOT_WAIT_S``).  Recorded on the track
    (``_note_default_tune``) once decided: a candidate is a real tune, or
    every candidate was judged not to be one (then the first tune — a silent
    file is reported when it renders).  Otherwise the run leaves the file's
    state (``_DEFAULT_PROBE_STATE``, never the record): candidates judged
    not to be tunes stay judged — the next run resumes after them (the
    budget ran out: at once, in the background) — and the pick a bare play
    plays meanwhile is the first candidate not judged yet; a candidate that
    can't be told (a renderer that timed out, a file gone meanwhile) or no
    slot waits ``_DEFAULT_TUNE_RETRY_S`` for the next run.  Returns the tune
    a bare play plays now."""
    count = _tune_count(track)
    end = min(count, _DEFAULT_PROBE_MAX_TUNES)
    st = _probe_state(track_id, track)
    start = st["next"] if st is not None else 0
    base: "int | None" = None           # uade: resolved only for a tune past the first
    gme_data = None
    if family == "gme":
        from soniqboom.core import gme_render
        try:
            raw = await asyncio.to_thread(path.read_bytes)
            gme_data = await asyncio.to_thread(gme_render.unpack_gym, raw)
        except (OSError, ValueError):
            gme_data = None
    nxt = start                         # the first candidate not judged yet
    chosen: "int | None" = None         # decided
    unsure: "int | None" = None         # a candidate that can't be told
    no_slot = False
    t_wait = time.monotonic()
    if start == 0 and await _cached_default_ok(track_id, family):
        chosen = 0                                      # the first tune's render is music
    else:
        slot = _render_sem if family != "gme" else None
        held = False
        try:
            if slot is not None:
                try:
                    await asyncio.wait_for(slot.acquire(), timeout=_DEFAULT_PROBE_SLOT_WAIT_S)
                    held = True
                except asyncio.TimeoutError:
                    no_slot = True
            if not no_slot:
                t0 = time.monotonic()
                for i in range(start, end):
                    if time.monotonic() - t0 > _DEFAULT_PROBE_BUDGET_S:
                        break
                    if family == "uade" and i and base is None:
                        base = await _uade_resolve_base(track_id, track, path, i)
                    verdict = await _tune_audible(family, path, i, base=base or 0,
                                                  gme_data=gme_data)
                    if verdict is True:
                        chosen = i
                        break
                    if verdict is None:
                        unsure = i
                        break
                    nxt = i + 1                 # judged: not a tune
                else:
                    chosen = 0                  # every candidate: not a tune
        finally:
            if held:
                slot.release()
    if chosen is not None:
        log.debug("Default tune of %s (%s, %d tunes): %d — %.0f ms", track_id, family,
                  count, chosen, (time.monotonic() - t_wait) * 1000)
        _note_default_tune(track_id, track, chosen, family)
        return chosen
    pick = unsure if unsure is not None else nxt
    stuck = unsure is not None or no_slot
    log.debug("Default tune of %s (%s, %d tunes): undecided after %d, playing %d%s — %.0f ms",
              track_id, family, count, nxt, pick, " (retried later)" if stuck else "",
              (time.monotonic() - t_wait) * 1000)
    _set_probe_state(track_id, track, nxt, pick,
                     time.monotonic() + _DEFAULT_TUNE_RETRY_S if stuck else 0.0)
    return pick


def _start_default_probe(track_id: str, track, path: Path, family: str, *,
                         background: bool) -> "asyncio.Future":
    """Start a probe run of ``track_id`` (``_probe_default_tune``), shared
    through ``_DEFAULT_PROBES``; ``background``: behind ``_bg_render_sem`` at
    ``PRIO_PROBE``, as the other probes.  A run that ends undecided with its
    budget spent (state ``retry`` 0) is followed by a background run at once,
    so the file settles without any play waiting for it; one that failed
    waits ``_DEFAULT_TUNE_RETRY_S``."""
    async def _run() -> int:
        if background:
            async with _bg_render_sem.slot(PRIO_PROBE):
                return await _probe_default_tune(track_id, track, path, family)
        return await _probe_default_tune(track_id, track, path, family)

    fut = asyncio.ensure_future(_run())
    _DEFAULT_PROBES[track_id] = fut

    def _done(f: "asyncio.Future") -> None:
        if _DEFAULT_PROBES.get(track_id) is f:
            _DEFAULT_PROBES.pop(track_id, None)
        if f.cancelled():
            return
        if f.exception() is not None:
            st = _probe_state(track_id, track)
            _set_probe_state(track_id, track, st["next"] if st else 0,
                             st["pick"] if st else 0, time.monotonic() + _DEFAULT_TUNE_RETRY_S)
            return
        st = _probe_state(track_id, track)
        if (st is not None and st["retry"] == 0.0 and track_id not in _DEFAULT_PROBES
                and _default_tune_decided(track_id, track) is None):
            try:
                _start_default_probe(track_id, track, path, family, background=True)
            except RuntimeError:
                pass                                    # the loop is closing
    fut.add_done_callback(_done)
    return fut


def default_probe_running(track_id: str) -> bool:
    """Is a default-tune probe of ``track_id`` under way?"""
    fut = _DEFAULT_PROBES.get(track_id)
    return fut is not None and not fut.done()


async def await_default_probe(track_id: str, timeout: float) -> "int | None":
    """Wait (at most ``timeout`` s) for a default-tune probe already under way
    (started by a play or a prewarm); its result, or None."""
    fut = _DEFAULT_PROBES.get(track_id)
    if fut is None:
        return None
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except Exception:
        return None


async def ensure_default_tune(track_id: str, track, path: Path,
                              family: "str | None", *,
                              background: bool = False) -> "int | None":
    """The 0-based tune a bare play of a multi-tune file of a probed family
    plays: its decided default tune; else, the first time, the outcome of a
    probe of the local file ``path`` (``_probe_default_tune`` — concurrent
    callers share one run); once a run ended undecided, the best pick so far
    at once, while the probe goes on in the background (a bare play waits
    for a file's probe once).  None for a single-tune file or another family
    (and SID / SNDH, whose header names it).  ``background``: a first run
    started here waits behind the background render gate (a Track Info open,
    not a play)."""
    if (family not in _PROBE_FAMILIES or track is None or _tune_count(track) <= 1
            or _header_tuned(track)):
        return None
    known = _default_tune_decided(track_id, track)
    if known is not None:
        return known
    fut = _DEFAULT_PROBES.get(track_id)
    st = _probe_state(track_id, track)
    if st is not None:
        if fut is None and time.monotonic() >= st["retry"]:
            _start_default_probe(track_id, track, path, family, background=True)
        return st["pick"]
    if fut is None:
        fut = _start_default_probe(track_id, track, path, family, background=background)
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("Default-tune probe of %s failed", track_id, exc_info=True)
        return default_tune_known(track_id, track) or 0


async def _bare_play_track(track_id: str, track, path: Path, family: "str | None"):
    """For a bare play of a multi-tune file: make sure its default tune is
    known (``ensure_default_tune``) and return the track as the store has it
    now — the probe may have moved the default and reset the stored length,
    which the caller's copy predates.  A libgme rip scanned before tune
    counts were read gets its count first (``_gme_backfill_tune_count``)."""
    if family == "gme" and not _tune_count(track):
        track = await _gme_backfill_tune_count(track_id, track, path)
    if (family is None or _tune_count(track) <= 1 or _header_tuned(track)
            or _tfield(track, "default_subsong") is not None):
        return track                     # nothing to probe, or the copy is current
    if _default_tune_decided(track_id, track) is None:
        await ensure_default_tune(track_id, track, path, family)
    return await get_track(track_id) or track


_GME_COUNTED_EXTS = (".nsf", ".nsfe", ".gbs", ".ay", ".sap")
# (track id, mtime) of files whose header was read and holds one tune (or
# none readable): not read again on every play.  Bounded.
_GME_COUNT_READ: "OrderedDict[tuple, bool]" = OrderedDict()


async def _gme_backfill_tune_count(track_id: str, track, path: "Path | None"):
    """A libgme rip's tune count read from its header (NSF / NSFe / GBS / AY
    / SAP) for a record scanned before the count was recorded: written to the
    record and returned on a copy of ``track`` (unchanged when there is
    nothing to add or the file can't be read)."""
    if track is None or _tune_count(track) or path is None:
        return track
    if not str(path).lower().endswith(_GME_COUNTED_EXTS):
        return track
    seen = (track_id, _tfield(track, "mtime"))
    if seen in _GME_COUNT_READ:
        return track

    def _read() -> bytes:
        with open(path, "rb") as fh:
            return fh.read(64 * 1024)
    try:
        head = await asyncio.wait_for(asyncio.to_thread(_read), timeout=5.0)
    except Exception:
        return track
    from soniqboom.core.gme_render import header_tunes
    n, _ = header_tunes(head)
    if not n or n < 2:
        _GME_COUNT_READ[seen] = True
        while len(_GME_COUNT_READ) > 4096:
            _GME_COUNT_READ.popitem(last=False)
        return track
    try:
        from soniqboom.core.store import get_store
        get_store().update_track_fields(track_id, {"subsongs": n})
    except Exception:
        log.debug("tune count write-back skipped for %s", track_id, exc_info=True)
    if isinstance(track, dict):
        return {**track, "subsongs": n}
    if hasattr(track, "model_copy"):
        return track.model_copy(update={"subsongs": n})
    import copy
    out = copy.copy(track)
    out.subsongs = n
    return out


def _sid_render_cmd(binary: str, path: Path, subsong: int, dur: int, out_wav: str,
                    mute: "tuple[int, ...]" = (), start: int = 1) -> list[str]:
    """Build the sidplayfp argv shared by the blocking and progressive renders,
    so the two paths never drift on chip-model / filter / digiboost flags.

    ``subsong`` is the wire index and ``start`` the file's 1-based start song
    (``sid_wire_tune``); sidplayfp's ``-o<num>`` is the 1-based tune.  Wire 0
    passes no flag: sidplayfp then plays the tune's own start song (PSID
    header).

    ``mute`` is a tuple of 1-indexed voices to silence via ``-u<n>`` — used by
    the per-voice VU pass (sid_vu) to isolate one voice per render.

    SID chip-model / filter overrides (settings; defaults = no flags, i.e.
    sidplayfp honours the tune's own PSID header).  Flags verified against
    sidplayfp --help: -m<o|n>[f], -nf, --fcurve=<num>, --digiboost, -u<n> mute
    voice; no space between a short flag and its value."""
    cmd = [binary]
    if subsong > 0:
        cmd.append(f"-o{sid_wire_tune(subsong, start)}")
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
    # Pin the output rate: sidplayfp's default changed (44100 → 48000 in newer
    # builds), and the progressive path synthesizes its WAV header from
    # ``_SID_WAV_RATE`` before the file exists — a mismatch plays at the wrong pitch.
    cmd.append(f"-f{_SID_WAV_RATE}")
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
    start = await _psid_start_song(path) if subsong > 0 else 1
    async with _sid_blocking_sem():
        tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_wav.close()
        cmd = _sid_render_cmd(binary, path, subsong, dur, tmp_wav.name, start=start)
        try:
            await _await_renderer(cmd, Path(tmp_wav.name), timeout=dur + 30, kind="SID")
            # Exactly ``dur`` seconds, like a progressive render of the tune
            # (``_normalize_sid_wav``): one size per cache key, whichever
            # path rendered it — what HEAD and a progressive answer promise.
            await asyncio.to_thread(
                _normalize_sid_wav, Path(tmp_wav.name),
                dur * _SID_WAV_RATE * _SID_WAV_CHANNELS * (_SID_WAV_BITS // 8))
        except BaseException:
            # Also when cancelled while still waiting for a render slot (a
            # retired prewarm): the temp exists before the renderer does.
            Path(tmp_wav.name).unlink(missing_ok=True)
            raise
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
            start = await _psid_start_song(sid_path) if subsong > 0 else 1
            for mute in sid_vu.VOICE_MUTES:
                tf = tempfile.NamedTemporaryFile(suffix=".wav", prefix="sidvu-", delete=False)
                tf.close()
                tp = Path(tf.name)
                tmps.append(tp)
                cmd = _sid_render_cmd(binary, sid_path, subsong, dur, tf.name, mute=mute,
                                      start=start)
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


async def _sid_prewarm_render(full_key: str, path: Path, subsong: int, dur: int) -> Path:
    """``_render_sid`` for a prewarm, registered in ``_SID_PREWARM_RENDERS``
    while it runs so a play of the same tune can retire it.  Ends as retired
    (cancelled) at once when a progressive play of the tune was admitted
    first — the check and the registration are one synchronous step, and so
    are that play's admission and its retire, so one of them always sees the
    other."""
    prog = _SID_PROG_DONE.get(full_key)
    if prog is not None and not prog.is_set():
        raise asyncio.CancelledError()
    me = asyncio.current_task()
    if me is not None:
        _SID_PREWARM_RENDERS[full_key] = me
    try:
        return await _render_sid(path, subsong=subsong, duration=dur)
    finally:
        if me is not None and _SID_PREWARM_RENDERS.get(full_key) is me:
            _SID_PREWARM_RENDERS.pop(full_key, None)


async def _retire_sid_prewarm(full_key: str) -> bool:
    """Stop a prewarm's blocking render of ``full_key`` because a listener is
    about to play the tune progressively (audio in ~0.2 s instead of waiting
    for the prewarm's whole render).  Cancelling its task kills its sidplayfp
    and drops its temp (``_await_renderer``); we wait for that, so at most one
    render of the tune is ever running.  True when one was retired."""
    task = _SID_PREWARM_RENDERS.pop(full_key, None)
    if task is None or task.done():
        return False
    task.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.wait({task}, timeout=5.0)
    log.info("progressive SID: retired the prewarm render of %s for a play", full_key)
    return True


async def _progressive_render_silent(key: str, format_type: str, wav: Path) -> bool:
    """For a render that was streamed while it ran (judged only now): True —
    having dropped the file and recorded the failure, so ``/render-status``
    says why and the next play renders blocking and gets the 422 — when it is
    silent (``conversion_cache.refuse_silent``)."""
    from soniqboom.core.conversion_cache import refuse_silent, note_render_failure
    try:
        await refuse_silent(key, format_type, wav)
    except HTTPException as exc:
        note_render_failure(key, exc.status_code, exc.detail)
        return True
    return False


async def _await_sid_audible(proc, tmp: Path, *,
                             first_byte_timeout: "float | None" = None,
                             audible_timeout: "float | None" = None,
                             hard_timeout: "float | None" = None) -> str:
    """Wait for sidplayfp's first AUDIBLE PCM in ``tmp`` (``core.silence``)
    before a progressive SID answers — a tune that plays only silence must
    reach the player as the cache's 422, not as minutes of streamed silence,
    and an immediate render failure (bad tune, missing ROM) as a real error:

      "audible" — sound is in: stream;
      "stream"  — only with a timeout given (our web UI): the render is alive
                  but nothing audible after ``audible_timeout`` s (a long
                  silent intro — sidplayfp renders ~15x realtime, so that is
                  minutes of tune), or no byte at all after
                  ``first_byte_timeout`` s (the generator's idle timeout
                  handles a stuck render): stream anyway;
      "failed"  — exited without PCM, or with an error, or still alive after
                  ``hard_timeout`` s: the caller falls back to the blocking
                  render, which surfaces the error;
      "silent"  — exited cleanly having rendered only silence.
    Without timeouts (every other client) the answer waits for sound or the
    end of the render: "stream" never comes."""
    meter = _AudibilityMeter(_SID_WAV_CHANNELS, _SID_WAV_RATE)
    st = {"fd": -1, "pos": _WAV_HEADER_LEN, "closed": False}
    read_lock = threading.Lock()         # see ``_tail_render``'s step lock

    def _read() -> bool:
        with read_lock:
            return False if st["closed"] else _read_locked()

    def _read_locked() -> bool:
        # Worker thread: judge the PCM that is new since the last look.
        if st["fd"] < 0:
            try:
                st["fd"] = os.open(str(tmp), os.O_RDONLY)
            except OSError:
                return False
        size = os.fstat(st["fd"]).st_size
        while st["pos"] < size:
            buf = os.pread(st["fd"], min(size - st["pos"], 1 << 20), st["pos"])
            if not buf:
                break
            st["pos"] += len(buf)
            if meter.feed(buf):
                return True
        return False

    t0 = time.monotonic()
    try:
        while True:
            exited = proc.returncode is not None
            if await asyncio.to_thread(_read):
                return "audible"
            if exited:
                if st["pos"] <= _WAV_HEADER_LEN or proc.returncode != 0:
                    return "failed"
                return "silent"
            waited = time.monotonic() - t0
            if (audible_timeout is not None and waited >= audible_timeout) or (
                    first_byte_timeout is not None and st["pos"] <= _WAV_HEADER_LEN
                    and waited >= first_byte_timeout):
                return "stream"
            if hard_timeout is not None and waited >= hard_timeout:
                return "failed"
            await asyncio.sleep(_TAIL_POLL_S)
    finally:
        with read_lock:
            st["closed"] = True
            if st["fd"] >= 0:
                os.close(st["fd"])
                st["fd"] = -1


def _normalize_sid_wav(path: Path, data_bytes: int) -> None:
    """Make a finished sidplayfp WAV exactly the resource the progressive
    stream and HEAD promise: ``data_bytes`` of PCM under the synthesized
    header.  sidplayfp renders a few milliseconds past ``-t``; within a
    second either way the tail is cut / padded with silence, a bigger
    difference (or a layout that isn't sidplayfp's 44-byte mono 16-bit
    header) is left alone.  Blocking."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(_WAV_HEADER_LEN)
    except OSError:
        return
    lay = _tail_fmt(head)
    if (lay is None or lay[1] != _WAV_HEADER_LEN or lay[0]["tag"] != 1
            or lay[0]["bits"] != _SID_WAV_BITS or lay[0]["channels"] != _SID_WAV_CHANNELS
            or lay[0]["rate"] != _SID_WAV_RATE):
        return
    pcm = size - _WAV_HEADER_LEN
    one_sec = _SID_WAV_RATE * _SID_WAV_CHANNELS * (_SID_WAV_BITS // 8)
    if abs(pcm - data_bytes) > one_sec:
        return
    want = _synth_wav_header(_SID_WAV_RATE, _SID_WAV_CHANNELS, _SID_WAV_BITS, data_bytes)
    if pcm == data_bytes and head == want:
        return
    try:
        with open(path, "r+b") as fh:
            if pcm > data_bytes:
                fh.truncate(_WAV_HEADER_LEN + data_bytes)
            elif pcm < data_bytes:
                fh.seek(_WAV_HEADER_LEN + pcm)
                fh.write(b"\x00" * (data_bytes - pcm))
            fh.seek(0)
            fh.write(want)
    except OSError:
        pass


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
    *,
    web: bool = True,
) -> "Response | None":
    """Cold-play a SID with ~instant start: spawn sidplayfp and stream its
    STILL-RENDERING WAV instead of awaiting the whole render.

    sidplayfp renders ~15x realtime, so the first ~1 s of audio is on disk in
    ~0.2 s; we ship a synthesised full-length WAV header immediately, then relay
    sidplayfp's PCM (skipping its own 44-byte header) as the file grows.  As soon
    as the render finishes cleanly (full length, exit 0) the WAV is promoted to
    the conversion cache under ``full_key`` — even while the listener is still
    reading it — so the next play, the waveform and /render-status see it at
    once; ``_SID_PROG_DONE`` lets them attach to it while it renders.

    Returns ``None`` (having cleaned up) when the render fails to start or the
    pool is full — the caller must then use the blocking path so the error
    surfaces as a real 5xx rather than a silent full-length-silence 200.

    Answers Range/seek requests with a proper 206 + Content-Range against the
    known full length (``44 + data_bytes``), and serves header-only probes
    (Safari's ``bytes=0-1``) straight from the synthesised header with no render.
    That exact length is what lets every GET client take it — the web UI,
    Subsonic, DLNA and the cast byte-server alike; cache hits are served from
    the file.  A request for a tune whose render is already running attaches
    to it as another reader (``_SID_PROG_RENDERS``).

    Nothing is sent before the render is audible (``_await_sid_audible``) —
    except, for our web UI (``web``), once ~20 s of rendering produced
    nothing audible (a long silent intro; a tune that turns out silent is
    then reported from its next play on).  Every other caller waits for sound
    or the end of the render: a tune that plays only silence is the cache's
    422 (``HTTPException``), never streamed silence.  ``web`` also decides a
    render that stops producing output for a minute: the web UI's body is
    padded to its length with silence (an ``<audio>`` element rejects a short
    one), anyone else's is aborted.  ``request`` only supplies the Range
    header (an object with ``headers`` will do; None: the whole file)."""
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
    rng = _parse_audio_range(
        request.headers.get("range") if request is not None else None, total)
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

    # A render of this tune already running: be another reader of it.
    rd = _SID_PROG_RENDERS.get(full_key)
    if rd is not None and rd["data_bytes"] == data_bytes:
        handle = await _sid_attach(rd, web)
        if handle is not None:
            return _sid_reader_response(rd, handle, start, end_excl, is_range,
                                        base_headers, background, web)
        if (rd["verdict"] in ("audible", "stream") and not rd["stalled"]
                and rd["proc"] is not None
                and (rd["proc"].returncode is None or rd["proc"].returncode == 0)):
            # A healthy render this caller can't read live (it isn't audible
            # for it yet, or its file is being cached): never a second
            # sidplayfp beside it — wait for its result, then the caller's
            # blocking path finds the cached file (or the silent tune's 422).
            try:
                await asyncio.wait_for(rd["done_ev"].wait(),
                                       timeout=int(duration) + 90)
            except asyncio.TimeoutError:
                pass
            return None

    # Data range → we must render.  Concurrency cap: reserve a slot in the SAME
    # synchronous block as the check so a simultaneous burst can't all pass
    # "< cap" before any of them increments (asyncio runs this prefix to the
    # first ``await`` uninterrupted, so the count is authoritative here).
    #
    # A detached duplicate that could not be attached to (it stalled or
    # failed) is retired: the fresh live render supersedes it, and killing it
    # frees a slot before the cap check.
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
    _done_ev = asyncio.Event()
    _SID_PROG_DONE[full_key] = _done_ev
    _slot_released = {"v": False}

    def _release_slot() -> None:
        if not _slot_released["v"]:
            _slot_released["v"] = True
            _SID_PROG_ACTIVE[0] -= 1

    # The render's shared state (see ``_SID_PROG_RENDERS``).  ``readers``:
    # bodies being iterated now; ``started``: one ever was (else the response
    # was never sent); ``streamed_all``: one delivered the declared end;
    # ``stalled``: output stopped for a minute (not cacheable, not worth
    # finishing); ``detached``: the registry entry of a render left finishing
    # for the cache after its last reader left; ``fd_open``: a reader holds its
    # own descriptor (from then on the temp may be moved into the cache under
    # it); ``promoted``: the cache decision was made; ``verdict``: what
    # ``_await_sid_audible`` said (None while it is still listening).
    rd = {"key": full_key, "gen": _my_gen, "data_bytes": data_bytes, "header": header,
          "total": total, "binary": binary, "tmp": None, "proc": None,
          "readers": 0, "started": False, "streamed_all": False, "stalled": False,
          "detached": None, "fd_open": False, "promoted": False,
          "verdict": None, "verdict_ev": asyncio.Event(), "wake": asyncio.Event(),
          "release": _release_slot, "done_ev": _done_ev}
    _SID_PROG_RENDERS[full_key] = rd

    def _drop_inflight() -> None:
        # Identity-guarded so we never remove a newer render's entries.
        if _SID_PROG_INFLIGHT.get(full_key) == _my_gen:
            _SID_PROG_INFLIGHT.pop(full_key, None)
        _done_ev.set()
        if _SID_PROG_DONE.get(full_key) is _done_ev:
            _SID_PROG_DONE.pop(full_key, None)
        if _SID_PROG_RENDERS.get(full_key) is rd:
            _SID_PROG_RENDERS.pop(full_key, None)

    tmp: "Path | None" = None
    proc = None
    handle = None
    try:
        # A prewarm of this tune still rendering: stop it first (this render
        # supersedes it — audio now, not after its whole render).
        await _retire_sid_prewarm(full_key)
        tune_start = await _psid_start_song(sid_path) if subsong > 0 else 1
        # Create the temp + spawn together; if the spawn itself raises (FD
        # exhaustion, binary vanished mid-flight) unlink the orphaned temp — the
        # finaliser that normally owns cleanup isn't created until after this.
        tmp = Path(tempfile.mkstemp(suffix=".wav", prefix="sidprog-")[1])
        rd["tmp"] = tmp
        cmd = _sid_render_cmd(binary, sid_path, subsong, int(duration), str(tmp),
                              start=tune_start)
        proc = await forksafe.spawn(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        rd["proc"] = proc

        # Detect an immediate render failure — and a tune that plays only
        # silence — before committing to the streaming response.  If sidplayfp
        # dies without writing PCM, reap it, drop the temp, and signal the
        # caller to fall back to the blocking render.  A clean render that
        # held nothing audible is judged like the cache judges it: its 422
        # (raised here, the handler below cleans up), recorded so the next
        # play answers at once.  Only the web UI ever streams before sound
        # (``_await_sid_audible``'s "stream"); others wait for sound or the
        # end, bounded like the blocking render.
        if web:
            verdict = await _await_sid_audible(
                proc, tmp, first_byte_timeout=_SID_PROG_FIRST_BYTE_TIMEOUT,
                audible_timeout=_SID_PROG_AUDIBLE_WAIT)
        else:
            verdict = await _await_sid_audible(proc, tmp, hard_timeout=int(duration) + 30)
        if verdict == "silent":
            await asyncio.to_thread(_normalize_sid_wav, tmp, data_bytes)
            if await _progressive_render_silent(full_key, "sid", tmp):
                from soniqboom.core.conversion_cache import SILENT_RENDER_DETAIL
                raise HTTPException(422, SILENT_RENDER_DETAIL)
            verdict = "audible"       # the whole-file judgement differs: play it
        if verdict == "failed":
            try:
                if proc.returncode is None:
                    proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            tmp.unlink(missing_ok=True)
            log.info("progressive SID: no audible output (rc=%s) for %s — "
                     "falling back to blocking render", proc.returncode, full_key)
            rd["verdict"] = "failed"
            rd["verdict_ev"].set()
            _drop_inflight()          # finaliser (the usual popper) is never created here
            _release_slot()
            return None
        # Our own descriptor, opened before anything can move the temp.
        handle = _FdHandle(os.open(str(tmp), os.O_RDONLY))
        rd["fd_open"] = True
        rd["verdict"] = verdict
        rd["verdict_ev"].set()
    except BaseException:
        # Any failure before the finaliser exists (including CancelledError on
        # server shutdown mid-probe) must clean up itself — the finaliser that
        # normally owns the proc + temp isn't created until below.  ``kill`` is
        # synchronous so it runs even while the task is being cancelled; the
        # asyncio child watcher reaps the killed proc.
        if handle is not None:
            handle.close()
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
        rd["verdict"] = rd["verdict"] or "failed"
        rd["verdict_ev"].set()        # readers waiting to attach: not to this one
        _drop_inflight()              # finaliser never created on this path
        _release_slot()
        raise

    async def _finalise() -> None:
        # Owns the render's lifecycle INDEPENDENTLY of the response tasks, so a
        # client disconnect (which cancels a generator) can't abort the
        # reap/cache: awaiting ``proc.wait()`` inside the generator's finally
        # was being CancelledError'd on disconnect, leaking a zombie sidplayfp.
        _one_sec = _SID_WAV_RATE * _SID_WAV_CHANNELS * (_SID_WAV_BITS // 8)
        _min_file = _WAV_HEADER_LEN + max(0, data_bytes - _one_sec)

        async def _promote() -> None:
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
            # terminated → -15, reap-timeout kill → nonzero.  Runs once.
            if rd["promoted"]:
                return
            rd["promoted"] = True
            try:
                _tmp_size = os.path.getsize(tmp) if tmp.exists() else 0
            except OSError:
                _tmp_size = 0
            good = (proc.returncode == 0 and _tmp_size >= _min_file and not rd["stalled"])
            if good:
                # Exactly the resource this stream promised (``total``): a
                # later Range request against the cached file sees the same
                # size (sidplayfp renders a few ms past ``-t``).  Readers read
                # below ``total`` only, through their own descriptors.
                await asyncio.to_thread(_normalize_sid_wav, tmp, data_bytes)
                # Re-check: a concurrent render (another cold play, or a
                # blocking Subsonic play) may have cached this key first.  If
                # so, just drop our temp — store_cached is idempotent now, but
                # skipping avoids a redundant move + LRU touch.
                existing = await get_cached(full_key)
                if existing is not None:
                    tmp.unlink(missing_ok=True)
                    _spawn_sid_vu(full_key, existing, sid_path, subsong, int(duration))
                elif await _progressive_render_silent(full_key, "sid", tmp):
                    pass          # streamed once, never cached; the next play reports it
                else:
                    dest = await store_cached(full_key, "sid", tmp)   # MOVES tmp into cache
                    log.info("progressive SID: cached %s (%d s)%s", full_key,
                             int(duration),
                             " — finished after the listener left"
                             if rd["detached"] is not None else "")
                    # Kick off the retro per-voice VU meter in the background —
                    # ready for the next play; this one used the FFT fallback.
                    _spawn_sid_vu(full_key, dest, sid_path, subsong, int(duration))
            else:
                tmp.unlink(missing_ok=True)
            _done_ev.set()          # waveform / render-status: settled

        proc_wait = asyncio.ensure_future(proc.wait())
        try:
            # Wait until every reader has reached its final state — but cache
            # the render the moment it has finished cleanly, NOT when the
            # listeners have read it all: a slow reader held a finished tune
            # out of the cache (no waveform, "idle" render status, a
            # re-render on replay) for the whole play.  That is safe: every
            # reader holds its own open descriptor (it survives the move).
            #
            # A response the SERVER NEVER ITERATES (client vanished before the
            # body streamed) never starts a reader, so give it a short window
            # to START; if none has, don't pin a live pool slot for the whole
            # render — with the pool at 3, three such stuck slots would disable
            # the progressive path for everyone for ~duration s.  A reader that
            # HAS started is a genuinely slow client; wait the rest of the
            # budget for it.
            t0 = time.monotonic()
            start_by = t0 + 15
            hard_end = start_by + int(duration) + 45
            while not (rd["started"] and rd["readers"] == 0):
                if proc.returncode is not None and not rd["promoted"]:
                    await _promote()
                    continue
                now = time.monotonic()
                if now >= (hard_end if rd["started"] else start_by):
                    break          # never iterated → abandoned; or out of budget
                rd["wake"].clear()
                wake = asyncio.ensure_future(rd["wake"].wait())
                try:
                    await asyncio.wait({wake} if proc_wait.done() else {wake, proc_wait},
                                       timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    wake.cancel()
            # If no reader consumed the whole stream the render is normally
            # DETACHED to finish for the cache (see the readers' finally).
            # Kill it only when nothing detached it: a stalled render, an older
            # same-key duplicate, or a response the server never iterated
            # (client vanished before the body streamed).
            detached = rd["detached"] is not None
            if not rd["streamed_all"] and not detached and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            # A detached render legitimately keeps running for its remaining
            # render time (~duration/15 plus margin); a live-completed or
            # killed one reaps in seconds.
            _reap_s = int(duration) + 60 if detached else 15
            try:
                await asyncio.wait_for(asyncio.shield(proc_wait), timeout=_reap_s)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc_wait
                except Exception:
                    pass
            await _promote()
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
            if not proc_wait.done():
                proc_wait.cancel()
            # Drop OUR registry entries (a comeback/eviction/newer render may
            # already have replaced them — never pop someone else's).
            _ent = rd["detached"]
            if _ent is not None and _SID_DETACHED.get(full_key) is _ent:
                _SID_DETACHED.pop(full_key, None)
            _drop_inflight()
            _release_slot()          # idempotent — releases the concurrency slot
            _SID_PROG_FINALISERS.discard(fin_task)

    fin_task = asyncio.create_task(_finalise())
    _SID_PROG_FINALISERS.add(fin_task)          # strong ref so it isn't GC'd
    return _sid_reader_response(rd, handle, start, end_excl, is_range,
                                base_headers, background, web)


async def _sid_attach(rd: dict, web: bool) -> "_FdHandle | None":
    """Join the progressive SID render ``rd`` as another reader: its open
    descriptor once the render is audible for this caller, else None.  Waits
    for the first request's audibility verdict; a render the web UI streams
    before sound (a long silent intro) is judged again for anyone else, who
    never streams before sound.  A render that was left finishing for the
    cache (detached) is taken back — it is not evicted while it has a reader."""
    secs = rd["data_bytes"] // (_SID_WAV_RATE * _SID_WAV_CHANNELS * (_SID_WAV_BITS // 8))
    try:
        await asyncio.wait_for(rd["verdict_ev"].wait(), timeout=secs + 60)
    except asyncio.TimeoutError:
        return None
    proc, tmp = rd["proc"], rd["tmp"]
    if rd["verdict"] == "stream" and not web:
        if await _await_sid_audible(proc, tmp, hard_timeout=secs + 30) != "audible":
            return None
    elif rd["verdict"] not in ("audible", "stream"):
        return None
    # Synchronous from here: nothing can promote / evict in between.
    if (_SID_PROG_RENDERS.get(rd["key"]) is not rd or rd["promoted"] or rd["stalled"]
            or (proc.returncode is not None and proc.returncode != 0)):
        return None
    try:
        handle = _FdHandle(os.open(str(tmp), os.O_RDONLY))
    except OSError:
        return None
    ent = rd["detached"]
    if ent is not None:
        if _SID_DETACHED.get(rd["key"]) is ent:
            _SID_DETACHED.pop(rd["key"], None)
        rd["detached"] = None
        log.info("progressive SID: a listener came back — %s streams again", rd["key"])
    return handle


def _sid_reader_response(rd: dict, handle: "_FdHandle", start: int, end_excl: int,
                         is_range: bool, base_headers, background, web: bool) -> Response:
    """One reader of the progressive SID render ``rd``: the requested byte range
    ``[start, end_excl)`` of the virtual file — header region served from the
    synthesised header, data region read through ``handle`` from the growing
    temp at (pos - 44)."""
    header, data_bytes, total = rd["header"], rd["data_bytes"], rd["total"]
    proc, full_key = rd["proc"], rd["key"]
    h_lo, h_hi = start, min(end_excl, _WAV_HEADER_LEN)
    d_lo, d_hi = max(start, _WAV_HEADER_LEN) - _WAV_HEADER_LEN, end_excl - _WAV_HEADER_LEN

    async def _gen():
        rd["started"] = True        # the server is iterating us → not an abandoned response
        rd["readers"] += 1
        ent = rd["detached"]
        if ent is not None and proc.returncode is None:
            # Attached while another reader was still here, started after it
            # left the render finishing for the cache: take it back, so it is
            # not evicted under us.
            if _SID_DETACHED.get(full_key) is ent:
                _SID_DETACHED.pop(full_key, None)
            rd["detached"] = None
        streamed_all = False
        fd = handle.fd
        try:
            if h_lo < h_hi:
                yield header[h_lo:h_hi]
            magic = await asyncio.to_thread(os.pread, fd, 4, 0)
            if magic != b"RIFF":
                # One-time defensive check: sidplayfp really wrote a RIFF/WAVE
                # header of the length we skip.  If a future binary emits a
                # different container the offset-44 skip would desync — log it
                # rather than ship garbage silently.
                log.warning("progressive SID: temp header %r not RIFF for %s — "
                            "streaming anyway", magic, full_key)
            pos = d_lo
            idle = 0.0
            while pos < d_hi:
                want = min(_RANGE_STREAMING_CHUNK, d_hi - pos)
                buf = await asyncio.to_thread(os.pread, fd, want, _WAV_HEADER_LEN + pos)
                if buf:
                    yield buf
                    pos += len(buf)
                    idle = 0.0
                    continue
                # No new bytes right now.
                if proc.returncode is not None:
                    if proc.returncode != 0:
                        # The render died part-way (crash, kill): abort the
                        # body — the player gets an error and asks why —
                        # instead of padding the rest of the tune with silence
                        # as if it ended normally.  Nothing is cached (the
                        # finaliser's rc gate).
                        log.warning("progressive SID: render of %s failed (rc=%s) "
                                    "at %d/%d bytes — aborting the stream",
                                    full_key, proc.returncode, pos, d_hi)
                        # /render-status then says why (the player asks).
                        from soniqboom.core.conversion_cache import note_render_failure
                        _exc = _renderer_failure("SID", rd["binary"], proc.returncode, "")
                        note_render_failure(full_key, _exc.status_code, _exc.detail)
                        raise _RenderAborted(f"render failed after {pos} bytes")
                    # Render finished: pad any rounding shortfall so the declared
                    # Content-Length is satisfied exactly.
                    pad = d_hi - pos
                    while pad > 0:
                        n = min(_RANGE_STREAMING_CHUNK, pad)
                        yield b"\x00" * n
                        pad -= n
                    streamed_all = d_hi >= data_bytes
                    return
                await asyncio.sleep(_GROWING_POLL_INTERVAL)
                idle += _GROWING_POLL_INTERVAL
                if idle >= _GROWING_READ_TIMEOUT:
                    # Render stalled: not cacheable, not worth finishing.  The
                    # web UI gets the remainder as silence — a valid,
                    # declared-length WAV rather than a short read the <audio>
                    # element rejects; anyone else (Subsonic, DLNA, an encode
                    # for cast) an aborted body: never silence passed off as
                    # the tune, never an encode of it cached.
                    log.warning("progressive SID: no new bytes in %.0fs for %s — "
                                "%s at %d/%d", idle, full_key,
                                "padding to declared length" if web else "aborting",
                                pos, d_hi)
                    rd["stalled"] = True
                    if not web:
                        raise _RenderAborted(f"render stalled after {pos} bytes")
                    pad = d_hi - pos
                    while pad > 0:
                        n = min(_RANGE_STREAMING_CHUNK, pad)
                        yield b"\x00" * n
                        pad -= n
                    return
            streamed_all = d_hi >= data_bytes   # delivered the full range
        finally:
            # Sync-only cleanup (survives task cancellation): close our fd,
            # then — as the last reader — decide the render's fate.  The
            # detached finaliser reaps it and decides caching — never blocked
            # here.  (The finaliser may already have moved a finished temp into
            # the cache while we held the descriptor open; reads through it are
            # unaffected.)
            handle.close()
            rd["readers"] -= 1
            if streamed_all:
                rd["streamed_all"] = True
            # NB: when the render ALREADY exited (returncode set) there is
            # nothing to kill or detach — the finaliser's integrity gate
            # (rc 0 + full-length) caches a finished temp regardless of how
            # much the clients consumed.
            if (rd["readers"] == 0 and not rd["streamed_all"]
                    and proc.returncode is None and rd["detached"] is None):
                # The last listener skipped away mid-tune.  DETACH the healthy
                # render — let it finish and be cached — instead of killing it:
                # discarding an already-mostly-paid-for render meant a tune you
                # never played to the end could NEVER become warm (each replay
                # re-entered the same discard loop).  It keeps holding its pool
                # slot until its finaliser runs, so total sidplayfp processes
                # stay bounded by _sid_render_cap(); a live play can reclaim
                # the slot at admission (oldest-detached eviction), and a
                # listener coming back takes it back (``_sid_attach``).
                # Exceptions, all "this render is redundant, don't detach a
                # duplicate": a STALLED render isn't worth finishing; an older
                # detached render of this key is already finishing; or a NEWER
                # render for this key was admitted.
                superseded = _SID_PROG_INFLIGHT.get(full_key) != rd["gen"]
                if rd["stalled"] or full_key in _SID_DETACHED or superseded:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                else:
                    entry = {"proc": proc, "release": rd["release"],
                             "t": time.monotonic()}
                    _SID_DETACHED[full_key] = entry
                    rd["detached"] = entry
                    log.info("progressive SID: listener left — finishing %s in "
                             "the background for the cache", full_key)
            rd["wake"].set()        # the finaliser re-checks

    extra = _accel_off(dict(base_headers or {}))
    extra["Accept-Ranges"] = "bytes"
    extra["X-Stream-Mode"] = "sid-progressive"
    # We know the exact byte count we will deliver (the range is padded to fill
    # it), so always send a real Content-Length — WebKit/Safari media loading
    # prefers a known length over an open-ended chunked stream.  A Range request
    # gets 206 + Content-Range; a bare GET gets 200 with the full length.
    extra["Content-Length"] = str(end_excl - start)
    if is_range:
        extra["Content-Range"] = f"bytes {start}-{end_excl - 1}/{total}"
    # (A body aborted by a failed render still runs ``background``; the
    # descriptor of a body that never started is closed by it too.)
    return _CleanupStreamingResponse(
        _gen(), status_code=206 if is_range else 200, media_type="audio/wav",
        headers=extra,
        background=_compose_backgrounds(BackgroundTask(handle.close), background),
    )


# ── libgme rendering (NSF/SPC/GBS/VGM/AY/KSS/SAP/HES/GYM) — E-14 ─────────────

_GME_EXTS_STREAM = {".nsf", ".nsfe", ".spc", ".gbs", ".vgm", ".vgz",
                    ".ay", ".kss", ".sap", ".gym", ".hes"}


async def _render_gme(path: Path, subsong: int = 0, *,
                      live_key: str | None = None,
                      expected_seconds: float = 0.0) -> Path:
    """Render a libgme chiptune file to a temp WAV.

    Prefers an explicit ``gme`` CLI when configured.  Falls back to
    ffmpeg's built-in gme demuxer (``ffmpeg -i file.nsf -t N output.wav``)
    when the helper isn't available — that path works on standard Homebrew
    ffmpeg builds with libgme.

    ``live_key``: the in-process libgme render is a live render
    (``_LiveWav``), played while it runs — FM chips are slow to emulate (a
    3-minute Mega Drive VGM takes ~9 s).  ``expected_seconds``: the tune's
    stored length, when known."""
    duration = settings.sid_default_duration   # shares the chiptune default
    from soniqboom.core import gme_render
    loop = asyncio.get_event_loop()
    data = None
    unpacked = False                  # ``data`` is an inflated GYMX, not the file
    if path.suffix.lower() == ".gym":
        # A packed GYMX is inflated first (libgme refuses it), and a GYM —
        # a register dump with a known length — renders to its own end: the
        # cap is its length plus the 8 s fade, so the fade only ever touches
        # a loop played past the end, never the music.
        raw = await loop.run_in_executor(None, path.read_bytes)
        try:
            data = await loop.run_in_executor(None, gme_render.unpack_gym, raw)
        except ValueError as exc:
            log.info("GYM %s can't be unpacked: %s", path.name, exc)
            raise HTTPException(
                422, "This GYM file is packed, but its packed data is damaged.") from exc
        secs = await loop.run_in_executor(None, gme_render.gym_seconds, data)
        if secs:
            duration = min(3600, int(secs) + 1 + 8)
        unpacked = data is not raw
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    out = Path(tmp_wav.name)

    # Preferred path: in-process libgme via ctypes.  Homebrew ffmpeg ships
    # without --enable-libgme and there is no standalone gme CLI, so on a stock
    # macOS/Linux box this is the ONLY working renderer for NSF/SPC/GBS/... —
    # the CLI / ffmpeg branches below stay as fallbacks for hosts that have them.
    if gme_render.is_available():
        if data is None:
            data = await loop.run_in_executor(None, path.read_bytes)
        if live_key:
            try:
                got = await _render_gme_live(data, subsong, int(duration), live_key,
                                             expected_seconds)
            except BaseException:
                out.unlink(missing_ok=True)
                raise
            if got is not None:
                out.unlink(missing_ok=True)
                return got
        else:
            wav = await loop.run_in_executor(
                None, gme_render.render_wav, data, subsong, int(duration),
            )
            if wav:
                out.write_bytes(wav)
                return out
        log.info("libgme produced no audio for %s — trying gme CLI / ffmpeg", path.name)
    # The external renderers get the inflated GYMX too (they refuse a packed one).
    unpacked_tmp: Path | None = None
    if unpacked:
        fd, name = tempfile.mkstemp(suffix=".gym")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        unpacked_tmp = Path(name)
    try:
        return await _render_gme_external(unpacked_tmp or path, out, subsong, duration)
    finally:
        if unpacked_tmp is not None:
            unpacked_tmp.unlink(missing_ok=True)


async def _render_gme_live(data: bytes, subsong: int, duration: int, live_key: str,
                           expected_seconds: float) -> "Path | None":
    """The libgme render of ``_render_gme`` as a live render: ``render_pcm``
    runs in a worker thread and appends each block to a ``_LiveWav`` (its
    listeners told on the loop every 128 KB).  The finished file, or None
    when libgme produced nothing (the caller falls back to the CLI)."""
    from soniqboom.core import gme_render
    loop = asyncio.get_running_loop()
    lw = _LiveWav(live_key, expected_seconds=expected_seconds)
    stop = threading.Event()
    st = {"since": 0}

    def _sink(chunk: bytes):
        if stop.is_set():
            return False
        lw.write(chunk)
        st["since"] += len(chunk)
        if st["since"] >= 128 * 1024:
            n, st["since"] = st["since"], 0
            loop.call_soon_threadsafe(lw.account, n)
        return True

    fut = loop.run_in_executor(None, gme_render.render_pcm, data, subsong, duration, _sink)
    try:
        total = await asyncio.shield(fut)
        # The thread is done and its queued notices have run: the remainder.
        lw.account(st["since"])
        if total <= 0:
            lw.abandon()
            return None
        return lw.finish()
    except BaseException:
        # Cancelled: the render stops at its next block — wait for that, so
        # the file is never closed under the worker thread.
        stop.set()
        with contextlib.suppress(BaseException):
            await asyncio.wait({fut}, timeout=10)
        raise
    finally:
        lw.close()


async def _render_gme_external(path: Path, out: Path, subsong: int, duration) -> Path:
    """The ``gme`` CLI / ffmpeg-with-libgme fallback of ``_render_gme``."""
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


def _render_ident(path_str: str, track=None) -> tuple[str, bool]:
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
      * A uade PREFIX token never overrides an extension another engine owns
        (``One.wav``, ``P10.mp3``, ``UFO.XM``, ``ONE.IT``, C64 ``Fred.sid``)
        — see ``_uade_name_routes``.  ``track`` (optional) lets the scanner's
        content verdict keep a verified Amiga module on uade — also an Amiga
        SoundFactory module saved as ``.psf`` (no PSF magic; see
        ``_psf_has_magic``).
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
    if ext == ".psf" and (_track_is_amiga_module(track)
                          or _local_psf_is_amiga(path_str, track)):
        return ext, True
    return ext, _uade_name_routes(member, ext, track)


# Plain local ``.psf`` path (+ the record's mtime) → "no PSF magic" (an Amiga
# SoundFactory module), so a record scanned before content routing still
# routes to uade on the O(1) paths (HEAD, waveform, VU).  Bounded.
_PSF_AMIGA_MEMO: "OrderedDict[tuple, bool]" = OrderedDict()
_PSF_AMIGA_MEMO_MAX = 4096
_PSF_AMIGA_READING: set = set()     # keys whose read runs (in a daemon thread)
_PSF_AMIGA_LOCK = threading.Lock()
_PSF_AMIGA_READERS = threading.BoundedSemaphore(4)   # reads blocked on a mount at once
_PSF_AMIGA_WAIT_S = 0.25            # how long the loop waits for one read


def _psf_amiga_read(path_str: str) -> bool:
    """The file read behind ``_local_psf_is_amiga`` (blocking)."""
    p = Path(path_str)
    return p.is_file() and not _psf_has_magic(p)


def _psf_amiga_remember(key: tuple, got: bool) -> None:
    with _PSF_AMIGA_LOCK:
        _PSF_AMIGA_MEMO[key] = got
        while len(_PSF_AMIGA_MEMO) > _PSF_AMIGA_MEMO_MAX:
            _PSF_AMIGA_MEMO.popitem(last=False)


def _local_psf_is_amiga(path_str: str, track=None) -> bool:
    """Is the plain local ``.psf`` file at ``path_str`` without the PSF magic?
    A 3-byte read, remembered per path and record mtime; False for archive
    members and remote files (playback sniffs their local copy).  Never read
    on the event loop (a mount that stopped answering froze it on a HEAD /
    waveform / VU request): there the read runs in a daemon thread, waited
    for ``_PSF_AMIGA_WAIT_S`` — a disk that answers routes the first request
    right; one that doesn't counts as a PSF rip until its read is in (the
    play itself sniffs its file off the loop, ``stream_track``)."""
    if not path_str or "::" in path_str or "://" in path_str:
        return False
    key = (path_str, _tfield(track, "mtime"))
    got = _PSF_AMIGA_MEMO.get(key)
    if got is not None:
        return got
    try:
        asyncio.get_running_loop()
    except RuntimeError:                                # a worker thread: read here
        got = _psf_amiga_read(path_str)
        _psf_amiga_remember(key, got)
        return got
    with _PSF_AMIGA_LOCK:
        if key in _PSF_AMIGA_READING:
            return False                                # read already hanging
        if not _PSF_AMIGA_READERS.acquire(blocking=False):
            return False                                # enough reads hang already
        _PSF_AMIGA_READING.add(key)
    done = threading.Event()

    def _read() -> None:
        try:
            _psf_amiga_remember(key, _psf_amiga_read(path_str))
        except Exception:                               # noqa: BLE001 — counted as PSF
            pass
        finally:
            with _PSF_AMIGA_LOCK:
                _PSF_AMIGA_READING.discard(key)
            _PSF_AMIGA_READERS.release()
            done.set()
    try:
        threading.Thread(target=_read, daemon=True, name="psf-sniff").start()
    except BaseException:                               # (no thread: nothing read)
        with _PSF_AMIGA_LOCK:
            _PSF_AMIGA_READING.discard(key)
        _PSF_AMIGA_READERS.release()
        raise
    done.wait(_PSF_AMIGA_WAIT_S)
    return _PSF_AMIGA_MEMO.get(key, False)


def _track_is_amiga_module(track) -> bool:
    """Did the scanner verify this track's content as an Amiga module?
    (``uade123 -g`` accepted it — ``metadata._extract_uade`` stores the
    genre ``["Amiga", "Module"]``.)  Record fields only, no file IO."""
    if track is None:
        return False
    g = track.get("genre") if isinstance(track, dict) else getattr(track, "genre", None)
    if not g:
        return False
    if isinstance(g, str):
        g = [g]
    low = {str(x).strip().lower() for x in g}
    return "amiga" in low and "module" in low


def _uade_name_routes(member: str, ext: str, track=None) -> bool:
    """Does this (AdLib-free) member name send the file to uade?

    A uade name match (``mdat.song``, ``song.fc13``) routes — except that a
    PREFIX token never overrides an extension another engine owns, the
    scanner's rule too ("core, non-uade extensions win"): ``One.wav`` /
    ``P10.mp3`` are audio, ``ONE.IT`` / ``UFO.XM`` tracker modules, a C64
    ``Fred.sid`` goes by its PSID magic.  The one exception is a track the
    scanner verified as an Amiga module (``bp.song.mod``, a SidMon
    ``fred.sid``) — only for a tracker extension or ``.sid``, never plain
    audio.  O(1), no file IO."""
    if _uade_formats.classify(member) is None:
        return False
    if ext in _UADE_EXTS or not _uade_formats.ext_owned_elsewhere(ext):
        return True
    return (_uade_formats.owned_ext_can_be_amiga(ext)
            and _track_is_amiga_module(track))
_ADLIB_DEFAULT_TIMEOUT_S = 8 * 60
# The length the scan stores for every AdLib tune (it can't know one).
from soniqboom.core.metadata import _ADLIB_DEFAULT_DURATION as _ADLIB_PLACEHOLDER_S
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


async def _render_adlib_one(binary: str, path: Path, subsong: int,
                            live_key: str | None = None, *,
                            expected_seconds: float = 0.0,
                            min_audible_seconds: float = 0.0) -> Path:
    """Render a single AdLib/OPL subsong to a fresh temp WAV (no audio check).
    ``live_key``: as a live render, copied into a live WAV as adplay writes it
    (``_tail_render``, which takes ``expected_seconds`` / ``min_audible_seconds``)."""
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    cmd = [binary, "-O", "disk", "-d", tmp_wav.name,
           "-e", _ADLIB_OPL_EMULATOR, "-f", "44100", "--stereo"]
    if subsong > 0:
        cmd += ["-s", str(subsong)]      # multi-song AdLib formats (RAD, .adl, …)
    cmd.append(str(path))
    if live_key:
        return await _tail_render(cmd, Path(tmp_wav.name), live_key, kind="adlib",
                                  timeout=_ADLIB_DEFAULT_TIMEOUT_S, require_audio=False,
                                  expected_seconds=expected_seconds,
                                  min_audible_seconds=min_audible_seconds)
    await _await_renderer(
        cmd, Path(tmp_wav.name),
        timeout=_ADLIB_DEFAULT_TIMEOUT_S, kind="adlib",
        require_audio=False,    # ``_render_adlib`` tells a missing bank from a corrupt file
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


async def _render_adlib(path: Path, subsong: int = 0, *,
                        live_key: str | None = None,
                        expected_seconds: float = 0.0) -> Path:
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

    ``live_key``: the asked subsong renders as a live render (``_LiveWav``,
    the Nuked OPL3 core needs ~2 s for 3 minutes), played while adplay
    writes it once it proves audible; a subsong that turns out empty stops
    being offered and the probe of the next ones runs as before (no
    listener is attached to silence).  ``expected_seconds``: the tune's
    stored length, when known (the header is then exact from byte 0).
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
        # Unpinned (subsong 0), a split-second stub is not the music: no
        # listener before ``_ADLIB_MIN_AUDIO_S`` of sound (``_wav_is_audible``).
        out = await _render_adlib_one(
            binary, render_path, subsong, live_key, expected_seconds=expected_seconds,
            min_audible_seconds=_ADLIB_MIN_AUDIO_S if subsong == 0 else 0.0)
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


async def _render_imf(path: Path, subsong: int = 0, *,
                      live_key: str | None = None,
                      expected_seconds: float = 0.0) -> Path:
    """Render a ``.imf`` file, disambiguating the overloaded extension.

    Two unrelated formats share ``.imf``:
      * **Imago Orpheus** — a PC tracker module (decoded by openmpt123).
      * **id Software / Apogee IMF** — an OPL2 FM register dump (Wolfenstein 3D,
        Commander Keen, Duke Nukem …) decoded by AdPlug.

    Imago Orpheus carries an ``IM10`` signature at offset 0x3C (60); id IMF does
    not — so we read that signature and route to the right renderer.
    ``live_key`` / ``expected_seconds`` go to the AdLib render (openmpt123
    renders a module in a fraction of a second — never live).
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
    return await _render_adlib(path, subsong=subsong,          # id/Apogee AdLib IMF
                               live_key=live_key, expected_seconds=expected_seconds)


async def _backfill_rendered_duration(track_id: str, track, wav_path,
                                      placeholder: float | None = None, *,
                                      subsong: int = 0,
                                      authoritative: bool = False,
                                      sink: "list | None" = None) -> float | None:
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

    ``subsong`` (the tune index rendered): the track's duration describes its
    DEFAULT tune (``default_tune_index``), so a render of any other tune is
    never written (returns None).  ``authoritative`` (uade:
    the render is the only length source) also corrects an already-stored value
    that is off by ≥ 0.5 s.  ``sink``: collect ``(track_id, fields)`` instead of
    writing, so a batch caller can write them in one store update.
    Returns the real length in seconds, or None.  Best-effort — a duration
    cosmetic must never break playback.
    """
    try:
        if track is None or subsong != default_tune_index(track_id, track):
            return None
        if placeholder is None:
            from soniqboom.core.metadata import _ADLIB_DEFAULT_DURATION
            placeholder = float(_ADLIB_DEFAULT_DURATION)
        meta = track.__dict__ if hasattr(track, "__dict__") else {}
        stored = float(meta.get("duration") or 0)
        if (not authoritative and stored > 0
                and abs(stored - float(placeholder)) > 0.01):
            return stored  # already carries a real, non-placeholder duration
        real = round(_wav_audio_seconds(wav_path), 2)
        if real <= 0:
            return None
        if abs(real - stored) >= 0.5:
            if sink is not None:
                sink.append((track_id, {"duration": real}))
            else:
                from soniqboom.core.store import get_store
                get_store().update_track_fields(track_id, {"duration": real})
            log.debug("Rendered duration backfilled: %s -> %.2fs", track_id, real)
        return real
    except Exception:
        return None


async def _resolve_adlib_local_path(track_id: str, path_str: str, *,
                                    lane: str = "stream",
                                    uade: bool | None = None) -> Path | None:
    """Resolve a (possibly remote, possibly zip-member) AdLib track to a local
    renderable file path — the subset of ``stream_track``'s resolution that
    AdLib tunes need.  AdLib files are tiny, so fetching a remote one purely to
    probe its length is cheap (unlike big media, which prewarm deliberately
    skips).  Returns the local Path, or None if it couldn't be resolved.
    ``lane`` is the remote-read priority lane of every fetch (the duration
    probe passes ``"scan"``, never the lane a listener's play waits on).
    ``uade`` is ``_render_ident``'s verdict for the track (a loose remote
    Amiga module is fetched with its companion halves); None decides by name.
    """
    loop = asyncio.get_running_loop()
    is_remote = is_remote_path(path_str)
    if is_remote and "::" in path_str:
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        zip_rel, member = remote_path.split("::", 1)
        source = get_source(scan_root)
        local_zip = await loop.run_in_executor(
            None, functools.partial(
                _remote_zip.archive_for_member, scan_root, zip_rel, member, source,
                lane=lane, companion=_archive_companion_filter(member)),
        )
        return await _get_or_extract_zip_member(
            f"{local_zip}::{member}", track_id,
            bank_fallback=_make_zip_bank_fallback(remote=(zip_rel, source), lane=lane),
        )
    if is_remote:
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        source = get_source(scan_root)
        # uade Amiga modules: fetch module + companion halves into one dir.
        if uade is None:
            uade = _render_ident(path_str)[1]
        if source is not None and uade:
            mat = await _materialize_loose_remote_uade(
                track_id, scan_root, remote_path, source, lane=lane,
            )
            if mat is not None:
                return mat
        globs = _ADLIB_COMPANION_GLOBS.get(Path(remote_path).suffix.lower())
        if globs and source is not None:
            mat = await _materialize_loose_remote_adlib(
                track_id, scan_root, remote_path, source, globs, lane=lane,
            )
            if mat is not None:
                return mat
        local = await loop.run_in_executor(
            None, functools.partial(get_cache().fetch, scan_root, remote_path, source,
                                    lane=lane),
        )
        return Path(local) if local else None
    if "::" in path_str:
        return await _get_or_extract_zip_member(
            path_str, track_id,
            bank_fallback=_make_zip_bank_fallback(local_zip=path_str.split("::")[0]),
        )
    return Path(path_str)


async def _materialize_loose_remote_uade(
    track_id: str, scan_root: str, remote_path: str, source, *,
    lane: str = "stream",
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
    marker = out_dir / ".loose_marker"

    def _fresh() -> bool:
        return (tune_out.exists() and marker.exists()
                and marker.read_text() == marker_val)

    async with lock:
        if _fresh():
            _register_adlib_extract(track_id, out_dir)   # same budget/LRU pool
            return tune_out

    def _fetch() -> "tuple[Path, list[tuple[str, Path]]] | None":
        # The network part runs WITHOUT the extract lock: a play must never
        # queue behind a prewarm's (scan-lane) download of the same tune.
        # The remote cache dedupes the downloads themselves.
        from soniqboom.core.remote_cache import get_cache
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
        try:
            local = get_cache().fetch(scan_root, remote_path, source, lane=lane)
        except Exception:
            return None
        got = []
        for base, sib_path in sibs:
            try:
                got.append((base, get_cache().fetch(scan_root, sib_path, source,
                                                    lane=lane)))
            except Exception:
                continue
        return local, got

    fetched = await loop.run_in_executor(None, _fetch)
    if fetched is None:
        return None
    async with lock:
        if _fresh():                  # another caller installed it meanwhile
            _register_adlib_extract(track_id, out_dir)
            return tune_out

        def _install() -> Path | None:
            import shutil
            local, got = fetched
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(local, tune_out)
            except Exception:
                shutil.rmtree(out_dir, ignore_errors=True)
                return None
            for base, lp in got:
                try:
                    shutil.copyfile(lp, out_dir / base)
                except Exception:
                    continue
            marker.write_text(marker_val)
            return tune_out

        tune = await loop.run_in_executor(None, _install)
        if tune is not None:
            _register_adlib_extract(track_id, out_dir)
            try:
                await asyncio.to_thread(_zip_evict_until_under_budget)
            except Exception:
                log.exception("UADE loose-remote eviction failed")
        return tune


async def _materialize_loose_remote_adlib(
    track_id: str, scan_root: str, remote_path: str, source, globs, *,
    lane: str = "stream",
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
    marker = out_dir / ".loose_marker"

    def _fresh() -> bool:
        return (tune_out.exists() and marker.exists()
                and marker.read_text() == marker_val)

    async with lock:
        if _fresh():
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

    def _fetch() -> "tuple[bytes, list[tuple[str, bytes]]] | None":
        # Network reads WITHOUT the extract lock (AdLib tunes and banks are
        # tiny): a play must never queue behind a prewarm's (scan-lane) read.
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
        tune_bytes = source.read_file(remote_path, lane=lane)
        got = []
        for base, sib_remote in sibs:
            try:
                got.append((base, source.read_file(sib_remote, lane=lane)))
            except Exception:
                continue
        return tune_bytes, got

    fetched = await loop.run_in_executor(None, _fetch)
    if fetched is None:
        return None
    async with lock:
        if _fresh():                  # another caller installed it meanwhile
            _register_adlib_extract(track_id, out_dir)
            return tune_out

        def _install() -> Path:
            import shutil
            tune_bytes, got = fetched
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            tune_out.write_bytes(tune_bytes)
            for base, data in got:
                try:
                    (out_dir / base).write_bytes(data)
                except Exception:
                    continue
            marker.write_text(marker_val)
            return tune_out

        tune = await loop.run_in_executor(None, _install)
        if tune is not None:
            _register_adlib_extract(track_id, out_dir)
            try:
                await asyncio.to_thread(_zip_evict_until_under_budget)
            except Exception:
                log.exception("AdLib loose-extract eviction failed")
        return tune


# A uade duration probe renders the whole tune at full quality anyway, so it
# keeps that render in the conversion cache (the first play of a probed row is
# then an instant hit, and a click DURING the probe attaches to the live
# render instead of starting a second one) — while the cache has room: past
# this fraction of ``conversion_cache_max_bytes`` it stays a throw-away probe,
# so browsing a big folder can't flush the renders people actually play.
_UADE_PROBE_KEEP_FRACTION = 0.5
_UADE_PROBE_EST_BYTES = 64 * 1024 * 1024      # ≈ 6 min of 44.1 kHz stereo WAV


def _uade_probe_can_keep() -> bool:
    from soniqboom.core import conversion_cache as _cc
    try:
        limit = int(settings.conversion_cache_max_bytes) * _UADE_PROBE_KEEP_FRACTION
    except (TypeError, ValueError):
        return False
    return _cc._total_bytes + _UADE_PROBE_EST_BYTES < limit


async def _probe_one_rendered_duration(track_id: str, *,
                                       sink: "list | None" = None,
                                       allow_full_render: "bool | None" = None,
                                       ) -> float | None:
    """Render a render-only tune once, JUST to learn its length, persist it, and
    return it.  Covers AdLib/id-IMF, GME chiptunes (NSF/SPC/GBS/VGM/AY/KSS/…),
    HVL, PSF, SC68 and the uade (Amiga) family.  The WAV is thrown away (a
    probe, not a play, so it never pollutes the conversion cache with multi-MB
    renders) — except for uade while the cache has headroom and "Prepare
    upcoming tracks" (``render_prewarm``) is on, where the render IS the
    play's render (see ``_UADE_PROBE_KEEP_FRACTION``), registered at the
    eviction end of the cache so it never pushes out a played render.
    Returns None for other formats, already-known durations, or render
    failures (e.g. a missing companion bank → 422, which just leaves the
    placeholder in place).  ``sink`` collects the duration write for a
    batched store update.

    ``allow_full_render`` (default: the ``render_prewarm`` setting) False
    skips the families whose length only a full render can tell — Amiga
    (uade, incl. SidMon ``.sid``), HVL, SC68, PSF (incl. Dreamcast ``.dsf``):
    they return None and learn their length on first play.

    The source is resolved BEFORE a background render slot is taken — a
    download must never sit on one — and remote reads go on the ``"scan"``
    lane, one probe download per share at a time; a remote archive member is
    probed only when its archive is already cached locally (a row's length is
    not worth pulling a whole archive across the share).  Only the renderer
    run holds the ``PRIO_PROBE`` slot.
    """
    track = await get_track(track_id)
    if track is None:
        return None
    path_str = getattr(track, "path", "") or ""
    ext, _uade_named = _render_ident(path_str, track)
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
    if allow_full_render is None:
        allow_full_render = _prewarm_enabled()
    if not allow_full_render and (is_uade or is_hvl or is_sc68 or is_psf
                                  or maybe_sidmon or maybe_dreamcast):
        return None        # low-power: no full renders just to learn a length
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
    share_gate = _remote_prewarm_gate(path_str)
    if share_gate is not None and "::" in path_str and not _remote_bytes_local(path_str):
        return None           # never fetch a whole remote archive for one length
    wav = None
    key = None
    pinned = False
    try:
        try:
            if share_gate is not None:
                # One probe download per share; a cancelled probe keeps the
                # share's turn until its (unstoppable) download ends.
                async with share_gate:
                    local = await _hold_until_done(_resolve_adlib_local_path(
                        track_id, path_str, lane="scan", uade=_uade_named))
            else:
                local = await _resolve_adlib_local_path(track_id, path_str, lane="scan",
                                                        uade=_uade_named)
        except Exception:
            return None
        if local is None:
            return None
        if "::" in path_str or share_gate is not None:
            _zip_pin(track_id)    # the extract / materialized dir outlives the render
            pinned = True
        if maybe_sidmon:
            if _is_c64_sid(local):
                return None       # real C64 — HVSC owns those durations
            is_uade = True
        if maybe_dreamcast and not is_psf:
            if not _dsf_is_dreamcast(local):
                return None       # Sony DSD stream — not render-only
            is_psf = True
        if is_psf and ext == ".psf" and not _psf_has_magic(local):
            is_psf, is_uade = False, True     # Amiga SoundFactory module
        # ``keep``: rendered under the PLAY's key and as a live render — a
        # click meanwhile attaches (and streams it), the first play after is a
        # cache hit.  Kept alive, so a cancelled probe request can't fail a
        # play already waiting on this render — the probe then keeps its slot
        # until the render ends.  ``cold``: a probe is not a play — the new
        # entry sits at the eviction end until someone plays it.  Pinned until
        # its length is read, so that cold slot can't be evicted before the
        # backfill reads it.
        keep = is_uade and allow_full_render and _uade_probe_can_keep()
        async with _bg_render_sem.slot(PRIO_PROBE):
            # The length stored is the DEFAULT tune's: a multi-tune file's
            # first tune that isn't empty (probed once, ``ensure_default_tune``).
            fam = "gme" if is_gme else "sc68" if is_sc68 else "uade" if is_uade else None
            track = await _bare_play_track(track_id, track, local, fam)
            d = default_tune_index(track_id, track)
            base = await _uade_resolve_base(track_id, track, local, d) if is_uade else 0
            if keep:
                from soniqboom.core.conversion_cache import pin
                key = uade_cache_key(track_id, d, base)
                pin(key)
            if is_gme:
                wav = await _render_gme(local, subsong=d)
            elif ext == ".imf":
                wav = await _render_imf(local)
            elif is_hvl:
                wav = await _render_hvl(local)
            elif is_psf:
                wav = await _render_psf(local)
            elif is_sc68:
                wav = await _render_sc68(local, subsong=d)
            elif keep:
                from soniqboom.core.conversion_cache import get_or_render
                task = asyncio.ensure_future(get_or_render(
                    track_id=track_id, format_type="uade", subsong=d,
                    render_fn=lambda: _render_uade(local, subsong=d, live_key=key,
                                                   subsong_base=base),
                    variant=uade_cache_variant(d, base), cold=True))
                task.add_done_callback(lambda f: f.cancelled() or f.exception())
                _bg_keep(task)
                cached_path, _hit = await _hold_until_done(task)
            elif is_uade:
                wav = await _render_uade(local, subsong=d, with_vu=False,
                                         subsong_base=base)
            else:
                wav = await _render_adlib(local)
        # Reading the length and the store write happen outside the slot.
        if keep:
            return await _backfill_rendered_duration(
                track_id, track, cached_path, placeholder, subsong=d,
                authoritative=True, sink=sink)
        return await _backfill_rendered_duration(track_id, track, wav, placeholder,
                                                 subsong=d, sink=sink)
    except HTTPException:
        return None      # undecodable / missing bank — leave the placeholder
    except Exception:
        return None
    finally:
        if key is not None:
            from soniqboom.core.conversion_cache import unpin
            unpin(key)
        if pinned:
            try:
                _zip_unpin(track_id)
            except Exception:
                pass
        if wav is not None:
            try:
                Path(wav).unlink()
            except OSError:
                pass


# ── MIDI rendering ────────────────────────────────────────────────────────────

async def _render_midi(path: Path, *, live_key: str | None = None) -> Path:
    """Render MIDI file to a temp WAV via FluidSynth and return the path.

    ``live_key``: a live render (``_LiveWav``) — fluidsynth writes its WAV
    as it renders (first audio ~0.2 s in, a 3-minute song in ~1.5 s), and
    the growing file plays meanwhile.  Its length is unknown until the end
    (the song plus the instruments' release tail)."""
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

    if live_key:
        return await _tail_render(cmd, Path(tmp_wav.name), live_key,
                                  kind="MIDI", timeout=600)
    await _await_renderer(cmd, Path(tmp_wav.name), timeout=600, kind="MIDI")
    return Path(tmp_wav.name)


# ── Tracker module rendering ─────────────────────────────────────────────────

def _old_med(head: bytes) -> bool:
    """MED 2 / 3 / 4 (``MED\\x02``–``MED\\x04``): the pre-OctaMED format
    libopenmpt can't read (it reads MMD0–MMD3) — zxtune plays it."""
    return head[:3] == b"MED" and head[3:4] in (b"\x02", b"\x03", b"\x04")


# An early Digital Sound Interface Kit module (``DSM\x10``): no installed
# engine reads it (libopenmpt, uade, zxtune all refuse it).
_DSM_EARLY_MAGIC = b"DSM\x10"
_DSM_EARLY_DETAIL = ("This Digital Sound Interface Kit module is an early DSM variant "
                     "that none of the installed players can read.")


async def _render_tracker(path: Path, subsong: int = 0) -> Path:
    """Render tracker module to a temp WAV via openmpt123 and return the path.

    Routed by content first: an old MED module (``_old_med``) renders through
    zxtune (``_render_zxtune_capped``); an early DSIK module is a 422 naming
    it (``_DSM_EARLY_DETAIL``).

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
    def _head() -> bytes:
        try:
            with open(path, "rb") as fh:
                return fh.read(4)
        except OSError:
            return b""
    head = await asyncio.to_thread(_head)
    if head == _DSM_EARLY_MAGIC:
        raise HTTPException(422, _DSM_EARLY_DETAIL)
    if _old_med(head):
        return await _render_zxtune_capped(path, kind="MED")
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


# zxtune123 has no length option, and its loop detection can run away (an
# old MED module it renders as 1:03:40 of looping, written at ~360 MB/s):
# the render is cut at the chiptune default length (``sid_default_duration``)
# with an 8 s fade, like libgme's capped tunes.  A module that ends earlier
# keeps its own end.
_ZXTUNE_POLL_S = 0.02
_ZXTUNE_FADE_S = 8
_ZXTUNE_WALL_S = 120.0


def _cap_and_fade_wav(wav: Path, data_bytes: int, fade_s: int) -> None:
    """Cut the 44.1 kHz stereo s16 WAV ``wav`` (a canonical 44-byte header)
    to ``data_bytes`` of audio, fade its last ``fade_s`` seconds out and
    rewrite the header sizes.  Blocking."""
    import array as _array
    import sys
    frame = 4
    data_bytes -= data_bytes % frame
    with open(wav, "r+b") as fh:
        fh.truncate(_WAV_HEADER_LEN + data_bytes)
        n = min(data_bytes, fade_s * 44100 * frame)
        if n:
            fh.seek(_WAV_HEADER_LEN + data_bytes - n)
            a = _array.array("h")
            a.frombytes(fh.read(n))
            if sys.byteorder == "big":
                a.byteswap()
            frames = len(a) // 2
            for i in range(frames):
                g = (frames - i) / frames
                a[2 * i] = int(a[2 * i] * g)
                a[2 * i + 1] = int(a[2 * i + 1] * g)
            if sys.byteorder == "big":
                a.byteswap()
            fh.seek(_WAV_HEADER_LEN + data_bytes - n)
            fh.write(a.tobytes())
        fh.seek(0)
        fh.write(_build_wav_header(44100, 2, data_bytes // frame, bits_per_sample=16))


async def _render_zxtune_capped(path: Path, *, kind: str) -> Path:
    """Render ``path`` with zxtune123 to a temp WAV, cut at
    ``sid_default_duration`` seconds (see above).  zxtune writes the file
    itself (it refuses an existing one, so the temp name is freed first);
    the size is watched and the process killed once the cap is reached."""
    binary = _find_renderer(settings.zxtune123_path, "zxtune123")
    if not binary:
        raise HTTPException(501, f"zxtune123 not installed — {kind} modules require it. "
                                 "Re-run install.sh, or set renderers.zxtune123_path.")
    cap_s = max(5, min(int(settings.sid_default_duration), 3600))
    cap_bytes = cap_s * 44100 * 4
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    out = Path(tmp_wav.name)
    out.unlink(missing_ok=True)
    capped = ok = False
    try:
        async with _render_sem:
            proc = await forksafe.spawn(
                binary, "--silent", "--wav", f"filename={out}", str(path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + _ZXTUNE_WALL_S
                while proc.returncode is None:
                    try:
                        size = out.stat().st_size
                    except OSError:
                        size = 0
                    if size >= _WAV_HEADER_LEN + cap_bytes:
                        capped = True
                        break
                    if time.monotonic() > deadline:
                        raise HTTPException(504, f"{kind} render timed out")
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=_ZXTUNE_POLL_S)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except Exception:
                        pass
        if capped:
            await asyncio.to_thread(_cap_and_fade_wav, out, cap_bytes, _ZXTUNE_FADE_S)
        elif proc.returncode != 0:
            raise HTTPException(502, f"{kind} renderer exited with status {proc.returncode}")
        if not _render_has_audio(out):
            raise HTTPException(422, "The renderer finished but produced no audio for this file.")
        ok = True
        return out
    finally:
        if not ok:
            out.unlink(missing_ok=True)


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
from soniqboom.core import xpk as _xpk

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


# Live (still-rendering) WAVs, keyed by conversion-cache key — uade's (PCM
# from a pipe) and every other renderer ``_LiveWav`` follows (fluidsynth,
# adplay, psgplay, zxtune, libgme).  Their first audio is on disk ~0.1 s in,
# so while a render runs its growing temp WAV can already be played — see
# ``_serve_live``.
#   {"path", "expected_size" (0 = length unknown), "exact" (the renderer was
#    told that length and produces it), "complete" Event, "data" Event (set
#    on the first bytes, the first audible ones, then every ~256 KB, and at
#    the end), "clean_exit" bool|None, "bytes", "audible", "bytes_per_sec"}
_UADE_LIVE: dict[str, dict] = {}
# Set (and dropped) when a render registers the live entry a waiting
# ``_await_uade_live`` asked for.
_UADE_LIVE_REGISTERED: dict[str, asyncio.Event] = {}
_UADE_RATE, _UADE_CH, _UADE_BPS = 44100, 2, 2          # uade123 -c: s16le stereo 44.1 kHz
_UADE_BYTES_PER_SEC = _UADE_RATE * _UADE_CH * _UADE_BPS
# Live renders that finished and were cached, newest last: key → bytes per
# second of their WAV, so /render-status can give a finished render's exact
# length (``duration_seconds``).  Bounded.
_LIVE_FINISHED: "OrderedDict[str, int]" = OrderedDict()
_LIVE_FINISHED_MAX = 256


class _LiveWav:
    """The canonical WAV a render writes while a listener may already play it.

    Our own 44-byte 16-bit PCM header comes first: exact when a length is
    known (``expected_seconds``; ``exact`` when the renderer itself was told
    that length and produces it, so any client may stream it — otherwise a
    stored length the render normally matches, which only the web player,
    which corrects a wrong one, is given), else — for a live render — the
    "read to the end" streaming header.  ``feed`` (or ``write`` from a worker
    thread, then ``account`` on the loop) appends PCM; with a ``live_key``
    the file is registered in ``_UADE_LIVE`` and ``audible`` is set once the
    PCM holds sound (``core.silence``) — a listener is attached only then, so
    a tune that plays only silence ends as the cache's 422, never streamed.
    ``finish`` aligns a render that lands within a second of a promised
    length to it exactly — so the cached file is byte for byte the resource
    a live reader was told about — and writes the real header; ``close``
    settles the live entry and, unless the render finished, deletes the
    file."""

    def __init__(self, live_key: "str | None", *, rate: int = 44100,
                 channels: int = 2, expected_seconds: float = 0.0,
                 exact: bool = False, min_audible_seconds: float = 0.0) -> None:
        self.key = live_key
        self.rate, self.channels = int(rate), int(channels)
        self.frame_bytes = 2 * self.channels
        self.bytes_per_sec = self.rate * self.frame_bytes
        try:
            secs = float(expected_seconds or 0)
        except (TypeError, ValueError):
            secs = 0.0
        if not (math.isfinite(secs) and 0 < secs <= _UADE_MAX_EXPECTED_S):
            secs = 0.0               # no length (or an absurd one): unknown
        self.exp_frames = (int(round(secs * self.rate))
                           if secs > (0 if exact else 1) else 0)
        self.expected_size = (_WAV_HEADER_LEN + self.exp_frames * self.frame_bytes
                              if self.exp_frames else 0)
        # ``min_audible_seconds``: sound counts only once this much audio is
        # in (an AdLib default subsong of a split second is a stub, not the
        # music — ``_render_adlib``).
        self._min_audible_bytes = int(max(0.0, min_audible_seconds) * self.bytes_per_sec)
        self.written = 0
        self.clean = False
        self._since = 0
        self._heard = False
        self.live = None
        self._meter = None
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        self.path = Path(tmp.name)
        try:
            with open(self.path, "wb") as hf:
                if self.exp_frames or not live_key:
                    hf.write(_build_wav_header(self.rate, self.channels, self.exp_frames,
                                               bits_per_sample=16))
                else:
                    # Length unknown but a listener may stream this file while
                    # it grows: RIFF and data sizes 0xFFFFFFFF = play to the
                    # end of the stream (a 0-frame header would say there is
                    # no audio).
                    hf.write(_streaming_wav_header(self.rate, self.channels, 16))
            self._fh = open(self.path, "ab")
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise
        if live_key:
            # Registered last: nothing below can fail, so an entry never
            # outlives a half-made file (the renderer's ``close`` settles it).
            self._meter = _AudibilityMeter(self.channels, self.rate)
            self.live = {"path": self.path, "expected_size": self.expected_size,
                         "exact": bool(exact and self.exp_frames),
                         "complete": asyncio.Event(), "data": asyncio.Event(),
                         "clean_exit": None, "bytes": 0, "no_pad_on_failure": True,
                         "subscribers": 0, "audible": False,
                         "bytes_per_sec": self.bytes_per_sec,
                         "rate": self.rate, "channels": self.channels}
            _UADE_LIVE[live_key] = self.live
            reg = _UADE_LIVE_REGISTERED.pop(live_key, None)
            if reg is not None:
                reg.set()

    def _wake(self) -> None:
        if self.live is not None:
            ev = self.live["data"]
            ev.set()
            ev.clear()

    def write(self, chunk: bytes) -> None:
        """Append PCM and judge it (blocking — one thread at a time, any
        thread; ``account`` it on the loop afterwards)."""
        self._fh.write(chunk)
        self._fh.flush()
        self.written += len(chunk)
        if self._meter is not None and not self._heard and self._meter.feed(chunk):
            self._heard = True

    def account(self, nbytes: int) -> None:
        """Tell listeners about ``nbytes`` of PCM ``write`` appended (event
        loop only): wake them on the first audio and the first audible audio
        (a waiting first play answers at once), then every 256 KB."""
        if self.live is None or nbytes <= 0:
            return
        first = self.live["bytes"] == 0
        self.live["bytes"] += nbytes
        self._since += nbytes
        heard = False
        if (self._heard and not self.live["audible"]
                and self.live["bytes"] >= self._min_audible_bytes):
            self.live["audible"] = heard = True
        if heard or first or self._since >= 256 * 1024:
            self._since = 0
            self._wake()

    def feed(self, chunk: bytes) -> None:
        """``write`` + ``account`` (event loop)."""
        self.write(chunk)
        self.account(len(chunk))

    def finish(self) -> Path:
        """The render ended cleanly: align it to a promised length it lands
        within a second of, write the real header, and return the file."""
        self._fh.close()
        written = self.written
        fb = self.frame_bytes
        if self.exp_frames:
            exp_bytes = self.exp_frames * fb
            if 0 < abs(written - exp_bytes) <= self.bytes_per_sec:
                with open(self.path, "r+b") as hf:
                    if written > exp_bytes:
                        hf.truncate(_WAV_HEADER_LEN + exp_bytes)
                    else:
                        hf.seek(_WAV_HEADER_LEN + written)
                        hf.write(b"\x00" * (exp_bytes - written))
                written = exp_bytes
        frames = written // fb
        with open(self.path, "r+b") as hf:
            if frames * fb != written:
                hf.truncate(_WAV_HEADER_LEN + frames * fb)
            hf.seek(0)
            hf.write(_build_wav_header(self.rate, self.channels, frames,
                                       bits_per_sample=16))
        self.written = frames * fb
        if self.live is not None:
            self.live["bytes"] = self.written
        self.clean = True
        return self.path

    def abandon(self) -> None:
        """Stop offering this file to listeners (nobody is reading it: it
        never became audible) while the render goes on another way."""
        if self.live is not None:
            self.live["clean_exit"] = False
            self.live["complete"].set()
            self.live["data"].set()
            if _UADE_LIVE.get(self.key) is self.live:
                _UADE_LIVE.pop(self.key, None)
            self.live = None

    def close(self) -> None:
        """Settle the live entry (complete; dropped now when the render
        failed, once the cache holds the file when it finished) and delete
        the file of a render that didn't finish.  Idempotent."""
        try:
            self._fh.close()
        except Exception:
            pass
        if self.live is not None:
            live, self.live = self.live, None
            live["clean_exit"] = self.clean
            live["complete"].set()
            live["data"].set()
            if not self.clean:
                if _UADE_LIVE.get(self.key) is live:
                    _UADE_LIVE.pop(self.key, None)
            else:
                _LIVE_FINISHED[self.key] = self.bytes_per_sec
                _LIVE_FINISHED.move_to_end(self.key)
                while len(_LIVE_FINISHED) > _LIVE_FINISHED_MAX:
                    _LIVE_FINISHED.popitem(last=False)
                _drop_live_after_store(self.key, live)
        if not self.clean:
            self.path.unlink(missing_ok=True)


# How often ``_tail_render`` looks for new output, and how much of the
# renderer's growing file it holds back while the renderer runs: a writer may
# append a trailing chunk (zxtune's 128-byte tag block) as it closes, before
# it patches its header's data size — one of up to this size is never copied
# as audio (after the exit the patched data size bounds the copy).
_TAIL_POLL_S = 0.02
_TAIL_HOLDBACK = 8192


def _tail_fmt(head: bytes) -> "tuple[dict, int] | None":
    """``({"tag", "channels", "rate", "bits"}, data_offset)`` from the start
    of a renderer's RIFF/WAVE output, or None until its data chunk header is
    in (a header may still be being written)."""
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos, fmt = 12, None
    while pos + 8 <= len(head):
        cid = head[pos:pos + 4]
        size = int.from_bytes(head[pos + 4:pos + 8], "little")
        body = pos + 8
        if cid == b"fmt " and body + 16 <= len(head):
            tag = int.from_bytes(head[body:body + 2], "little")
            if tag == 0xFFFE and size >= 26 and body + 26 <= len(head):
                tag = int.from_bytes(head[body + 24:body + 26], "little")
            fmt = {"tag": tag,
                   "channels": int.from_bytes(head[body + 2:body + 4], "little"),
                   "rate": int.from_bytes(head[body + 4:body + 8], "little"),
                   "bits": int.from_bytes(head[body + 14:body + 16], "little")}
        elif cid == b"data":
            return (fmt, body) if fmt is not None else None
        pos = body + size + (size & 1)
    return None


async def _tail_render(cmd: list[str], src: Path, live_key: str, *, kind: str,
                       timeout: float, require_audio: bool = True,
                       env: "dict | None" = None, expected_seconds: float = 0.0,
                       exact: bool = False, min_audible_seconds: float = 0.0) -> Path:
    """Run renderer ``cmd``, which writes a 16-bit PCM WAV to ``src`` as it
    renders, and copy its audio into a live WAV (``_LiveWav`` registered
    under ``live_key``, made with ``expected_seconds`` / ``exact`` /
    ``min_audible_seconds``) while it does — polled every ``_TAIL_POLL_S`` —
    so a listener can play the render before it ends.

    Returns the live WAV's finished file (``src`` is deleted).  A renderer whose
    output isn't what ``live`` declares (another rate / channel count /
    sample format) is never offered live: the render completes and its own
    file is returned, exactly as without a listener.  A non-zero exit is the
    usual ``_renderer_failure``; an exit 0 without audio a 422 (unless
    ``require_audio`` is False: the caller judges the empty result).  On any
    failure ``src`` and the live file are deleted and the live entry says
    the render failed (a listener's stream is aborted, not padded).

    While the renderer runs, the last ``_TAIL_HOLDBACK`` bytes of its file
    are not copied: a trailing chunk of up to that size it appends at close
    (zxtune's tag block), before patching its header's data size, is never
    taken for audio; at the end the patched data size bounds the copy."""
    state = {"fd": -1, "pos": None, "fmt": None, "ok": True, "closed": False}
    live: "_LiveWav | None" = None
    # Held by a worker-thread step and by the close below: a step still
    # running when the render is cancelled must never read a descriptor
    # number that has been closed (and maybe reused) or write a closed file.
    step_lock = threading.Lock()

    def _step(final: bool) -> int:
        with step_lock:
            return 0 if state["closed"] else _step_locked(final)

    def _step_locked(final: bool) -> int:
        # Worker thread: append what's new in ``src`` to ``live``; returns
        # the byte count.
        if state["fd"] < 0:
            try:
                state["fd"] = os.open(str(src), os.O_RDONLY)
            except OSError:
                return 0
        fd = state["fd"]
        size = os.fstat(fd).st_size
        if state["pos"] is None:
            parsed = _tail_fmt(os.pread(fd, min(size, 65536), 0))
            if parsed is None:
                if size > 65536 or final:
                    state["ok"] = False      # no header we can follow
                return 0
            fmt, off = parsed
            state["fmt"], state["pos"] = fmt, off
            if not (fmt["tag"] == 1 and fmt["bits"] == 16
                    and fmt["channels"] == live.channels and fmt["rate"] == live.rate):
                state["ok"] = False
                return 0
        if not state["ok"]:
            return 0
        if final:
            # The renderer patched its header: its data size bounds the audio
            # (anything after it — a tag chunk — is not audio).
            end = size
            head = os.pread(fd, min(size, 65536), 0)
            p = _tail_fmt(head)
            if p is not None:
                dsize = int.from_bytes(head[p[1] - 4:p[1]], "little")
                if 0 < dsize < 0xFFFFFFFF and p[1] + dsize <= size:
                    end = p[1] + dsize
        else:
            end = size - _TAIL_HOLDBACK
        pos = state["pos"]
        if end <= pos:
            return 0
        buf = os.pread(fd, min(end - pos, 4 * 1024 * 1024), pos)
        if buf:
            live.write(buf)
            state["pos"] = pos + len(buf)
        return len(buf)

    err_ring = bytearray()
    ok = False
    try:
        live = _LiveWav(live_key, expected_seconds=expected_seconds, exact=exact,
                        min_audible_seconds=min_audible_seconds)
        async with _render_sem:
            proc = await forksafe.spawn(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                env=env)

            async def _drain_stderr():
                try:
                    while True:
                        raw = await proc.stderr.read(4096)
                        if not raw:
                            return
                        err_ring.extend(raw)
                        if len(err_ring) > 8192:
                            del err_ring[:len(err_ring) - 8192]
                except Exception:
                    pass

            err_task = asyncio.create_task(_drain_stderr())
            # One exit waiter for the whole render (a ``wait_for`` per poll
            # would leave a cancelled waiter behind every 20 ms).
            proc_wait = asyncio.ensure_future(proc.wait())
            try:
                deadline = time.monotonic() + timeout
                while True:
                    final = proc.returncode is not None
                    while True:
                        n = await asyncio.to_thread(_step, final)
                        if not n:
                            break
                        live.account(n)
                        if not final:
                            break
                    if final:
                        break
                    if time.monotonic() > deadline:
                        raise HTTPException(504, f"{kind} render timed out after {int(timeout)}s")
                    await asyncio.wait({proc_wait}, timeout=_TAIL_POLL_S)
            finally:
                if not proc_wait.done():
                    proc_wait.cancel()
                if proc.returncode is None:
                    try:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(err_task, timeout=2.0)
                except Exception:
                    err_task.cancel()
                with step_lock:          # a step still in its thread finishes first
                    state["closed"] = True
                    if state["fd"] >= 0:
                        os.close(state["fd"])
                        state["fd"] = -1
        if proc.returncode != 0:
            raise _renderer_failure(kind, cmd[0], proc.returncode,
                                    bytes(err_ring).decode("utf-8", "replace"))
        if not state["ok"]:
            # Not followable live: the renderer's own file is the render.
            live.abandon()
            if require_audio and not _render_has_audio(src):
                raise HTTPException(
                    422, "The renderer finished but produced no audio for this file.")
            ok = True
            return src
        out = live.finish()
        if require_audio and live.written <= 0:
            log.warning("%s renderer exited 0 without audio for %s: %s", kind, cmd[-1],
                        bytes(err_ring).decode("utf-8", "replace").strip()[:300])
            live.clean = False
            raise HTTPException(
                422, "The renderer finished but produced no audio for this file.")
        ok = True
        src.unlink(missing_ok=True)
        return out
    finally:
        if not ok:
            src.unlink(missing_ok=True)
        if live is not None:
            live.close()


def _uade_subsong_arg(subsong: int, base: int = 0) -> list[str]:
    """uade123 arguments selecting picker index ``subsong``.

    Index 0 is the module's default tune: no flag at all (uade's "cur" tune —
    its first one on every module checked).  Index N > 0 is tune
    ``base + N``, where ``base``
    is the module's first subsong number (``uade123 -g`` "min"): 0 for most
    formats, 1 for e.g. every Jochen Hippel family — rendering ``--subsong=N``
    there played the default tune again at index 1 and never the last one."""
    if subsong <= 0:
        return []
    return [f"--subsong={max(0, int(base)) + int(subsong)}"]


# Subsong bases learnt from a ``uade123 -g`` probe for tracks whose record
# carries no ``subsong_base``.  track_id → base.  Bounded, oldest dropped.
_UADE_BASE_MEMO: "OrderedDict[str, int]" = OrderedDict()
_UADE_BASE_MEMO_MAX = 4096


def _uade_base_known(track_id: str, track=None) -> int | None:
    """A uade track's subsong base when already known — the track record's
    ``subsong_base``, else one probed earlier in this process — or None."""
    b = getattr(track, "subsong_base", None) if track is not None else None
    if b is None:
        b = _UADE_BASE_MEMO.get(track_id)
    try:
        return max(0, int(b)) if b is not None else None
    except (TypeError, ValueError):
        return None


def _remember_uade_base(track_id: str, base: int) -> None:
    _UADE_BASE_MEMO[track_id] = int(base)
    _UADE_BASE_MEMO.move_to_end(track_id)
    while len(_UADE_BASE_MEMO) > _UADE_BASE_MEMO_MAX:
        _UADE_BASE_MEMO.popitem(last=False)


def uade_cache_variant(subsong: int, base: int) -> str | None:
    """The cache-key variant of a uade render: tune index N > 0 of a module
    numbered from ``base`` ≥ 1 gets its own key (``b<base>``) — an older
    entry under the plain key rendered the wrong tune.  None otherwise, so
    every other key is unchanged."""
    return f"b{int(base)}" if base > 0 and subsong > 0 else None


def uade_cache_key(track_id: str, subsong: int = 0, base: int = 0) -> str:
    return _ck(track_id, "uade", subsong=subsong, variant=uade_cache_variant(subsong, base))


def uade_cache_key_known(track_id: str, subsong: int = 0, track=None) -> str:
    """The uade cache key under the base known right now (no probing) — for
    lookups that must stay synchronous and O(1)."""
    return uade_cache_key(track_id, subsong, _uade_base_known(track_id, track) or 0)


def _uade_cwd(path: Path) -> str:
    """Working directory for a uade123 run on ``path``, which is then passed
    by its bare name (``path.name``): uade's players look for companion
    halves beside the module, and some (Musicline Editor) crash — "score
    crashed", no audio — on a module path longer than 127 characters."""
    return str(Path(path).absolute().parent)


async def _probe_uade_base(path: Path) -> int | None:
    """``_probe_uade_base_file`` on ``path`` — unpacked first when XPK-packed;
    None when it can't be unpacked."""
    try:
        async with _xpk_unpacked(path) as src:
            return await _probe_uade_base_file(src)
    except HTTPException:
        return None


async def _probe_uade_base_file(path: Path) -> int | None:
    """The module's first subsong number from ``uade123 -g`` ("subsongs: cur
    C min M max X"), or None when uade can't tell.  ~0.1 s, off the loop's
    CPU (a subprocess), bounded by a timeout."""
    binary = _find_renderer(settings.uade123_path, "uade123")
    if not binary:
        return None
    try:
        proc = await forksafe.spawn(
            binary, "-g", "--", path.name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=_uade_cwd(path))
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass
        return None
    import re as _re
    m = _re.search(r"subsongs:.*?\bmin\s+(-?\d+)", (out or b"").decode("utf-8", "replace"))
    return max(0, int(m.group(1))) if m else None


async def _uade_resolve_base(track_id: str, track, path: Path, subsong: int) -> int:
    """Subsong base to render picker index ``subsong`` with: 0 for the
    default tune (it needs none); otherwise the track's stored base, or a
    one-time ``uade123 -g`` probe of the local module (remembered per
    track for this process — a failed probe as 0, so the Range requests of
    one play never re-probe).  0 when uade can't tell."""
    if subsong <= 0:
        return 0
    b = _uade_base_known(track_id, track)
    if b is None and track is None:
        try:
            b = _uade_base_known(track_id, await get_track(track_id))
        except Exception:
            b = None
    if b is None:
        b = await _probe_uade_base(Path(path)) or 0
    _remember_uade_base(track_id, b)
    return b


@contextlib.asynccontextmanager
async def _xpk_unpacked(path: Path):
    """``path`` for uade — or, when the module is XPK-packed (uade: "The file
    is SQSH packed. Please depack first"), an unpacked copy in a temp folder,
    with links to its companion halves (``smpl.X`` …) beside it, removed
    afterwards (``xpk.write_unpacked``; all file work off the event loop).
    The module's own folder is never written to.  A method other than SQSH,
    or damaged packed data, is a 422 saying so; an unreadable file a 502."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        head = b""
    if _xpk.xpk_method(head) is None:
        yield path
        return

    def _prepare() -> Path:
        data = _xpk.unpack_file(Path(path))
        return _xpk.write_unpacked(
            path, data, _uade_formats.companion_sibling_names(Path(path).name))

    try:
        out = await asyncio.to_thread(_prepare)
    except _xpk.XpkUnsupported as exc:
        raise HTTPException(
            422,
            f"This module is packed with XPK-{exc.method}, which can't be "
            "unpacked here.",
        ) from exc
    except _xpk.XpkError as exc:
        raise HTTPException(
            422,
            "This module is XPK-SQSH packed, but the packed data is damaged "
            "and can't be unpacked.",
        ) from exc
    except OSError as exc:
        log.warning("XPK unpack of %s failed: %s", path, exc)
        raise HTTPException(502, "This packed module couldn't be read or unpacked.") from exc
    try:
        yield out
    finally:
        # Synchronous on purpose: a cancelled render must still clean up (one
        # small file and a few links).
        shutil.rmtree(out.parent, ignore_errors=True)


async def _render_uade(path: Path, *args, **kwargs) -> Path:
    """``_render_uade_file`` on ``path`` — unpacked first when XPK-packed
    (``_xpk_unpacked``)."""
    async with _xpk_unpacked(path) as src:
        return await _render_uade_file(src, *args, **kwargs)


async def _render_uade_file(path: Path, subsong: int = 0, with_vu: bool = True, *,
                       live_key: str | None = None,
                       expected_seconds: float = 0.0,
                       subsong_base: int = 0) -> Path:
    """Render an AHX / Hively / Amiga-tracker module to WAV via uade123.

    Returns the temp-file path; caller (``conversion_cache.get_or_render``)
    moves it into the on-disk cache and unlinks the temp.

    Output spec: 44.1 kHz / stereo / 16-bit signed LE.  Matches what
    sidplayfp + openmpt123 produce so the downstream cast pipeline
    can treat all rendered formats uniformly.

    ``subsong`` is the picker index (0 = the default tune); ``subsong_base``
    is the module's first subsong number, so index N renders tune
    ``subsong_base + N`` (see ``_uade_subsong_arg``) — modules whose tunes
    are numbered from 1 need that translation.

    uade123 streams to stdout (``-c``, byte-identical PCM to its ``-e wav``
    file output) and we write our own canonical header: with ``live_key`` the
    growing temp WAV is registered in ``_UADE_LIVE`` so a listener can start
    hearing it while the rest renders.  ``expected_seconds`` (a length learnt
    from an earlier render or a duration probe) makes the header exact from
    the first byte, which is what lets a browser play and seek it early (a
    render that lands within a second of that length is trimmed / padded to
    it exactly); with no length a live file starts with a "read to the end"
    streaming header, and the header is patched once the render ends.

    ``with_vu`` is kept for callers' compatibility: the per-voice VU sidecar
    is no longer produced here (it doubled the wait before the first byte) —
    ``_spawn_uade_vu`` builds it in the background, from the cached WAV,
    when the now-playing meter asks for it (``request_uade_vu``).
    """
    binary = _find_renderer(settings.uade123_path, "uade123")
    if not binary:
        raise HTTPException(
            501,
            "uade123 not installed — Amiga formats (AHX, TFMX, Future "
            "Composer, SidMon, …) require it. Install via 'brew install "
            "uade' (macOS) or 'apt install uade' (Debian/Ubuntu).",
        )

    # ``--filter=A1200`` picks the Amiga 1200 LED-filter model (the
    # default A500 sounds muffled on modern listeners).  ``--headphones``
    # adds a tiny stereo-widening effect that mimics what AHX players
    # commonly did at the time.  ``-c`` writes the audio to stdout.  ``-1``
    # (--one) is ESSENTIAL: without it uade plays the start subsong and
    # every FOLLOWING one concatenated into a single render (QA M1).
    cmd = [binary, "-1", "--filter=A1200", "--headphones", "-c"]
    cmd += _uade_subsong_arg(subsong, subsong_base)
    # uade runs IN the module's folder and gets its bare name: some players
    # (Musicline Editor) crash on a module path longer than 127 characters,
    # which a deep library folder or the extract dir easily reaches.
    # Companion halves (smpl.X) are still found — they sit beside it.
    cmd += ["--", path.name]

    # Our canonical header is in place before the first sample: exact for a
    # known length, else (live) a streaming one; the end-of-render rewrite
    # (``_LiveWav.finish``) sets the real sizes.  A listener is attached
    # only once the render is audible (``_serve_live``): a silent tune ends
    # without ever being streamed, and the cache refuses it (422).
    lw = _LiveWav(live_key, rate=_UADE_RATE, channels=_UADE_CH,
                  expected_seconds=expected_seconds)
    try:
        async with _render_sem:
            proc = await forksafe.spawn(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=_uade_cwd(path))
            err_ring = bytearray()

            async def _drain_stderr():
                try:
                    while True:
                        raw = await proc.stderr.read(4096)
                        if not raw:
                            return
                        err_ring.extend(raw)
                        if len(err_ring) > 8192:
                            del err_ring[:len(err_ring) - 8192]
                except Exception:
                    pass

            err_task = asyncio.create_task(_drain_stderr())

            async def _pump():
                head = b""
                header_done = False
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        return
                    if not header_done:
                        # uade's own WAV header (sizes unknown on a pipe):
                        # drop everything up to the end of its data chunk
                        # header; ours is already in place.
                        head += chunk
                        i = head.find(b"data")
                        if i < 0 or len(head) < i + 8:
                            if len(head) > 4096:
                                raise HTTPException(502, "uade123 produced no WAV data")
                            continue
                        chunk = head[i + 8:]
                        header_done = True
                        if not chunk:
                            continue
                    lw.feed(chunk)

            try:
                await asyncio.wait_for(_pump(), timeout=_UADE_DEFAULT_TIMEOUT_S)
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                raise HTTPException(504, f"uade render timed out after {int(_UADE_DEFAULT_TIMEOUT_S)}s")
            finally:
                if proc.returncode is None:
                    try:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(err_task, timeout=2.0)
                except Exception:
                    err_task.cancel()
            if proc.returncode != 0:
                raise _renderer_failure("uade", binary, proc.returncode,
                                        bytes(err_ring).decode("utf-8", "replace"),
                                        module_path=path)
            if lw.written <= 0:
                # uade exited cleanly but its player crashed before the first
                # sample ("score crashed"): a listener-facing reason, the raw
                # text in the log.
                log.info("uade123 produced no audio for %s: %s", path.name,
                         bytes(err_ring).decode("utf-8", "replace").strip()[-300:])
                raise HTTPException(502, "The Amiga player couldn't render this module.")
        # The file is self-consistent whatever the real length turned out to
        # be: the header is rewritten to the bytes that actually landed.  When
        # a length was promised up front and the render lands within a second
        # of it (the stored duration is rounded to 10 ms), the tail is
        # trimmed/padded to EXACTLY the promised size, so the cached file is
        # byte-for-byte the resource a live reader was told about (same
        # Content-Range total on the next Range request) and the stored
        # duration stays stable.  A larger mismatch (a stale stored length)
        # keeps the real render; the duration backfill then corrects the
        # stored value.
        return lw.finish()
    finally:
        lw.close()


def _drop_live_after_store(key: str, live: dict) -> None:
    """Forget a finished live render once the conversion cache has taken the
    file over (its in-flight event fires after the move) — until then a new
    request that sees the entry must wait for the cache, not open the temp."""
    from soniqboom.core.conversion_cache import inflight_event

    async def _later():
        ev = inflight_event(key)
        if ev is not None:
            try:
                await asyncio.wait_for(ev.wait(), timeout=120)
            except asyncio.TimeoutError:
                pass
        if _UADE_LIVE.get(key) is live:
            _UADE_LIVE.pop(key, None)
    _bg_keep(asyncio.ensure_future(_later()))


_BG_TASKS: set = set()


def _bg_keep(task: "asyncio.Future") -> None:
    """Hold a strong reference to a fire-and-forget task until it ends."""
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


# Upper bound on a stored uade length trusted for an exact up-front header —
# an audio-length sanity cap (the same 1 h clamp SID lengths get), NOT the
# render's wall-clock timeout: uade123's own subsong timeout ends looping tunes
# at ~512 s (3 of the 19 local test modules), and those get an exact header too.
_UADE_MAX_EXPECTED_S = 3600.0


def _uade_expected_seconds(track, subsong: int = 0, track_id: "str | None" = None) -> float:
    """A uade track's length when one is already known (an earlier render or a
    duration probe stored it); 0 = unknown (the scan stores 0 for uade).

    The stored duration describes the DEFAULT tune only (tune index
    ``default_tune_index`` — 0, no ``--subsong`` flag, unless a probe found the
    first tune empty), so any other tune is "unknown": trusting it made a
    short tune's header claim the default tune's length (seconds of padded
    silence) or cut a longer one short."""
    return _stored_render_seconds(track, subsong, track_id)


def _stored_render_seconds(track, subsong: int = 0, track_id: "str | None" = None,
                           placeholder: "float | None" = None) -> float:
    """A render-only track's stored length, when it can stand for the render
    of tune index ``subsong`` (``_uade_expected_seconds`` explains why only
    the default tune's); 0 = unknown.  ``placeholder``: the value the scan
    stores for this format when it doesn't know the length (AdLib's 180 s,
    the chiptune default) — never a length."""
    if track is None or subsong != (default_tune_index(track_id, track) if track_id else 0):
        return 0.0
    try:
        d = float(getattr(track, "duration", 0) or 0)
    except (TypeError, ValueError):
        d = 0.0
    if placeholder is not None and abs(d - float(placeholder)) <= 0.01:
        return 0.0
    return d if 1.0 < d <= _UADE_MAX_EXPECTED_S else 0.0


_UADE_VU_INFLIGHT: set[str] = set()
_UADE_VU_UNSUPPORTED = False     # latched when this uade build can't --write-audio
_UADE_VU_UNSUPPORTED_AT = 0.0    # monotonic time of the latch
# A latch is re-tested after this long (a rebuilt/upgraded uade123 needs no
# restart; a build that still lacks the feature costs one instant-exit pass).
_UADE_VU_RELATCH_S = 6 * 3600.0
# uade renders whose per-voice VU sidecar may be wanted: ``_ck(id, "uade",
# subsong)`` → (source, subsong, zip pin id, first-served monotonic time, the
# render's cache key, subsong base).  Every uade serve registers here (a dict
# write); the pass itself only starts when GET /api/tracks/{id}/vu asks for the
# sidecar — the web now-playing meter, while the track is current and the meter
# is on — and only once the track was first served ``_UADE_VU_MIN_PLAYED_S``
# ago.  A queued pass is dropped (re-asked later if still wanted) unless the
# meter polled within ``_UADE_VU_POLL_FRESH_S`` — checked after the start delay
# and again once it holds a slot.  So skipped tracks, Subsonic / DLNA / cast
# plays and sessions with the meter off never pay for the second full uade
# pass.  Bounded, oldest dropped.
from collections import OrderedDict as _VUOrderedDict
_UADE_VU_WANTED: "_VUOrderedDict[str, tuple]" = _VUOrderedDict()
_UADE_VU_WANTED_MAX = 64
# Last time (monotonic) the meter asked for each wanted key.
_UADE_VU_LAST_POLL: "_VUOrderedDict[str, float]" = _VUOrderedDict()
# Tunes whose pass ran but made no sidecar (longer than _UADE_VU_MAX_TUNE_S,
# no disk headroom on the file fallback, or uade failed on it): key → the
# monotonic time.  GET /vu answers "skipped" for them — the meter stops
# asking — until _UADE_VU_SKIP_TTL_S has passed (then one new try).  Bounded.
_UADE_VU_SKIPPED: "_VUOrderedDict[str, float]" = _VUOrderedDict()
_UADE_VU_SKIP_TTL_S = 3600.0
_UADE_VU_MIN_PLAYED_S = 3.0
# The web player's Amiga VU re-ask ladder (frontend/js/app.js
# _AMIGA_VU_RETRY_MS) must keep every rung at least 5 s under this window
# (fetch latency + timer drift); tests/js/frontend_contracts.test.mjs checks
# the two against each other.
_UADE_VU_POLL_FRESH_S = 20.0
# Seconds a requested VU pass waits before queueing for a background slot:
# the player asks for the NEXT track's prewarm ~3 s into playback, and a
# running VU pass (seconds of uade) can't be pre-empted — so let the prewarm
# reach the priority gate first.
_UADE_VU_START_DELAY = 4.0


def uade_vu_unavailable_reason() -> str | None:
    """Why Amiga per-voice meters can't be produced right now: ``"unsupported"``
    while this uade build is latched as unable to dump voices (re-tested after
    ``_UADE_VU_RELATCH_S``), ``"off"`` when the ``uade_vu_meters`` setting is
    off, else None.  GET /vu sends it as ``X-VU-Unavailable`` so the player
    stops asking."""
    global _UADE_VU_UNSUPPORTED
    if _UADE_VU_UNSUPPORTED:
        if time.monotonic() - _UADE_VU_UNSUPPORTED_AT < _UADE_VU_RELATCH_S:
            return "unsupported"
        _UADE_VU_UNSUPPORTED = False
    try:
        from soniqboom.core.data import get_store
        if not get_store().get_config("uade_vu_meters", True):
            return "off"
    except Exception:
        pass
    return None


def _uade_vu_enabled() -> bool:
    return uade_vu_unavailable_reason() is None


def uade_vu_skipped_reason(track_id: str, subsong: int = 0) -> str | None:
    """``"skipped"`` when this tune's per-voice pass already ran without making
    a sidecar (too long, no disk headroom, uade failed on it) within
    ``_UADE_VU_SKIP_TTL_S``, else None.  GET /vu sends it as
    ``X-VU-Unavailable`` so the player stops asking."""
    key = _ck(track_id, "uade", subsong=subsong)
    t = _UADE_VU_SKIPPED.get(key)
    if t is None:
        return None
    if time.monotonic() - t >= _UADE_VU_SKIP_TTL_S:
        _UADE_VU_SKIPPED.pop(key, None)
        return None
    return "skipped"


def _note_uade_vu_skipped(key: str) -> None:
    _UADE_VU_SKIPPED[key] = time.monotonic()
    _UADE_VU_SKIPPED.move_to_end(key)
    while len(_UADE_VU_SKIPPED) > 256:
        _UADE_VU_SKIPPED.popitem(last=False)


def is_uade_routed(path_str: str, track=None) -> bool:
    """Does playback route this track through uade (Amiga)?  Name-only, no
    file IO: a uade suffix token / prefix-form name — AdLib names and owned
    extensions excluded, exactly as ``_render_ident`` decides (pass ``track``
    so a scanner-verified Amiga module counts).  (A magic-less ``.sid`` —
    SidMon — is otherwise decidable only from the file, so it doesn't count
    here.)"""
    ext, uade_named = _render_ident(path_str or "", track)
    return bool(uade_named) or ext in _UADE_EXTS


def reset_uade_vu_latch() -> None:
    """Forget a "this uade123 can't dump voices" verdict (e.g. when the
    ``uade_vu_meters`` setting is saved) so the next request tries again."""
    global _UADE_VU_UNSUPPORTED
    _UADE_VU_UNSUPPORTED = False


def _note_uade_vu_wanted(key: str, src_path: Path, subsong: int,
                         zip_pin_id: str | None, *, cache_key: str | None = None,
                         base: int = 0) -> None:
    prev = _UADE_VU_WANTED.get(key)
    served_at = prev[3] if prev is not None else time.monotonic()
    _UADE_VU_WANTED[key] = (Path(src_path), int(subsong), zip_pin_id, served_at,
                            cache_key or key, int(base))
    _UADE_VU_WANTED.move_to_end(key)
    while len(_UADE_VU_WANTED) > _UADE_VU_WANTED_MAX:
        _UADE_VU_WANTED.popitem(last=False)


def _uade_vu_still_wanted(key: str) -> bool:
    """Did the meter ask for ``key`` recently (the listener is still on it)?"""
    t = _UADE_VU_LAST_POLL.get(key)
    return t is not None and time.monotonic() - t <= _UADE_VU_POLL_FRESH_S


def request_uade_vu(track_id: str, subsong: int = 0) -> bool:
    """Start the per-voice VU pass for a uade render this server has served
    (GET /vu calls it on a sidecar miss).  False when nothing was served for
    that track/subsong or the meters are off.  True otherwise — the pass is
    started (deduped; it waits for a render still in progress), or, within
    ``_UADE_VU_MIN_PLAYED_S`` of the first serve, left for the next ask so a
    track skipped right away never costs it.  Every call marks the track as
    still wanted (see ``_uade_vu_still_wanted``)."""
    key = _ck(track_id, "uade", subsong=subsong)
    ent = _UADE_VU_WANTED.get(key)
    if (ent is None or not _uade_vu_enabled()
            or uade_vu_skipped_reason(track_id, subsong) is not None):
        return False
    now = time.monotonic()
    _UADE_VU_LAST_POLL[key] = now
    _UADE_VU_LAST_POLL.move_to_end(key)
    while len(_UADE_VU_LAST_POLL) > _UADE_VU_WANTED_MAX:
        _UADE_VU_LAST_POLL.popitem(last=False)
    src, ss, pin_id, served_at, cache_key, base = ent
    if now - served_at < _UADE_VU_MIN_PLAYED_S:
        return True
    _spawn_uade_vu(cache_key, src, ss, pin_id, base=base, poll_key=key)
    return True


def _spawn_uade_vu(key: str, src_path: Path, subsong: int,
                   zip_pin_id: str | None = None, *, base: int = 0,
                   poll_key: str | None = None) -> None:
    """Build the per-voice VU sidecar for a cached uade WAV in the background.

    A second full uade pass plus a dump parse, so it never delays audio: it
    waits for the render, then ``_UADE_VU_START_DELAY``, then the LOWEST
    background priority, once per key — and is dropped (not marked done, so a
    later ask starts it again) when the meter stopped asking for ``poll_key``
    (default ``key``) before it got going.  Only the uade run holds the
    background slot; the dump parse and the sidecar write happen after it is
    released, off the event loop.  Holds the zip-extract pin so the source
    module isn't evicted mid-pass, and the cache pin so the WAV isn't.
    """
    if key in _UADE_VU_INFLIGHT or not _uade_vu_enabled():
        return
    binary = _find_renderer(settings.uade123_path, "uade123")
    if not binary:
        return
    poll_key = poll_key or key
    _UADE_VU_INFLIGHT.add(key)
    if zip_pin_id:
        _zip_pin(zip_pin_id)

    async def _run():
        global _UADE_VU_UNSUPPORTED, _UADE_VU_UNSUPPORTED_AT
        from soniqboom.core.conversion_cache import (
            inflight_event, get_cached, pin as _pin, unpin as _unpin,
        )
        pinned = attempted = wrote = unsupported = False
        dump = None
        try:
            ev = inflight_event(key)
            if ev is not None:
                await ev.wait()
            wav = await get_cached(key)
            if wav is None or wav.with_suffix(".vu").exists() or not Path(src_path).exists():
                return
            _pin(key)
            pinned = True
            if _UADE_VU_START_DELAY > 0:
                await asyncio.sleep(_UADE_VU_START_DELAY)
            if not _uade_vu_still_wanted(poll_key):
                return                    # the listener moved on
            async with _bg_render_sem.slot(PRIO_VU):
                # Re-check after the (possibly long) wait for a slot: another
                # pass may have written it, the listener may have moved on, or
                # the source may be gone (an unpinned remote-cache entry, a
                # moved file) — a vanished source must not read as "this uade
                # build can't dump".
                if (wav.with_suffix(".vu").exists() or not Path(src_path).exists()
                        or not _uade_vu_still_wanted(poll_key)):
                    return
                attempted = True
                dump = await _uade_vu_dump(binary, Path(src_path), subsong, wav, base=base)
            # The slot is free again: write (a dump file: parse) outside it.
            if dump is False:
                unsupported = True
                _UADE_VU_UNSUPPORTED = True
                _UADE_VU_UNSUPPORTED_AT = time.monotonic()
                log.info("uade123 has no --write-audio support — Amiga VU meters "
                         "fall back to the spectrum view")
            elif dump is not None:
                done, dump = dump, None   # _uade_vu_finish owns (and removes) a file
                wrote = bool(await _uade_vu_finish(done, wav, Path(src_path).name))
        except Exception:
            log.debug("UADE VU background pass failed for %s", key, exc_info=True)
        finally:
            if isinstance(dump, tuple):
                Path(dump[0]).unlink(missing_ok=True)
            if pinned:
                _unpin(key)
            if zip_pin_id:
                try:
                    _zip_unpin(zip_pin_id)
                except Exception:
                    pass
            if attempted:
                # Done (sidecar written, skipped for length/disk, or failed):
                # later polls must not re-run the pass.  A new play re-registers.
                _UADE_VU_WANTED.pop(poll_key, None)
                # No sidecar and not the build-wide latch: tell the meter this
                # tune's won't come (GET /vu → X-VU-Unavailable: skipped).
                if not wrote and not unsupported:
                    _note_uade_vu_skipped(poll_key)
            _UADE_VU_INFLIGHT.discard(key)

    _bg_keep(asyncio.ensure_future(_run()))


async def _serve_uade(request: Request, track_id: str, track, path: Path,
                      subsong: int, *, web_session: bool, background,
                      zip_pin_id: str | None, xf: "_Xform | None" = None) -> Response:
    """Serve a uade-rendered track through ``_serve_live``: uade123 pipes its
    PCM, so the growing WAV plays while it renders.  The length promised up
    front is a stored one (``_uade_expected_seconds``) — never exact — so
    only our web UI gets the growing file; Subsonic, DLNA and cast wait for
    the finished one."""
    base = await _uade_resolve_base(track_id, track, path, subsong)
    key = uade_cache_key(track_id, subsong, base)
    expected = _uade_expected_seconds(track, subsong, track_id)
    _note_uade_vu_wanted(_ck(track_id, "uade", subsong=subsong), path, subsong,
                         zip_pin_id, cache_key=key, base=base)

    async def _after(cached_path):
        # uade formats are render-only: the scan stored duration 0 — uade123
        # renders to the tune's natural end, so persist the WAV's real length
        # (default tune only; the render is the authority, so a stale stored
        # value is corrected too).
        await _backfill_rendered_duration(track_id, track, cached_path, 0.0,
                                          subsong=subsong, authoritative=True)

    return await _serve_live(
        request, track_id=track_id, format_type="uade", subsong=subsong, key=key,
        render_fn=lambda: _render_uade(path, subsong=subsong, live_key=key,
                                       expected_seconds=expected, subsong_base=base),
        cache_kw={"variant": uade_cache_variant(subsong, base)},
        web_session=web_session, background=background, xf=xf,
        rendered="uade123", after_render=_after)


async def _live_attach(*, track_id: str, format_type: str, subsong: int, key: str,
                       render_fn, cache_kw: "dict | None" = None
                       ) -> "tuple[asyncio.Future | None, Path | None, dict | None]":
    """``(task, cached, live)`` for a live-capable render of ``key``: the
    cached file, else the live entry of the render in progress — started
    here (``task``, the ``get_or_render`` future) unless one is running."""
    from soniqboom.core.conversion_cache import get_or_render, get_cached
    cached = await get_cached(key)
    if cached is not None:
        return None, cached, None
    live = _UADE_LIVE.get(key)
    task = None
    if live is None or live["complete"].is_set():
        task = asyncio.ensure_future(get_or_render(
            track_id=track_id, format_type=format_type, subsong=subsong,
            render_fn=render_fn, **(cache_kw or {})))
        task.add_done_callback(lambda f: f.cancelled() or f.exception())
        _bg_keep(task)
        # The render registers its live entry before it waits for a slot.
        live = await _await_uade_live(key, task)
    return task, None, live


async def _live_open_audible(live: dict, timeout: float = 45.0) -> "_FdHandle | None":
    """Wait until the live render ``live`` is audible, then open its growing
    file — or None when it ended first (failed, silent, or simply short: the
    cache then has it) or stayed silent for ``timeout`` s.

    Never answer before audible audio is in: a file the renderer rejects, or
    a tune that plays only silence, must reach the player as an error, not
    as silence.  The render wakes ``data`` on its first bytes, its first
    audible ones and when it ends.  The descriptor is opened NOW: it
    survives the move into the cache, a lazy open did not."""
    deadline = time.monotonic() + timeout
    while not live.get("audible") and not live["complete"].is_set():
        rem = deadline - time.monotonic()
        if rem <= 0:
            break
        try:
            await asyncio.wait_for(live["data"].wait(), rem)
        except asyncio.TimeoutError:
            break
    if not live.get("audible") or live["complete"].is_set():
        return None
    try:
        return _FdHandle(os.open(str(live["path"]), os.O_RDONLY))
    except OSError:
        return None


async def _serve_live(request: Request, *, track_id: str, format_type: str,
                      subsong: int, key: str, render_fn,
                      cache_kw: "dict | None" = None, web_session: bool,
                      background, xf: "_Xform | None", rendered: str,
                      after_render=None) -> Response:
    """Serve a track whose render can be played while it runs (a live render:
    ``_LiveWav``): cache hit → the file with Range; otherwise attach to (or
    start) the render and, once it is audible, play the growing WAV — first
    audio in well under a second instead of after the whole render.

    Who gets the growing file:
      • our web UI (``web_session``): with a known length (``expected_size``
        — exact, or a stored length the player corrects once the render is
        complete) every request (no Range, probes, seeks) is served against
        the final size; with an unknown length an open-ended first request
        (no Range / ``bytes=0-``) streams under a provisional "read to the
        end" header — except to Safari, whose media stack needs real
        byte-range answers: it waits for the finished file.  A range probe
        (``bytes=0-1`` — any WebKit, including home-screen apps and in-app
        web views whose UA lacks "Safari") waits for it too, then gets a real
        206;
      • any other GET client (Subsonic, DLNA, the cast byte-server) only when
        the length is EXACT (``live["exact"]``: the renderer was told it and
        produces it): they need a true Content-Length, and nothing corrects
        a stored length that turns out wrong for them.
    A request for another codec / a bitrate cap (``xf.codec``) without a time
    offset gets a live encode of the growing file (``_transcode_growing_render``;
    with an estimated Content-Length asked for, only when the length is
    exact).  Everyone else, and a time offset, waits for the finished file.

    ``render_fn`` renders with ``live_key=key``; ``cache_kw`` are the
    ``get_or_render`` arguments beyond the subsong that make ``key``
    (soundfont, variant).  ``after_render(cached_path)`` (a coroutine
    function: the duration backfill) runs once the render is cached — before
    the answer on the blocking path, in the background after a progressive
    start.  ``rendered`` names the renderer (``X-Rendered``)."""
    from soniqboom.core.conversion_cache import known_silent
    transform = xf is not None and xf.active
    task, cached, live = await _live_attach(
        track_id=track_id, format_type=format_type, subsong=subsong, key=key,
        render_fn=render_fn, cache_kw=cache_kw)

    range_hdr = (request.headers.get("range") or "").strip()
    open_ended = not range_hdr or range_hdr == "bytes=0-"
    if (not transform and cached is None and live is not None
            and request.method == "GET"
            and not live["complete"].is_set() and not known_silent(key)
            and ((web_session and (live["expected_size"] > 0
                                   or (open_ended and not _is_safari(request))))
                 or (not web_session and live.get("exact")
                     and live["expected_size"] > 0))):
        handle = await _live_open_audible(live)
        # None: a short tune finished while we waited (the cache below serves
        # the complete file), or the render failed / came out silent (the
        # task below raises its error).
        if handle is not None:
            _live_finish_later(
                task, after_render,
                lambda: _live_attach_task(track_id, format_type, subsong, render_fn,
                                          cache_kw))
            headers = {"X-Rendered": rendered, "X-Cache": "miss-progressive"}
            if live["expected_size"] <= 0:
                headers["X-Render-Length"] = "unknown"
                return await _chunked_growing_file_response(
                    request, live["path"], live["expected_size"], live["complete"],
                    media_type="audio/wav", headers=headers,
                    data_event=live["data"], inflight=live,
                    unpin_key=None, background_task=background, fd=handle,
                )
            # Known length: exact Content-Length (no Range), or a 206 against
            # the final size — a ``bytes=0-1`` probe gets its 2 bytes, not
            # the whole growing file.
            return await _growing_file_range_response(
                request, live["path"], live["expected_size"], live["complete"],
                media_type="audio/wav", headers=headers,
                data_event=live["data"], inflight=live,
                unpin_key=None, background_task=background, fd=handle,
            )

    # Another codec (Subsonic ``format=`` / a bitrate cap, no time offset) of
    # a render still running: a live encode of the growing file, whatever
    # its length (the encode is chunked) — unless the client wants an
    # estimated Content-Length, which needs the exact one.
    if (xf is not None and xf.codec and not xf.seek and request.method == "GET"
            and cached is None and live is not None
            and not live["complete"].is_set() and not known_silent(key)
            and (_estimate_len_ctx.get() is None
                 or (live.get("exact") and live["expected_size"] > 0))):
        done = await _cached_rendered_transcode(track_id, xf, key)
        if done is not None:
            return await _serve_rendered_transcode_hit(
                request, done, xf, {"X-Rendered": rendered}, background)
        feed = await _live_feed_of(live)
        if feed is not None:
            attach = (lambda: _live_attach_task(track_id, format_type, subsong,
                                                render_fn, cache_kw))
            _live_finish_later(task, after_render, attach)
            rendered_task = task if task is not None else asyncio.ensure_future(attach())
            if task is None:
                rendered_task.add_done_callback(lambda f: f.cancelled() or f.exception())
            exact_s = ((live["expected_size"] - _WAV_HEADER_LEN)
                       / live.get("bytes_per_sec", _UADE_BYTES_PER_SEC)
                       if live.get("exact") and live["expected_size"] > 0 else None)
            return await _transcode_growing_render(
                request, track_id=track_id, feed=feed, xf=xf, source_key=key,
                rendered=lambda: asyncio.shield(rendered_task),
                headers={"X-Rendered": rendered, "X-Cache": "miss-progressive"},
                background=background, src_seconds=exact_s)

    if task is None:
        task = _live_attach_task(track_id, format_type, subsong, render_fn, cache_kw)
    cached_path, hit = await task
    if after_render is not None:
        await after_render(cached_path)
    return await _serve_rendered(
        request, cached_path,
        headers={"X-Rendered": rendered, "X-Cache": "hit" if hit else "miss"},
        background=background, xf=xf, track_id=track_id, source_key=key,
    )


async def _live_feed_of(live: dict):
    """The growing file of the live render ``live`` as an async iterator of a
    whole WAV, once it is audible — or None (``_live_open_audible``).  Ends
    with the render; raises ``_RenderAborted`` when the render fails
    part-way.  Its header is the file's own only when the length is EXACT;
    otherwise a "read to the end" one: a consumer (ffmpeg) honours a header's
    length, and a stored length that turned out wrong must never cut an
    encode short of the render (or leave it padded)."""
    handle = await _live_open_audible(live)
    if handle is None:
        return None
    if live.get("exact") and live["expected_size"] > 0:
        resp = await _chunked_growing_file_response(
            None, live["path"], live["expected_size"], live["complete"],
            media_type="audio/wav", data_event=live["data"], inflight=live, fd=handle)
    else:
        resp = await _chunked_growing_file_response(
            None, live["path"], 0, live["complete"], media_type="audio/wav",
            data_event=live["data"], inflight=live, fd=handle,
            head=_streaming_wav_header(live.get("rate", _UADE_RATE),
                                       live.get("channels", _UADE_CH), 16),
            data_start=_WAV_HEADER_LEN)
    return resp.body_iterator


def _live_attach_task(track_id: str, format_type: str, subsong: int, render_fn,
                      cache_kw: "dict | None"):
    """A ``get_or_render`` call for a live-capable render: a cache hit, an
    attach to the render in progress, or (nothing running) a new render."""
    from soniqboom.core.conversion_cache import get_or_render
    return get_or_render(track_id=track_id, format_type=format_type,
                         subsong=subsong, render_fn=render_fn, **(cache_kw or {}))


async def _await_uade_live(key: str, task: "asyncio.Future",
                           timeout: float = 2.0) -> "dict | None":
    """The live entry of the render ``task`` is starting for ``key`` — woken
    the moment the render registers it (``_LiveWav``; or the task ends, e.g.
    it attached to another render), at most ``timeout`` s."""
    live = _UADE_LIVE.get(key)
    if (live is not None and not live["complete"].is_set()) or task.done():
        return live
    ev = _UADE_LIVE_REGISTERED.setdefault(key, asyncio.Event())
    waiter = asyncio.ensure_future(ev.wait())
    try:
        await asyncio.wait({waiter, task}, timeout=timeout,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
        if _UADE_LIVE_REGISTERED.get(key) is ev:
            _UADE_LIVE_REGISTERED.pop(key, None)
    return _UADE_LIVE.get(key)


def _live_finish_later(task, after_render, attach) -> None:
    """After a progressive start: once the render is cached, run
    ``after_render`` on it (the response is already streaming).  ``attach()``
    gives the render's result when this request didn't start it."""
    if after_render is None:
        return

    async def _after():
        try:
            cached_path, _ = await (task if task is not None else attach())
            await after_render(cached_path)
        except Exception:
            log.debug("post-render bookkeeping failed", exc_info=True)
    _bg_keep(asyncio.ensure_future(_after()))


async def live_wav_feed(track_id: str, *, format_type: str, subsong: int, key: str,
                        render_fn, cache_kw: "dict | None" = None):
    """For an in-process consumer that reads a rendered WAV from its first byte
    (the cast pipeline's ffmpeg): an async iterator over the growing file of
    the live render of ``key`` — attached to, or started with ``render_fn``
    (as ``_serve_live`` does) — once it is audible; it ends with the render
    and raises (``_RenderAborted``) when the render fails part-way.  None
    when there is nothing live to read: already cached, a tune that just
    rendered silent, or a render that ended (or failed) before it was
    audible — the caller then takes the cached file or the render's error
    through ``get_or_render``, which attaches to the same render."""
    from soniqboom.core.conversion_cache import known_silent
    if known_silent(key):
        return None
    task, cached, live = await _live_attach(
        track_id=track_id, format_type=format_type, subsong=subsong, key=key,
        render_fn=render_fn, cache_kw=cache_kw)
    if cached is not None or live is None or live["complete"].is_set():
        return None
    return await _live_feed_of(live)


async def sid_wav_feed(sid_path: Path, subsong: int, duration: int, full_key: str):
    """``live_wav_feed`` for a C64 SID: the progressive render's whole WAV
    (``_serve_sid_progressive`` as a non-web caller — the same render pool,
    caching and attach-to-a-running-render as a progressive play) as an async
    iterator, once the render is audible; it raises (``_RenderAborted``) when
    the render dies or stalls part-way.  None when it can't start (pool full,
    the render failed at once, or it ended before this caller could read it
    live): the caller renders blocking, which surfaces the error or finds the
    cached file.  A tune that plays only silence raises the cache's 422."""
    from soniqboom.core.conversion_cache import known_silent
    if known_silent(full_key):
        return None
    resp = await _serve_sid_progressive(None, sid_path, subsong, duration, full_key,
                                        base_headers={}, background=None, web=False)
    return resp.body_iterator if resp is not None else None


# The Paula dump grows at ~2.5 MB per tune-second (measured).  The VU pass
# runs AFTER the main render, only when the now-known duration is within this
# cap (a runaway looping tune is cut at ~512 s by uade itself).  Where
# ``os.mkfifo`` exists uade writes the dump into a FIFO that is parsed as it
# arrives (``uade_vu.parse_stream``): nothing is stored.  Elsewhere it goes to
# a temp file, and only with disk headroom.
_UADE_VU_MAX_TUNE_S = 420
_UADE_VU_MIN_FREE_BYTES = 4 * 1024**3


def _uade_vu_cmd(binary: str, path: Path, subsong: int, base: int, dump: str) -> list[str]:
    """uade123 argv for a VU pass — run it with ``cwd=_uade_cwd(path)`` (the
    module goes by its bare name, like ``_render_uade``; ``dump`` stays an
    absolute path)."""
    cmd = [binary, "-1", "--filter=A1200", "--headphones",
           f"--write-audio={dump}", "-e", "wav", "-f", os.devnull]
    cmd += _uade_subsong_arg(subsong, base)
    return cmd + ["--", path.name]


def _uade_vu_cannot_dump(err: bytes | None) -> bool:
    """Does uade's stderr say this BUILD can't dump voices (no write-audio
    support, or the option is unknown) — rather than this tune failing?"""
    low = (err or b"")[:4096].decode("utf-8", "replace").lower()
    return any(m in low for m in ("write audio", "write-audio", "writeaudio",
                                  "unrecognized option", "unknown option",
                                  "invalid option"))


async def _uade_vu_dump(
    binary: str, path: Path, subsong: int, wav_path: Path, *, base: int = 0,
) -> "object | tuple[Path, float] | bool | None":
    """``_uade_vu_dump_file`` on ``path`` — unpacked first when XPK-packed
    (``_xpk_unpacked``); None (no meters for this tune) when it can't be."""
    try:
        async with _xpk_unpacked(path) as src:
            return await _uade_vu_dump_file(binary, src, subsong, wav_path, base=base)
    except HTTPException:
        return None


async def _uade_vu_dump_file(
    binary: str, path: Path, subsong: int, wav_path: Path, *, base: int = 0,
) -> "object | tuple[Path, float] | bool | None":
    """The uade run of a per-voice VU pass (the part that needs a render
    slot).  Where ``os.mkfifo`` exists the dump is parsed while uade writes
    it and the ``VUResult`` is returned (``_uade_vu_dump_fifo``); elsewhere
    uade writes a temp file and ``(dump_path, duration)`` is returned for
    ``_uade_vu_parse`` — the caller then owns the dump file.  False when this
    uade build cannot dump voices at all (no dump and uade's stderr names the
    missing write-audio support or rejects the option), None when skipped or
    failed for this tune only — including a source that vanished, which must
    never switch the meters off for every tune.  Holds a render slot while
    uade runs, like every other renderer.
    """
    duration = _wav_audio_seconds(wav_path)
    if not (0 < duration <= _UADE_VU_MAX_TUNE_S):
        log.debug("UADE VU skipped for %s: duration %.0fs out of range",
                  path.name, duration)
        return None
    if hasattr(os, "mkfifo"):
        return await _uade_vu_dump_fifo(binary, path, subsong, duration, base=base)
    dump_tmp: Path | None = None
    keep = False
    try:
        import shutil as _sh
        if _sh.disk_usage(tempfile.gettempdir()).free < (
                _UADE_VU_MIN_FREE_BYTES + int(duration * 3 * 1024 * 1024)):
            log.info("UADE VU skipped for %s: low disk", path.name)
            return None
        _d = tempfile.NamedTemporaryFile(suffix=".uadedump", delete=False)
        _d.close()
        dump_tmp = Path(_d.name)
        cmd = _uade_vu_cmd(binary, path, subsong, base, str(dump_tmp))
        async with _render_sem:
            if not path.exists():
                return None
            proc = await forksafe.spawn(
                *cmd, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, cwd=_uade_cwd(path),
            )
            try:
                _, err = await asyncio.wait_for(
                    proc.communicate(), timeout=min(duration * 2 + 60, 600))
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
                return None
        if dump_tmp.stat().st_size == 0:
            if not path.exists():
                return None                      # the source went away mid-pass
            if _uade_vu_cannot_dump(err):
                return False                     # this build can't dump voices
            return None                          # this tune only
        keep = True
        return dump_tmp, duration
    except Exception:
        log.warning("UADE VU extraction failed for %s", path, exc_info=True)
        return None
    finally:
        if dump_tmp is not None and not keep:
            try:
                dump_tmp.unlink(missing_ok=True)
            except OSError:
                pass


async def _uade_vu_dump_fifo(
    binary: str, path: Path, subsong: int, duration: float, *, base: int = 0,
) -> "object | bool | None":
    """``_uade_vu_dump`` through a FIFO: uade writes the dump into it and a
    worker thread parses it as it arrives (``uade_vu.parse_stream``), so the
    dump (~2.6 MB per tune-second) never touches the disk and no free-space
    headroom is needed.  Returns the ``VUResult``, False (this build can't
    dump voices) or None (skipped / failed for this tune).

    We hold our own write end of the FIFO from before uade starts until it has
    ended: nothing ever blocks opening it, and the reader sees EOF only then —
    also when uade never opens it (a build without the option, a spawn that
    failed).  uade is killed on timeout or cancellation; the reader always
    drains to EOF, so uade never stalls on a full pipe.
    """
    from soniqboom.core import uade_vu
    loop = asyncio.get_running_loop()
    d = tempfile.mkdtemp(prefix="uadevu-")
    fifo = os.path.join(d, "dump")
    rfd = wfd = -1
    reader = None
    proc = None
    stats: dict = {}
    err = b""
    try:
        try:
            os.mkfifo(fifo, 0o600)
            rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            wfd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.set_blocking(rfd, True)
        except OSError:
            log.warning("UADE VU: could not set up a FIFO in %s", d, exc_info=True)
            return None

        def _read(fd: int = rfd) -> "object | None":
            with os.fdopen(fd, "rb", buffering=0) as fh:
                try:
                    return uade_vu.parse_stream(fh, duration, stats=stats)
                except Exception:
                    log.debug("UADE VU stream parse failed", exc_info=True)
                    while fh.read(1 << 16):      # never leave the writer blocked
                        pass
                    return None
        reader = loop.run_in_executor(None, _read)
        rfd = -1                                 # the reader owns (and closes) it
        cmd = _uade_vu_cmd(binary, path, subsong, base, fifo)
        async with _render_sem:
            if not path.exists():
                return None
            proc = await forksafe.spawn(
                *cmd, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, cwd=_uade_cwd(path),
            )
            try:
                _, err = await asyncio.wait_for(
                    proc.communicate(), timeout=min(duration * 2 + 60, 600))
            except asyncio.TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                return None
        os.close(wfd)
        wfd = -1
        result = await reader
        reader = None
        if not stats.get("bytes"):
            if not path.exists():
                return None                      # the source went away mid-pass
            if _uade_vu_cannot_dump(err):
                return False                     # this build can't dump voices
            return None                          # this tune only
        return result
    except Exception:
        log.warning("UADE VU extraction failed for %s", path, exc_info=True)
        return None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        if wfd >= 0:
            os.close(wfd)                        # → EOF for the reader
        if rfd >= 0:
            os.close(rfd)
        if reader is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(reader), timeout=10.0)
        shutil.rmtree(d, ignore_errors=True)


async def _uade_vu_write(result, wav_path: Path, name: str = "") -> bool | None:
    """Write a parsed VU result as the sidecar next to ``wav_path`` (in a
    worker thread).  True when written, else None."""
    if result is None or result.frames <= 0:
        return None
    from soniqboom.core import openmpt_vu
    vu_path = wav_path.with_suffix(".vu")
    await asyncio.get_running_loop().run_in_executor(
        None, openmpt_vu.write_sidecar, vu_path, result,
    )
    log.info("UADE VU sidecar for %s: %d ch × %d frames @ %d Hz",
             name or wav_path.name, result.channels, result.frames,
             result.sample_rate)
    return True


async def _uade_vu_parse(dump_tmp: Path, duration: float, wav_path: Path,
                         name: str = "") -> bool | None:
    """Parse a VU dump file and write the sidecar next to ``wav_path`` — both
    in a worker thread (the chunked parser keeps its GIL holds to a few ms).
    True when a sidecar was written, else None.  Always removes the dump."""
    try:
        from soniqboom.core import uade_vu
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: uade_vu.parse_dump(dump_tmp, duration),
        )
        return await _uade_vu_write(result, wav_path, name)
    except Exception:
        log.warning("UADE VU parse failed for %s", name or wav_path, exc_info=True)
        return None
    finally:
        try:
            Path(dump_tmp).unlink(missing_ok=True)
        except OSError:
            pass


async def _uade_vu_finish(dump, wav_path: Path, name: str = "") -> bool | None:
    """Turn what ``_uade_vu_dump`` returned into the sidecar: a ``VUResult``
    is written, a ``(dump_path, duration)`` file is parsed first (and
    removed).  True when a sidecar was written, else None."""
    if isinstance(dump, tuple):
        return await _uade_vu_parse(dump[0], dump[1], wav_path, name)
    try:
        return await _uade_vu_write(dump, wav_path, name)
    except Exception:
        log.warning("UADE VU sidecar write failed for %s", name or wav_path, exc_info=True)
        return None


async def _uade_vu_pass(
    binary: str, path: Path, subsong: int, wav_path: Path, *, base: int = 0,
) -> bool | None:
    """Best-effort per-voice VU sidecar via a second, dump-only uade run.

    Kept OUT of the main render so the dump is bounded by the already-known
    tune duration — a VU failure or skip never affects the audio render.
    ``_uade_vu_dump`` then ``_uade_vu_finish``: True when a sidecar was
    written, False when this uade build cannot dump voices at all, None when
    skipped or failed for this tune only.  (``_spawn_uade_vu`` runs the two
    halves itself so the sidecar write happens outside the background slot.)
    """
    dump = await _uade_vu_dump(binary, path, subsong, wav_path, base=base)
    if dump is None or dump is False:
        return dump
    return await _uade_vu_finish(dump, wav_path, path.name)


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
        proc = await forksafe.spawn(
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


def _psf_has_magic(path: Path) -> bool:
    """Content sniff for the ``.psf`` extension collision: a PSF rip starts
    with 'PSF' + its version byte; Amiga SoundFactory modules (Modland's
    ``SoundFactory/*.psf``) don't — uade plays those.  An unreadable file
    counts as PSF (the PSF path then reports it)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(3) == b"PSF"
    except OSError:
        return True


def _dsf_is_dreamcast(path: Path) -> bool:
    """Content sniff for the ``.dsf`` extension collision: 'PSF\\x12' =
    Sega Dreamcast rip (sequenced); 'DSD ' = Sony DSD audio stream."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"PSF\x12"
    except OSError:
        return False


_PSF_LIB_TAG_RE = re.compile(r"^_lib\d*$", re.IGNORECASE)


_PSF_TAG_MAX = 50_000        # the PSF spec's cap on the [TAG] block


def _psf_lib_present(folder: Path, name: str) -> bool:
    """Is library ``name`` (a path relative to the rip, "/" or "\\"
    separated) there — its folders as written, its file name in any case, or
    at least a file of that name beside the rip."""
    rel = name.replace("\\", "/").strip("/")
    sub, _, base = rel.rpartition("/")
    for where in ((folder / sub) if sub else folder, folder):
        try:
            if base.lower() in {n.lower() for n in os.listdir(where)}:
                return True
        except OSError:
            pass
    return False


def _psf_tag_lines(path: Path) -> list[str]:
    """The lines of a PSF-family rip's ``[TAG]`` block; empty when it has
    none or the file can't be read.  Reads only the header and the tag
    block."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
            if len(head) < 16 or head[:3] != b"PSF":
                return []
            fh.seek(16 + int.from_bytes(head[4:8], "little")
                    + int.from_bytes(head[8:12], "little"))
            tag = fh.read(5 + _PSF_TAG_MAX)
    except (OSError, ValueError, OverflowError):
        return []
    if tag[:5] != b"[TAG]":
        return []
    from soniqboom.core.metadata import _psf_tag_text
    return _psf_tag_text(tag[5:]).splitlines()


def _psf_missing_libs(path: Path) -> list[str]:
    """The libraries a PSF-family rip names in its ``[TAG]`` block
    (``_lib=driver.psflib``, ``_lib2=…``) that aren't there
    (``_psf_lib_present``); empty when none is missing or the file can't be
    read.  Reads only the header and the tag block."""
    libs: list[str] = []
    for line in _psf_tag_lines(path):
        key, sep, val = line.partition("=")
        if sep and _PSF_LIB_TAG_RE.match(key.strip()) and val.strip():
            libs.append(val.strip())
    return [n for n in libs if not _psf_lib_present(path.parent, n)]


def _psf_render_seconds(path: Path) -> float:
    """How long zxtune renders a PSF-family rip: its ``length`` tag — the
    installed zxtune stops there and plays no ``fade`` (measured on PSF,
    PSF2, USF, GSF, 2SF, NCSF and SSF rips), while the scan stores length +
    fade as the duration; 0 when untagged, or tagged with something that is
    no length (not finite, over an hour).  Blocking (header + tag block)."""
    from soniqboom.core.metadata import _psf_parse_length
    for line in _psf_tag_lines(path):
        key, sep, val = line.partition("=")
        if sep and key.strip().lower() == "length":
            secs = _psf_parse_length(val)
            return secs if math.isfinite(secs) and 0 < secs <= _UADE_MAX_EXPECTED_S else 0.0
    return 0.0


async def _render_psf(path: Path, subsong: int = 0, *,
                      live_key: str | None = None,
                      expected_seconds: float = 0.0) -> Path:
    """Render a PSF-family file to WAV via zxtune123.

    PSF rips are one-track-per-file (minipsf per song, shared *lib beside
    it) — ``subsong`` is accepted for signature parity but unused.  zxtune
    honours the embedded length/fade tags for the stop point.  A library the
    rip names (``_lib=…``) that isn't beside it is a 422 naming it
    (``_psf_missing_libs``) — zxtune would render nothing, exit 0.

    ``live_key``: a live render (``_LiveWav``) played while zxtune writes it
    — the console emulators are slow for some systems (a 3-minute Dreamcast
    rip takes ~9 s, a GBA one ~2 s).  ``expected_seconds``: the length it
    will render, when tagged (``_psf_render_seconds``).
    """
    binary = _find_renderer(settings.zxtune123_path, "zxtune123")
    if not binary:
        raise HTTPException(
            501,
            "zxtune123 not installed — console music rips (PSF/USF/GSF/2SF/"
            "SSF/DSF) require it. Re-run install.sh, or set "
            "renderers.zxtune123_path.",
        )
    missing = await asyncio.to_thread(_psf_missing_libs, Path(path))
    if missing:
        names = " and ".join(missing) if len(missing) < 3 else ", ".join(missing)
        raise HTTPException(
            422,
            f"This PSF rip needs {names} next to it, and "
            f"{'it is' if len(missing) == 1 else 'they are'} missing.",
        )
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    # zxtune REFUSES to overwrite an existing file yet still exits 0
    # ("File already exists", rc=0 — verified), which cached the pre-created
    # empty temp as a "successful" render.  Unlink the placeholder first.
    Path(tmp_wav.name).unlink(missing_ok=True)
    # File-based WAV backend: ``--wav filename=<out>``.  No display needed.
    cmd = [binary, "--silent", "--wav", f"filename={tmp_wav.name}", str(path)]
    if live_key:
        return await _tail_render(
            cmd, Path(tmp_wav.name), live_key, kind="PSF", timeout=600,
            expected_seconds=expected_seconds)
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
        proc = await forksafe.spawn(
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


def _sndh_info(path: Path) -> tuple[int, dict[int, int], int]:
    """(default_track, {track: seconds}, track count) from ``psgplay -i``
    tags (``!#`` / ``TIME`` / ``##``).  default_track falls back to 1, the
    count to 0 (unknown)."""
    binary = _find_renderer(settings.psgplay_path, "psgplay")
    default_track, times, count = 1, {}, 0
    if not binary:
        return default_track, times, count
    import subprocess as _sp
    try:
        r = forksafe.run([binary, "-i", str(path)], capture_output=True,   # a worker thread: never a fork
                         text=True, timeout=20)
    except (_sp.TimeoutExpired, OSError):
        return default_track, times, count
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts[:2] != ["tag", "field"] or len(parts) < 4:
            continue
        if parts[2] == "!#":
            try:
                default_track = max(1, int(parts[3]))
            except ValueError:
                pass
        elif parts[2] == "##":
            try:
                count = max(0, int(parts[3]))
            except ValueError:
                pass
        elif parts[2] == "TIME" and len(parts) >= 5:
            try:
                times[int(parts[3])] = max(0, int(parts[4]))
            except ValueError:
                continue
    return default_track, times, count


async def _render_sndh(path: Path, subsong: int = 0, *,
                       track_id: str | None = None,
                       live_key: str | None = None) -> Path:
    """Render an Atari ST SNDH file via psgplay (stereo 16-bit 44.1 kHz).

    Subsong semantics mirror SID: the param is the wire index and the
    file's default track (SNDH ``!#`` tag) its start song, so wire 0 plays
    that track and the rest follow ``sid_wire_tune``; psgplay's ``-t`` is
    the 1-based track.  psgplay
    hard-errors on ``--stop=auto`` when a tune declares no TIME tag, so a
    ``--length`` is ALWAYS passed: the tag's duration when present, else
    the Atari default cap.  ``track_id`` (optional) gets the start song
    written back to its record (``_persist_start_song``).

    ``live_key``: a live render (``_LiveWav``) played while psgplay writes
    it (a 3-minute tune takes ~1.3 s).  psgplay renders exactly the
    ``--length`` it is given, so the length is EXACT from byte 0 and every
    client may stream it.
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
    default_track, times, count = await loop.run_in_executor(None, _sndh_info, path)
    if track_id:
        _persist_start_song(track_id, sid_wire_tune(0, default_track, count), count)
    track = sid_wire_tune(subsong, default_track, count)
    secs = times.get(track, 0)
    # Hard-cap the length: an untrusted TIME tag ("TIME 1 999999999") would
    # otherwise drive an unbounded render + timeout — filling the disk and
    # pinning a render slot forever (QA C1, 2026-07-02).
    length = min(secs if secs > 0 else _ATARI_DEFAULT_S, 3600)
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    cmd = [binary, "-t", str(track), "-f", "44100",
           f"--length={length}", "-o", tmp_wav.name, str(path)]
    if live_key:
        return await _tail_render(
            cmd, Path(tmp_wav.name), live_key, kind="SNDH",
            timeout=max(60, length + 60), expected_seconds=length, exact=True)
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
    # Subsong semantics mirror SID (``sid_wire_tune``) with the first track as
    # the default: param = wire index, ``--track`` is 1-based, so wire N plays
    # track N+1.  0 = the first track: sc68 2.2.1's "--track=0 = all tracks"
    # would concatenate, so it is resolved explicitly.
    track = sid_wire_tune(subsong, 1)
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
            proc = await forksafe.spawn(
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

    Uses forksafe.spawn (no fork) so the event loop is never blocked
    while ffprobe inspects the file.  Bounded by a timeout so a slow SMB
    share or pathological file can't park the stream endpoint forever.
    """
    try:
        proc = await forksafe.spawn(
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


def _make_zip_bank_fallback(*, remote=None, local_zip=None, lane: str = "stream"):
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
            return source.read_file(ref, lane=lane)
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


def _archive_companion_filter(member_chain: str):
    """``companion(basename_lower) -> bool`` for an archive member: the
    sibling files its renderer needs beside it (uade companion halves,
    AdLib / PSF banks) — what a ranged subset of a remote archive must carry
    (``core.remote_zip``).  None when the member needs none of these."""
    import fnmatch
    base = member_chain.split("::")[-1].replace("\\", "/").rsplit("/", 1)[-1]
    real = _uade_member_real_name(base)
    wanted = ({s.lower() for s in _uade_formats.companion_sibling_names(real)}
              if real else set())
    globs = tuple(g.lower() for g in _ADLIB_COMPANION_GLOBS.get(Path(base).suffix.lower(), ()))
    if not wanted and not globs:
        return None
    return lambda b: b in wanted or any(fnmatch.fnmatch(b, g) for g in globs)


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


def _streaming_wav_header(sample_rate: int, channels: int,
                          bits_per_sample: int = 16) -> bytes:
    """A 44-byte PCM header for a WAV whose length is not known yet: RIFF and
    data sizes 0xFFFFFFFF, the conventional "read to the end of the stream"
    marker (what ffmpeg writes to a pipe)."""
    h = bytearray(_build_wav_header(sample_rate, channels, 0, bits_per_sample))
    h[4:8] = b"\xff\xff\xff\xff"
    h[40:44] = b"\xff\xff\xff\xff"
    return bytes(h)


def _wav_layout(path: "Path | str") -> "dict | None":
    """Header facts of a finished WAV: ``{"fmt_tag", "channels", "rate",
    "bits", "block_align", "data_offset", "data_size"}`` (tag unwrapped from
    WAVE_FORMAT_EXTENSIBLE), or None when unreadable.  Header-only read."""
    import struct
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
            fsize = os.fstat(fh.fileno()).st_size
    except OSError:
        return None
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos, fmt = 12, None
    while pos + 8 <= len(head):
        cid = head[pos:pos + 4]
        size = struct.unpack_from("<I", head, pos + 4)[0]
        body = pos + 8
        if cid == b"fmt " and body + 16 <= len(head):
            tag, ch, rate, _br, align, bits = struct.unpack_from("<HHIIHH", head, body)
            if tag == 0xFFFE and size >= 26 and body + 26 <= len(head):
                tag = struct.unpack_from("<H", head, body + 24)[0]
            fmt = {"fmt_tag": tag, "channels": ch, "rate": rate,
                   "bits": bits, "block_align": align or max(1, ch * bits // 8)}
        elif cid == b"data":
            if fmt is None:
                return None
            avail = max(0, fsize - body)
            dsize = avail if size in (0, 0xFFFFFFFF) else min(size, avail)
            return {**fmt, "data_offset": body, "data_size": dsize}
        pos = body + size + (size & 1)
    return None


def _wav_header_for(layout: dict, data_bytes: int) -> bytes:
    """A canonical 44-byte header matching ``layout``'s sample format and
    declaring ``data_bytes`` of audio (PCM or IEEE-float)."""
    h = bytearray(_build_wav_header(layout["rate"], layout["channels"],
                                    data_bytes // max(1, layout["block_align"]),
                                    bits_per_sample=layout["bits"]))
    if layout.get("fmt_tag") == 3:
        h[20:22] = (3).to_bytes(2, "little")
    return bytes(h)


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
        proc = await forksafe.spawn(
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
                proc = await forksafe.spawn(
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


class _RenderAborted(RuntimeError):
    """A live render failed mid-stream: the growing-file body is aborted (no
    terminating chunk / short of its Content-Length), so the player gets a
    network error and asks why, instead of a normal end of track."""


class _CleanupStreamingResponse(StreamingResponse):
    """``StreamingResponse`` whose background cleanup (cache unpin, archive
    extract unpin, descriptor close) runs exactly once however the body ends
    — Starlette skips it when the body raises (``_RenderAborted``) or the
    server rejects a body short of its Content-Length."""

    async def __call__(self, scope, receive, send) -> None:
        bg, self.background = self.background, None
        try:
            await super().__call__(scope, receive, send)
        finally:
            if bg is not None:
                try:
                    await bg()
                except Exception:
                    log.debug("stream cleanup failed", exc_info=True)


async def _serve_inflight_wav(
    request: Request,
    track,
    src_path: Path,
    track_id: str,
    target_rate: int | None,
    target_channels_hint: int | None,
    original_codec_label: str,
    background_task,
    *,
    seek: float = 0.0,
) -> Response:
    """Adaptive cold-start dispatcher.

    States, in priority order:
      1. Cache hit         → serve final WAV with Range. Zero penalty.
      2. In-flight attach  → growing-file Range response against the
                              partial WAV that an earlier subscriber or
                              the prewarm path is already producing.
      3. Cold start        → kick off a new render via _get_or_start_inflight_wav
                              then attach as state 2.

    ``seek`` > 0 (a Subsonic time offset) serves the WAV from that point on
    under a header sized to the rest: from the cached file (O(1)), or — while
    it is still rendering — streamed from the growing file.
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
        _hit_headers = {"X-Transcoded": "1", "X-Original-Codec": original_codec_label,
                        "X-Target-Codec": _INFLIGHT_CACHE_CODEC, "X-Cache": "hit"}
        if seek > 0:
            return await _wav_offset_response(
                request, cached_path, seek, _hit_headers,
                _make_unpin_task(background_task))
        return await _range_file_response(
            request, cached_path, media_type=_INFLIGHT_CACHE_MIME,
            headers=_hit_headers,
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
    if seek > 0:
        # Time-offset start into the still-rendering WAV: a header sized to
        # the rest of the track, then the growing file from that frame.
        _block = inflight["channels"] * 3               # s24le frames
        _frames = max(0, (inflight["expected_size"] - _WAV_HEADER_LEN) // _block)
        _skip = min(_frames, int(round(seek * inflight["sample_rate"])))
        headers["X-Time-Offset"] = f"{_skip / inflight['sample_rate']:.3f}"
        return await _chunked_growing_file_response(
            request, inflight["wav_path"], inflight["expected_size"],
            inflight["complete_event"], media_type=_INFLIGHT_CACHE_MIME,
            headers=headers, data_event=inflight.get("data_event"),
            inflight=inflight, unpin_key=cache_key,
            background_task=background_task,
            head=_build_wav_header(inflight["sample_rate"], inflight["channels"],
                                   _frames - _skip),
            data_start=_WAV_HEADER_LEN + _skip * _block,
        )

    is_web_ui = bool(request.cookies.get("sb_session"))
    range_hdr = (request.headers.get("range") or "").strip()
    # Only an open-ended first request streams chunked.  A header probe
    # (WebKit's ``bytes=0-1``, ``bytes=0-0``) gets its exact bytes as a 206
    # against the known final size below — never the whole growing file.
    is_initial_get = not range_hdr or range_hdr == "bytes=0-"
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


class _FdHandle:
    """An already-open read descriptor handed to a streaming responder.

    Opening the growing file BEFORE the response is returned closes the
    window in which a short render finishes and ``store_cached`` moves the
    temp away (the responder's own lazy open then failed and the client got
    an empty 200).  An open descriptor keeps the inode readable through
    ``os.replace`` / ``shutil.move`` and later LRU unlinks.  ``close`` is
    idempotent: the generator's ``finally``, the response's background task
    and (for a response whose body never started) garbage collection all
    call it, whichever comes first wins."""

    __slots__ = ("fd",)

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def __del__(self):
        self.close()


def _accel_off(headers: dict) -> dict:
    """Ask a reverse proxy (nginx) not to buffer a response that is still
    being produced — without it the listener hears nothing until nginx's
    buffers fill.  Merged into the caller's headers."""
    headers.setdefault("X-Accel-Buffering", "no")
    return headers


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
    *,
    fd: "_FdHandle | None" = None,
    head: bytes | None = None,
    data_start: int = 0,
) -> Response:
    """Serve a growing inflight WAV via chunked transfer-encoding.

    Differs from ``_growing_file_range_response``:

      • No ``Content-Length`` → ``Transfer-Encoding: chunked`` implied
        by Starlette.  Browsers don't gate on HAVE_FUTURE_DATA at all;
        playback starts as soon as the WAV header is read and the
        first PCM chunk arrives.
      • Starts from offset 0 — or, with ``head``, sends ``head`` (a WAV
        header built by the caller) and then the file from ``data_start``
        (a time-offset start into a growing render).  This is the "first
        play, cold cache" path — subsequent Range requests from the same
        browser (seeks, prefetches) are routed to the Range-served path
        which DOES handle byte ranges.
      • Reads via ``os.pread`` so this response and any concurrent
        Range readers don't fight over a shared file offset.

    ``expected_size`` is the file's final size; ``<= 0`` means unknown (the
    body simply ends with the render, never padded).  ``fd`` is an already
    open descriptor for ``file_path`` the response takes ownership of.

    The trade-off is no seeking during this single response — but the
    moment the file is promoted to the conversion cache (post-pump
    completion), the next request goes to the cache-hit fast path
    with full Range support.
    """
    extra = _accel_off(dict(headers or {}))
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

        if fd is not None:
            handle = fd
        else:
            try:
                handle = _FdHandle(await asyncio.to_thread(
                    os.open, str(file_path), os.O_RDONLY,
                ))
            except OSError as exc:
                log.warning("chunked-inflight: open failed for %s: %s",
                            file_path, exc)
                _release_pin_once()
                return
        rfd = handle.fd

        pos = 0
        # With a caller-built header the body must match what it declares:
        # never send bytes past the promised end (a render may overshoot it).
        limit = expected_size if (head is not None and expected_size > 0) else None
        try:
            if head is not None:
                yield head
                pos = max(0, int(data_start))
            while True:
                if limit is not None and pos >= limit:
                    return
                # Read whatever is currently available.  pread doesn't
                # advance a shared offset, so concurrent Range readers
                # against the same fd-target don't interfere.
                try:
                    chunk = await asyncio.to_thread(
                        os.pread, rfd,
                        _RANGE_STREAMING_CHUNK if limit is None
                        else min(_RANGE_STREAMING_CHUNK, limit - pos), pos,
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
                    # A render that FAILED must not turn into minutes of
                    # silence (uade live renders opt in via this flag; the
                    # ffmpeg pump keeps its rounding-gap padding).
                    no_pad = inflight is not None and inflight.get("no_pad_on_failure")
                    if no_pad and inflight.get("clean_exit") is False:
                        # Abort, don't end: a normal end would play the
                        # fragment as the whole track and move on silently.
                        log.warning("chunked-inflight: render failed at %d bytes, "
                                    "aborting stream", pos)
                        raise _RenderAborted(f"render failed after {pos} bytes")
                    # ffmpeg has finished.  If we've sent everything, exit
                    # (a length that was never known is never padded).
                    # If ffmpeg under-wrote vs the WAV header's stated
                    # data-chunk size (rounding on DSF duration), pad
                    # with silence so the browser's WAV duration check
                    # doesn't trip NS_ERROR_NET_PARTIAL_TRANSFER on
                    # Firefox or a silent cut-off on Chrome.
                    if expected_size <= 0 or pos >= expected_size:
                        return
                    # uade renders align to the promised length themselves
                    # when they land within a second of it; a bigger gap
                    # means the promised length was wrong — end the body
                    # rather than append that much silence.
                    if no_pad and expected_size - pos > _UADE_BYTES_PER_SEC:
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
            handle.close()
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

    return _CleanupStreamingResponse(
        _stream_pcm(),
        status_code=200,
        media_type=media_type,
        headers=extra,
        # The inflight-cache unpin runs in _stream_pcm's finally (_release_pin_once);
        # this releases the caller's zip-extract pin when the response closes
        # (and a handed-over descriptor, should the body never have started).
        background=_compose_backgrounds(
            BackgroundTask(fd.close) if fd is not None else None, background_task),
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
    *,
    fd: "_FdHandle | None" = None,
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

    ``fd``: an already open descriptor for ``file_path`` (see ``_FdHandle``)
    the response takes ownership of; otherwise the path is opened lazily.

    A single ``Range`` is answered as ``_parse_audio_range`` reads it against
    the final size — a suffix range (``bytes=-N``) is the last N bytes, a
    start past the end is a 416.  A live render (``no_pad_on_failure``) is
    never padded with more than a second of silence: a body more than that
    short of its length ends short instead (its promised length was wrong,
    or the render stopped).
    """
    extra = _accel_off(dict(headers or {}))
    extra["Accept-Ranges"] = "bytes"

    async def _drop_subscriber():
        # Symmetric with whoever counted this response as a subscriber.
        if inflight is not None:
            try:
                async with _INFLIGHT_LOCK:
                    cur = inflight.get("subscribers", 0)
                    if cur > 0:
                        inflight["subscribers"] = cur - 1
            except Exception:
                pass

    bg = None
    if unpin_key is not None:
        from soniqboom.core.conversion_cache import unpin as _unpin

        def _do_unpin():
            try:
                _unpin(unpin_key)
            except Exception:
                pass
        bg = BackgroundTask(_do_unpin)

    rng = _parse_audio_range(request.headers.get("range") if request is not None else None,
                             expected_size)
    if rng is None:
        extra["Content-Range"] = f"bytes */{expected_size}"
        return Response(
            status_code=416, media_type=media_type, headers=extra,
            background=_compose_backgrounds(
                bg, BackgroundTask(fd.close) if fd is not None else None,
                BackgroundTask(_drop_subscriber), background_task))
    start, end_excl, is_range = rng
    end = end_excl - 1
    if is_range:
        status_code = 206
        extra["Content-Range"] = f"bytes {start}-{end}/{expected_size}"
    else:
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
        if inflight is not None and inflight.get("never_pad"):
            return      # compressed audio (a remote file): end short, never zeros
        if (inflight is not None and inflight.get("no_pad_on_failure")
                and end - pos + 1 > inflight.get("bytes_per_sec", _UADE_BYTES_PER_SEC)):
            # A live render more than a second short of what was promised:
            # end short (see the docstring), never seconds of silence.
            log.info("growing file: render ended %d bytes short of its promised "
                     "length — ending the body short", end - pos + 1)
            return
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
            opened = (os.fdopen(fd.fd, "rb", closefd=False) if fd is not None
                      else open(file_path, "rb"))
            with opened as f:
                rfd = f.fileno()
                f.seek(pos)
                while pos <= end:
                    try:
                        current_size = os.fstat(rfd).st_size
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
                        if (inflight is not None and inflight.get("no_pad_on_failure")
                                and inflight.get("clean_exit") is False):
                            return
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
                                cur = os.fstat(rfd).st_size
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
                            current_size = os.fstat(rfd).st_size
                        except OSError:
                            async for buf in _yield_silent_padding(pos, end):
                                yield buf
                            return
                        available_end = min(current_size, end + 1)
        except asyncio.CancelledError:
            # Client disconnected mid-stream — just exit cleanly.
            raise
        finally:
            if fd is not None:
                fd.close()
            # Decrement subscriber count on response end (success, error,
            # or client disconnect).  The X-Inflight-Subscribers header
            # was set at response start so its value stays informational,
            # but the internal counter now stays accurate across the
            # full subscriber lifecycle.
            await _drop_subscriber()

    # The unpin (``bg``, above) runs as a BackgroundTask so the cache entry's
    # refcount drops as soon as the client closes the response — without this
    # every play would permanently anchor its cache entry and LRU eviction
    # would silently stop working (R2/R3 finding).
    return _CleanupStreamingResponse(
        _yield_growing_range(),
        status_code=status_code,
        media_type=media_type,
        headers=extra,
        # Run BOTH the inflight-cache unpin and the caller's zip-extract cleanup
        # (background_task) when the response closes — each awaited + isolated.
        background=_compose_backgrounds(
            bg, BackgroundTask(fd.close) if fd is not None else None,
            background_task),
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
        proc = await forksafe.spawn(
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
    # Codec, rate clamp, bitrate, the DSD filter chain and FLAC level — the
    # same arguments the live time-offset pipe uses (``_ffmpeg_encode_args``).
    # Built before the temp exists, so a cancel here can't leak one.
    enc_args = await _ffmpeg_encode_args(
        fmt, bitrate_kbps=bitrate_kbps, target_rate=target_rate,
        src_is_dsd=path.suffix.lower() in _DSD_EXTS, label=path.name)
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

    cmd += enc_args
    if progress_key and source_duration:
        cmd += ["-progress", "pipe:1"]
    cmd += ["-f", fmt, str(out)]

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
        await _await_renderer(cmd, out, timeout=timeout_s, kind="Transcode",
                              require_audio=False)     # checked just below
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
        proc = await forksafe.spawn(
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
                            target_rate: int | None = None, *,
                            codec: str | None = None,
                            bitrate_kbps: int | None = None,
                            copy: bool = False, src_feed=None):
    """Yield chunks from a live ffmpeg transcode of ``path``.

    seek_sec > 0 uses a fast pre-input seek (-ss before -i) so a client can
    start a transcoded stream at any position (Subsonic ``timeOffset``)
    without waiting for — or re-decoding — everything before it.

    ``codec`` / ``bitrate_kbps`` / ``target_rate`` are honoured exactly like
    the cached transcode (shared ``_ffmpeg_encode_args``; the codec defaults
    to ``settings.transcode_format``).  ``copy`` remuxes an input that is
    already in the target codec (a cached transcode) instead of re-encoding.

    ``src_feed``: the input is a rendered WAV still rendering, as an async
    iterator of its bytes from byte 0 (``sid_wav_feed`` / the growing file
    of a live render), written to ffmpeg's stdin as it arrives (``path``
    only names it; no seek).  A feed that fails part-way aborts the body
    (``_RenderAborted``) after ffmpeg's last bytes, so the client sees an
    error, not a short track.
    """
    fmt = (codec or settings.transcode_format).lower()
    if fmt not in TRANSCODE_MIME:
        fmt = settings.transcode_format
    cmd = [settings.ffmpeg_path or "ffmpeg", "-hide_banner", "-loglevel", "error",
           "-nostdin"]
    if src_feed is not None:
        cmd += ["-f", "wav", "-i", "pipe:0", "-vn"]
    else:
        if seek_sec > 0:
            # Place -ss before -i for a fast input seek
            cmd += ["-ss", f"{seek_sec:.3f}"]
        cmd += [
            "-i", str(path),
            "-vn",           # drop video/cover art
        ]
    if copy:
        cmd += ["-c:a", "copy"]
    else:
        cmd += await _ffmpeg_encode_args(
            fmt, bitrate_kbps=bitrate_kbps, target_rate=target_rate,
            src_is_dsd=Path(path).suffix.lower() in _DSD_EXTS, label=Path(path).name)
    cmd += ["-f", fmt, "pipe:1"]
    proc = await forksafe.spawn(
        *cmd,
        **({"stdin": asyncio.subprocess.PIPE} if src_feed is not None else {}),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    feed_failed = False

    async def _feed_stdin():
        nonlocal feed_failed
        try:
            async for piece in src_feed:
                proc.stdin.write(piece)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass                      # ffmpeg is gone
        except asyncio.CancelledError:
            raise
        except Exception:
            feed_failed = True
            log.warning("live transcode: the render of %s failed part-way", Path(path).name)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await src_feed.aclose()
            except Exception:
                pass

    feeder = asyncio.ensure_future(_feed_stdin()) if src_feed is not None else None
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
        if feeder is not None:
            # ffmpeg's output ended (or went idle): the feeder ends with it —
            # bounded, a source still waiting on a stuck render is cancelled.
            try:
                await asyncio.wait_for(asyncio.shield(feeder), timeout=10)
            except asyncio.TimeoutError:
                feeder.cancel()
                feed_failed = True
            if feed_failed:
                raise _RenderAborted(f"the render of {Path(path).name} failed part-way")
    finally:
        if feeder is not None and not feeder.done():
            feeder.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(feeder, timeout=2.0)
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
    subsong: int | None = Query(default=None, ge=0),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    request: Request = None,
):
    """Render state of a track, for every rendered format.

    ``state`` — ``idle`` (nothing cached or running), ``queued`` (a prewarm is
    waiting for a render slot or fetching its source), ``rendering``,
    ``ready_for_playback`` (still rendering, but a growing file can already be
    played), ``complete`` (a rendered WAV is cached), ``failed`` (the last
    render failed, within the last minute: ``error`` says why and
    ``error_status`` is its HTTP status).  The player uses it to keep
    "Rendering…" up instead of reporting a failure while the server is still
    working, never to treat a started prewarm as playable audio, and to report
    a failed render's reason without requesting the stream again.

    Live renders (Amiga / uade, MIDI, AdLib, SNDH, PSF, libgme chiptunes)
    also carry ``provisional`` — true while a render of unknown length is
    streaming under a "read to the end" header — and, once the render is
    cached, ``duration_seconds``: the exact length of that subsong's render.

    The SID fields (per-track target duration honouring HVSC Songlengths,
    what's cached, whether the full-duration version is ready) are unchanged.
    Same sign-in as the stream itself.
    """
    await _require_stream_auth(request, sb_session, u, p, s, t)
    from soniqboom.core.conversion_cache import (
        is_cache_ready, _cache_key, find_shorter_sid_entry, sid_warm_eligible,
        key_matches,
    )
    # The same per-tune length (and so the same cache key) as the SID stream
    # branch, so the UI never reads a stale global default while playback
    # honours HVSC.  O(1): the start song as already known (never a file read
    # on a status poll).
    track = await get_track(track_id)
    wire = explicit_wire(subsong, request)
    subsong = wire or 0                 # SID: the wire (wire 0 = its default)
    target_dur = (_sid_target_seconds(track, subsong) if track is not None
                  else max(5, min(int(settings.sid_default_duration), 3600)))

    full_key = _cache_key(track_id, "sid", subsong, duration=target_dur)
    full_ready = await is_cache_ready(full_key)

    cached_dur = target_dur if full_ready else 0
    partial = False

    if not full_ready:
        shorter = await find_shorter_sid_entry(track_id, subsong, target_dur)
        if shorter:
            cached_dur = shorter[1]
            partial = True

    # The renders are keyed by tune index (SID / SNDH: the wire itself); a
    # bare play renders the default tune — while its probe runs, that is
    # "rendering" whatever tune 1's state is.
    idx = tune_index(track_id, track, wire) if track is not None else subsong
    if wire is None and default_probe_running(track_id):
        state = "rendering"
    else:
        state = _render_state(track_id, idx, wire=wire)
    out = {
        "state": state,
        "ready": full_ready,
        "partial": partial,
        "cached_seconds": cached_dur,
        "target_seconds": target_dur,
        "warm_eligible": sid_warm_eligible(),
        "track_id": track_id,
    }
    if state == "failed":
        from soniqboom.core.conversion_cache import recent_failure
        fail = recent_failure(track_id, idx)
        if fail is not None:
            out["error"] = fail["detail"]
            out["error_status"] = fail["status"]
    # Live renders (uade and the others) are keyed per subsong, so tune N
    # never reports tune 0's length.  A scan of a tiny map, O(1) lookups.
    out["provisional"] = any(
        not lv["complete"].is_set() and lv["expected_size"] <= 0
        for k, lv in list(_UADE_LIVE.items()) if key_matches(k, track_id, idx))
    if state == "complete":
        uade_key = uade_cache_key_known(track_id, idx, track)
        size = _peek_cached_size(uade_key)
        bps = _UADE_BYTES_PER_SEC
        if not size:
            # Another live render's finished file (newest first).
            for k in reversed(list(_LIVE_FINISHED)):
                if key_matches(k, track_id, idx):
                    size, bps = _peek_cached_size(k), _LIVE_FINISHED.get(k) or bps
                    if size:
                        break
        if size and size > _WAV_HEADER_LEN:
            out["duration_seconds"] = (size - _WAV_HEADER_LEN) / bps
    return out


def _render_state(track_id: str, subsong: int = 0, *, wire: "int | None" = -1) -> str:
    """The ``state`` of /render-status for one track + subsong (see there).
    ``subsong`` is the tune index renders are keyed by (``tune_index``);
    ``wire`` what the request named — None for a bare play — which queued
    prewarms are keyed by (``_prewarm_key``; default: ``subsong``).

    Covers every render kind: live uade pipes, progressive SID streams, the
    adaptive in-flight WAV transcodes (DSD / ALAC / AIFF …), conversion-cache
    renders, queued prewarms and — only when none of those — a render that
    failed within the last minute.  Keys are matched per subsong
    (``conversion_cache.key_matches``) so recovery for tune 3 never reads
    tune 0's state.  Each check is a dict lookup or a scan of a tiny map."""
    from soniqboom.core.conversion_cache import render_state, key_matches
    for k, live in list(_UADE_LIVE.items()):
        if key_matches(k, track_id, subsong) and not live["complete"].is_set():
            # Both known- and unknown-length renders stream to the web UI —
            # once a second of audio is in and it is audible.
            playable = (live.get("audible")
                        and live["bytes"] >= live.get("bytes_per_sec", _UADE_BYTES_PER_SEC))
            return "ready_for_playback" if playable else "rendering"
    # Adaptive in-flight WAV transcode: once set up, the growing file already
    # plays.  Checked before the cache: a ``transcoded`` entry cached at
    # another target rate must not read as "complete" while this one runs.
    inflight = _INFLIGHT_TRANSCODES.get(track_id)
    if inflight is not None:
        ready = inflight.get("setup_ready")
        pump = inflight.get("pump_task")
        if ready is None or not ready.is_set():
            return "rendering"
        if pump is not None and not pump.done():
            return "ready_for_playback"
    # A progressive SID stream: rendering until its result is cached.
    for k, ev in list(_SID_PROG_DONE.items()):
        if not ev.is_set() and key_matches(k, track_id, subsong):
            return "rendering"
    state = render_state(track_id, subsong)
    if state != "idle":
        return state
    want = _prewarm_sub(subsong if wire == -1 else wire)
    for k, task in list(_prewarm_tasks.items()):
        tid, _, rest = k.partition("::")
        if tid == track_id and rest.rpartition("::")[2] == want and not task.done():
            return "queued"
    from soniqboom.core.conversion_cache import recent_failure
    if recent_failure(track_id, subsong) is not None:
        return "failed"
    return "idle"


@router.get("/{track_id}/transcode-status")
async def transcode_status(
    track_id: str,
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    request: Request = None,
):
    """Determinate progress for an in-flight transcode (DSD / ALAC / AIFF / …).

    Returns ``{ready, in_progress, percent, eta_seconds, target_duration}``.
    Frontend polls this while the converting badge is up so the indeterminate
    spinner can be swapped for a determinate bar with a visible ETA — the
    single biggest perceived-latency lever once the wait genuinely exceeds
    ~3 s (Hofman 2009; Card 1983).  Same sign-in as the stream itself.
    """
    await _require_stream_auth(request, sb_session, u, p, s, t)
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
# Who asked for each prewarm (``_prewarm_requester`` tags) — /prewarm/retain
# may only cancel work no OTHER listener still wants.
_prewarm_owner: dict[str, set] = {}
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


def _prewarm_sub(subsong: "int | None") -> str:
    """A prewarm key's tune part: the wire, or ``d`` for a bare play."""
    return "d" if subsong is None else str(int(subsong))


def _prewarm_key(track_id: str, fmt: str, subsong: "int | None" = None) -> str:
    return f"{track_id}::{fmt}::{_prewarm_sub(subsong)}"


# One prewarm download at a time per remote share (see ``_do_prewarm``).
_REMOTE_PREWARM_GATES: dict[str, asyncio.Semaphore] = {}


def _remote_share_and_rel(path_str: str) -> "tuple[str, str] | None":
    """``(share, path of the file to fetch)`` for a remote track — the OUTER
    archive for a ``::member`` path — or None for a local one."""
    if not is_remote_path(path_str):
        return None
    from soniqboom.core.filesource import parse_remote_path
    scan_root, remote_path = parse_remote_path(path_str)
    if not remote_path:
        return None
    return scan_root, remote_path.split("::", 1)[0]


def _remote_prewarm_gate(path_str: str) -> "asyncio.Semaphore | None":
    sr = _remote_share_and_rel(path_str)
    if sr is None:
        return None
    gate = _REMOTE_PREWARM_GATES.get(sr[0])
    if gate is None:
        gate = _REMOTE_PREWARM_GATES.setdefault(sr[0], asyncio.Semaphore(1))
    return gate


def _remote_bytes_local(path_str: str) -> bool:
    """A remote track whose file (or outer archive) is already in the local
    remote cache — or, for a member of a remote ZIP, the subset a play of it
    left there (``core.remote_zip``) — a prewarm then fetches nothing over
    the network."""
    sr = _remote_share_and_rel(path_str)
    if sr is None:
        return True
    try:
        from soniqboom.core.remote_cache import get_cache
        if get_cache().get_cached(*sr) is not None:
            return True
        from soniqboom.core.filesource import parse_remote_path
        member = parse_remote_path(path_str)[1].partition("::")[2]
        return bool(member) and _remote_zip.cached_subset(sr[0], sr[1], member) is not None
    except Exception:
        return False


async def _resolve_for_prewarm(track_id: str, track) -> tuple[Path, str, bool, str | None]:
    """``_resolve_play_source`` for a prewarm: every remote read on the
    ``"scan"`` lane, and at most one prewarm download per remote share at a
    time.  A prewarm cancelled while its download runs (a download in a
    worker thread can't be stopped) keeps the share's turn until that
    download ends — so the next queued one never starts a second download
    beside it — and releases what it resolved (the extract pin)."""
    gate = _remote_prewarm_gate(getattr(track, "path", "") or "")
    if gate is None:
        return await _resolve_play_source(track_id, track, lane="scan")
    async with gate:
        inner = asyncio.ensure_future(_resolve_play_source(track_id, track, lane="scan"))
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            def _drop(f: "asyncio.Future") -> None:
                if f.cancelled() or f.exception() is not None:
                    return
                pin = f.result()[3]
                if pin:
                    try:
                        _zip_unpin(pin)
                    except Exception:
                        pass
            inner.add_done_callback(_drop)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({inner})
            raise


async def _do_prewarm(
    track_id: str, track, subsong: "int | None", priority: int = PRIO_AHEAD,
) -> None:
    """Background-render one track under the low-priority prewarm gate.

    ``_bg_render_sem`` caps ALL background renders (this prewarm + the AdLib
    probe batch) at ``_RENDER_SLOTS - 1`` in aggregate and is held across the
    inner render (which itself acquires ``_render_sem``), so a speculative
    render never starves the foreground stream path of its reserved slot.  A
    FIFO-cap cancellation that fires while we're still waiting on the gate
    raises ``CancelledError`` straight out of the ``async with`` — the inner
    render never starts; one cancelled while its render runs keeps the slot
    until that render (which fills the cache regardless) ends.

    The source is resolved exactly like playback (remote fetch, archive member
    with its Amiga name + companions) BEFORE taking a render slot — a network
    fetch must not sit on CPU slots — and the gate serves the track that plays
    NEXT before further lookahead, duration probes and VU passes.

    Remote sources are fetched on the ``"scan"`` lane (never the one a
    listener's play waits on) and one prewarm fetch at a time per share: a
    queued one waits on an asyncio primitive, so the FIFO cap and
    /prewarm/retain really cancel it (only a download already running can't
    be stopped — see ``_resolve_for_prewarm``)."""
    pin_id = None
    try:
        try:
            file_path, ext, uade_named, pin_id = await _resolve_for_prewarm(track_id, track)
        except HTTPException as exc:
            log.info("Prewarm skipped for %s: %s", track_id, exc.detail)
            return
        if ext in NATIVE and not uade_named:
            return
        async with _bg_render_sem.slot(priority):
            await _hold_until_done(_do_prewarm_render(
                track_id, file_path, ext, subsong, uade_named=uade_named, track=track))
    finally:
        if pin_id:
            try:
                _zip_unpin(pin_id)
            except Exception:
                pass


async def _do_prewarm_render(
    track_id: str, file_path: Path, ext: str, subsong: "int | None",
    *, uade_named: bool = False, track=None,
) -> None:
    """Run the format-appropriate cached render for one track in the
    background.  Mirrors the routing in ``stream_track`` so the cache key
    matches exactly what playback will request later — the tune asked for
    (``subsong``), or None for the file's default tune, after the same
    default-tune probe (which a prewarm usually runs first, in the
    background)."""
    from soniqboom.core.conversion_cache import get_or_render
    try:
        c64 = ext in _SID_EXTS and _is_c64_sid(file_path)
        if (ext == ".psf" and not uade_named
                and not await asyncio.to_thread(_psf_has_magic, file_path)):
            uade_named = True              # Amiga SoundFactory, as in stream_track
        if subsong is None:              # the file's default tune (probed once)
            track = await _bare_play_track(
                track_id, track, file_path, _probe_family(ext, uade_named, file_path, c64=c64))
        subsong = tune_index(track_id, track, subsong)
        if c64:
            # Honour HVSC per-tune duration so the prewarm caches under the
            # same key the streaming path uses.
            track = await get_track(track_id)
            if track is not None:
                await sid_start_song(track_id, track, file_path)
            target_dur = (_sid_target_seconds(track, subsong) if track is not None
                          else max(5, min(int(settings.sid_default_duration), 3600)))
            full_key = _ck(track_id, "sid", subsong=subsong, duration=target_dur)
            prog = _SID_PROG_DONE.get(full_key)
            if prog is not None and not prog.is_set():
                return      # a play is rendering it progressively — and caches it
            # Registered while it renders: a click on this tune retires it and
            # plays progressively instead of starting a second render beside it.
            await get_or_render(
                track_id=track_id, format_type="sid", subsong=subsong,
                duration=target_dur,
                render_fn=lambda: _sid_prewarm_render(full_key, file_path, subsong,
                                                      target_dur),
            )
        elif ext in _MIDI_EXTS:
            # Live renders from here on (MIDI, PSF, SNDH, AdLib, libgme, uade):
            # a click while one is still running attaches and plays the
            # growing file.
            from soniqboom.config import get_active_soundfont
            sf = get_active_soundfont()
            _key = _ck(track_id, "midi", 0, str(sf) if sf else "")
            await get_or_render(
                track_id=track_id, format_type="midi", subsong=0,
                soundfont_path=str(sf) if sf else "",
                render_fn=lambda: _render_midi(file_path, live_key=_key),
            )
        elif ext in _HVL_EXTS:
            await get_or_render(
                track_id=track_id, format_type="hvl", subsong=subsong,
                render_fn=lambda: _render_hvl(file_path, subsong=subsong),
            )
        elif (ext in _PSF_STREAM_EXTS and not uade_named) or (
                ext == ".dsf" and _dsf_is_dreamcast(file_path)):
            if not _prewarm_enabled():
                return      # a Dreamcast rip is a render ("Prepare upcoming" off)
            _key = _ck(track_id, "psf", subsong=0)
            _exp = await asyncio.to_thread(_psf_render_seconds, file_path)
            await get_or_render(
                track_id=track_id, format_type="psf", subsong=0,
                render_fn=lambda: _render_psf(file_path, live_key=_key,
                                              expected_seconds=_exp),
            )
        elif ext in _SNDH_EXTS:
            _key = _ck(track_id, "sndh", subsong=subsong)
            await get_or_render(
                track_id=track_id, format_type="sndh", subsong=subsong,
                render_fn=lambda: _render_sndh(file_path, subsong=subsong,
                                               track_id=track_id, live_key=_key),
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
                and (ext in _UADE_EXTS or ext in _SID_EXTS or uade_named
                     or _uade_name_routes(file_path.name, file_path.suffix.lower(),
                                          track))):
            # uade family: suffix tokens, Amiga prefix-form names, and
            # magic-less .sid (SidMon — real C64 PSID returned above).  AdLib
            # extensions are excluded so an AMUSIC ``star.amd`` (uade ``star``
            # prefix collision) prewarms via AdPlug, matching playback.
            # Registered as a LIVE render: a click while it is still running
            # attaches and starts playing from the growing file.
            _base = await _uade_resolve_base(track_id, track, file_path, subsong)
            _ukey = uade_cache_key(track_id, subsong, _base)
            _exp = (_uade_expected_seconds(track, subsong, track_id)
                    if track is not None else 0.0)
            await get_or_render(
                track_id=track_id, format_type="uade", subsong=subsong,
                render_fn=lambda: _render_uade(file_path, subsong=subsong,
                                               live_key=_ukey, expected_seconds=_exp,
                                               subsong_base=_base),
                variant=uade_cache_variant(subsong, _base),
            )
        elif ext == ".imf" or ext in _ADLIB_EXTS:
            _fmt = "imf" if ext == ".imf" else "adlib"
            _key = _ck(track_id, _fmt, subsong=subsong)
            _exp = _stored_render_seconds(track, subsong, track_id,
                                          placeholder=_ADLIB_PLACEHOLDER_S)
            _rfn = _render_imf if ext == ".imf" else _render_adlib
            await get_or_render(
                track_id=track_id, format_type=_fmt, subsong=subsong,
                render_fn=lambda: _rfn(file_path, subsong=subsong, live_key=_key,
                                       expected_seconds=_exp),
            )
        elif ext in _TRACKER_EXTS:
            await get_or_render(
                track_id=track_id, format_type="tracker", subsong=subsong,
                render_fn=lambda: _render_tracker(file_path, subsong=subsong),
            )
        elif ext in _GME_EXTS_STREAM:
            _key = _ck(track_id, "gme", subsong=subsong)
            _exp = (0.0 if ext == ".gym" else _stored_render_seconds(
                track, subsong, track_id, placeholder=float(settings.sid_default_duration)))
            await get_or_render(
                track_id=track_id, format_type="gme", subsong=subsong,
                render_fn=lambda: _render_gme(file_path, subsong=subsong, live_key=_key,
                                              expected_seconds=_exp),
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
            # Shielded: the pump is SHARED — a listener may be streaming the
            # growing file right now.  Cancelling this prewarm (FIFO cap,
            # /prewarm/retain) must only end the prewarm, never the pump
            # (which then finishes and caches, like every other prewarm).
            await asyncio.shield(inflight["pump_task"])
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
            await asyncio.shield(inflight["pump_task"])   # shared pump — see DSD above
        # Native formats need no prewarm — the browser HTTP cache + our
        # range handler handle it; the original 256 KB-Range trick in the
        # frontend covers them.
    except asyncio.CancelledError:
        log.debug("Prewarm cancelled for %s", track_id)
        raise
    except Exception as exc:
        log.info("Prewarm failed for %s (%s) — will render on demand: %s",
                 track_id, ext, exc)


def _prewarm_requester(request: Request, sb_session: str | None, u: str | None,
                       pw: str | None = None) -> str:
    """Opaque per-listener tag so ``/prewarm/retain`` only drops the CALLER's
    own speculative work, never another listener's.  ``pw`` is a per-page id
    the player sends, so two tabs of one browser (one session cookie) are two
    owners; a caller without one keeps the per-session tag."""
    import hashlib
    raw = (sb_session or (f"u:{u}" if u else "")
           or (request.client.host if request is not None and request.client else ""))
    if isinstance(pw, str) and pw:       # (a direct call may pass the Query default)
        raw = f"{raw}:{pw}"
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _prewarm_enabled() -> bool:
    try:
        from soniqboom.core.data import get_store
        return bool(get_store().get_config("render_prewarm", True))
    except Exception:
        return True


def _is_rendered_ext(ext: str, uade_named: bool = False) -> bool:
    """Is this a RENDERED format (a replayer turns it into audio: SID, Amiga,
    trackers, chip formats, MIDI, AdLib …) rather than a native file or one
    ffmpeg converts (DSD, ALAC, AIFF, WavPack, Musepack …)?  ``ext`` is
    ``_render_ident``'s.  A bare ``.dsf`` counts as DSD — a Dreamcast rip is
    only known once the file is local.  Name-only, O(1)."""
    return bool(uade_named) or ext == ".imf" or any(ext in s for s in (
        _SID_EXTS, _MIDI_EXTS, _HVL_EXTS, _PSF_STREAM_EXTS, _SNDH_EXTS,
        _YM_EXTS, _SC68_EXTS, _UADE_EXTS, _ADLIB_EXTS, _TRACKER_EXTS,
        _GME_EXTS_STREAM))


def _prewarm_allowed(ext: str, uade_named: bool = False) -> bool:
    """May a track with this ``_render_ident`` verdict be prepared ahead?
    "Prepare upcoming tracks" (``render_prewarm``) gates the rendered
    formats only; conversions (DSD, ALAC, AIFF, …) are always prepared."""
    return _prewarm_enabled() or not _is_rendered_ext(ext, uade_named)


@router.post("/{track_id}/prewarm")
async def prewarm(
    track_id: str,
    subsong: int | None = Query(default=None, ge=0),
    priority: str = Query(default="ahead", pattern="^(next|ahead)$"),
    file_path: str | None = Query(default=None, alias="path"),
    pw: str | None = Query(default=None, max_length=64),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    request: Request = None,
):
    """Speculatively prepare a track's cached render in the background.

    Used by the player's look-ahead (the track that plays next, as soon as the
    current one is under way) and by hover-to-click.  ``priority=next`` jumps
    the background queue ahead of further lookahead and duration probes;
    hover-to-click sends the default ``ahead``, so it waits behind the next
    track and never fetches a remote file that isn't cached yet.
    Returns immediately with a status summary; the render happens off-request.
    Remote and archive-contained tracks are fetched/extracted exactly like
    playback would — a remote file not in the local cache yet only for
    ``priority=next``.  ``pw`` tags the calling page (see ``prewarm_retain``).
    With the ``render_prewarm`` setting off, rendered formats (retro, Amiga)
    are skipped; conversions (DSD, ALAC, AIFF, …) are still prepared.
    """
    await _require_stream_auth(request, sb_session, u, p, s, t)
    track = await get_track(track_id)
    if not track:
        if file_path:
            track = await _ingest_on_demand(track_id, file_path)
        if not track:
            raise HTTPException(404, "Track not found")

    ext, uade_named = _render_ident(track.path, track)
    # Native formats need no server-side prewarm.
    if ext in NATIVE and not uade_named:
        return {"status": "skipped", "reason": "native (no transcode needed)"}
    if not _prewarm_allowed(ext, uade_named):
        return {"status": "skipped", "reason": "prewarm disabled in settings"}
    # A remote file not yet in the local cache is fetched only for the track
    # that plays NEXT: hover and further lookahead must not pull whole
    # multi-hundred-MB archives or DSD files across the share.
    if priority != "next" and not _remote_bytes_local(track.path or ""):
        return {"status": "skipped", "reason": "remote lookahead"}

    prio = PRIO_NEXT if priority == "next" else PRIO_AHEAD
    who = _prewarm_requester(request, sb_session, u, pw)
    return _schedule_prewarm(track_id, track, explicit_wire(subsong, request), prio, who,
                             ext, named=True)


def _schedule_prewarm(track_id: str, track, subsong: "int | None", prio: int, who: str,
                      ext: str | None = None, *, named: bool = False) -> dict:
    """Register one prewarm in the shared registry (the web player's
    ``POST /prewarm`` and Subsonic's next-track prewarm): join a task already
    running for the same (track, format, tune) — refreshing its recency and
    adding ``who`` as an owner — else start ``_do_prewarm``; then keep the
    registry within ``_PREWARM_CAP``, cancelling the oldest task whose track
    isn't pinned (recently played) first.  ``ext`` defaults to
    ``_render_ident``'s.  Callers apply their own gates (setting, native,
    remote lookahead) first.  Plain work on the event loop, no awaits.
    ``subsong``: the tune asked for, None for the file's default; without
    ``named`` (Subsonic, whose ``<id>~0`` is the bare id) a 0 is the default
    too.  Returns the endpoint's status summary."""
    if ext is None:
        ext = _render_ident(getattr(track, "path", "") or "", track)[0]
    if not named:
        subsong = subsong or None
    key = _prewarm_key(track_id, ext, subsong)
    existing = _prewarm_tasks.get(key)
    if existing is not None and not existing.done():
        # Refresh recency — keep this task alive when capacity pressure hits.
        _prewarm_tasks.move_to_end(key)
        _prewarm_owner.setdefault(key, set()).add(who)
        return {"status": "already_running", "key": key}

    task = asyncio.create_task(_do_prewarm(track_id, track, subsong, prio))
    _prewarm_tasks[key] = task
    _prewarm_owner[key] = {who}

    def _on_done(t: asyncio.Task) -> None:
        # Identity check: only pop if the registry still points at *this*
        # task.  A fresh prewarm for the same key may have arrived between
        # this task's completion and the callback running — popping
        # unconditionally would remove the SUCCESSOR (orphaning it from
        # the cap accounting + shutdown cleanup).
        if _prewarm_tasks.get(key) is t:
            _prewarm_tasks.pop(key, None)
            _prewarm_owner.pop(key, None)
    task.add_done_callback(_on_done)

    # FIFO cap: cancel the oldest in-flight prewarm if we're over budget.
    # Skip any prewarm whose track_id is currently pinned in the cache
    # (i.e. recently played) — those represent work the user is likely
    # still consuming, and cancelling them would force the next play to
    # re-render.  Falls back to plain FIFO if every task is pinned.
    from soniqboom.core.conversion_cache import _pin_refs as _cache_pinned
    while len(_prewarm_tasks) > _PREWARM_CAP:
        evict_key: str | None = None
        for k in _prewarm_tasks:
            # Prewarm key is ``"{track_id}::{ext}::{subsong}"``.
            # ``_pin_refs`` is a dict { cache_key -> refcount }; we iterate
            # the keys to mirror the legacy ``_pinned`` set semantics.
            tid = k.split("::", 1)[0]
            if not any(p.startswith(tid) for p in _cache_pinned):
                evict_key = k
                break
        if evict_key is None:
            # Everything left is pinned — fall back to oldest.
            evict_key = next(iter(_prewarm_tasks))
            log.debug("Prewarm cap reached + all pinned — cancelling %s anyway", evict_key)
        old_task = _prewarm_tasks.pop(evict_key)
        _prewarm_owner.pop(evict_key, None)
        if not old_task.done():
            old_task.cancel()
            log.debug("Prewarm cap reached — cancelled %s", evict_key)

    return {"status": "queued", "key": key, "in_flight": len(_prewarm_tasks)}


@router.post("/prewarm/retain")
async def prewarm_retain(
    payload: dict = Body(...),
    pw: str | None = Query(default=None, max_length=64),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
    request: Request = None,
):
    """Drop the CALLER's queued prewarms for tracks no longer coming up.

    The player calls this when the listener skips or the queue changes, with
    the ids it still expects to play.  A prewarm still waiting for a slot (or
    fetching its source) is cancelled so CPU goes to what will actually play;
    one that is already rendering keeps going — its result is cached anyway.
    Ownership is per page (``pw``, one per tab): another listener's — or
    another tab's — prewarms are never touched."""
    await _require_stream_auth(request, sb_session, u, p, s, t)
    keep = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(keep, list):
        return {"cancelled": 0}
    keep_ids = {str(x) for x in keep[:50]}
    who = _prewarm_requester(request, sb_session, u, pw)
    cancelled = 0
    for k in list(_prewarm_tasks):
        owners = _prewarm_owner.get(k) or set()
        if who not in owners or k.split("::", 1)[0] in keep_ids:
            continue
        owners.discard(who)
        if owners:                       # someone else still wants it
            continue
        task = _prewarm_tasks.pop(k, None)
        _prewarm_owner.pop(k, None)
        if task is not None and not task.done():
            task.cancel()
            cancelled += 1
    return {"cancelled": cancelled}


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
    With "Prepare upcoming tracks" (``render_prewarm``) off, rows whose length
    only a full render can tell (Amiga, HVL, SC68, PSF) are skipped — they get
    their length on first play — and are absent from the answer.
    """
    await _require_stream_auth(request, sb_session, None, None)
    ids = payload.get("track_ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        return {}
    ids = [str(x) for x in ids][:200]          # cap the batch
    # Share the single low-priority background gate with web prewarm so probes
    # + prewarms in AGGREGATE never occupy the render slot reserved for a live
    # play (``_bg_render_sem`` = _RENDER_SLOTS-1; foreground never acquires it).
    pending: list[tuple[str, dict]] = []
    # "Prepare upcoming tracks" off = low power: no full renders (Amiga, HVL,
    # SC68, PSF) just to learn a length — those learn it on first play.
    full = _prewarm_enabled()

    async def _one(tid: str):
        # Each probe takes its own PRIO_PROBE slot around the render only —
        # resolving (and any download) happens before it.
        try:
            return tid, await _probe_one_rendered_duration(
                tid, sink=pending, allow_full_render=full)
        except Exception:
            return tid, None

    out = await asyncio.gather(*[_one(t) for t in ids])
    if pending:
        # ONE store write for the whole batch (one AOF record, one mutation
        # bump) instead of one per probed row.  Cosmetic: never fail on it.
        try:
            from soniqboom.core.store import get_store
            get_store().update_track_fields_batch(pending)
        except Exception:
            log.debug("probe-durations batch write failed", exc_info=True)
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
            if not is_remote_path(str(r.get("path", "")))
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
        from soniqboom.core import game_titles
        if '::' in file_path and game_titles.platforms_for(track.format)[0]:
            # A retro archive member: its game may come from the archive's name.
            game_titles.schedule()
        return track
    except Exception as exc:
        log.error("On-demand ingest upsert failed for %s: %s", file_path, exc)
        return None


async def _stream_scrypt_check(store, u: str, plain: str):
    """The scrypt fallback of a ``p=`` stream sign-in, off the event loop.

    Shares /rest's 2-slot gate (``subsonic._scrypt_semaphore``), so a burst of
    wrong passwords queues there instead of blocking the loop for ~60 ms each
    or starting many ~32 MB scrypt workers at once; and /rest's callable
    (``subsonic._p_authenticate``): misses count in the ``p`` lockout scope, so
    a device stuck on a stale password can't lock the web login."""
    from soniqboom.api import subsonic as _ss
    async with _ss._scrypt_semaphore():
        return await asyncio.to_thread(_ss._p_authenticate(store), u, plain)


async def _require_stream_auth(
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
    event loop for nearly a second per track switch.  Pen-test #1 P0-2.
    Every check is O(1) on the loop except a ``p=`` that misses both fast
    paths: that one scrypt runs in a worker thread (``_stream_scrypt_check``).
    ``p=`` follows /rest's lockout rules: refused while the main or the ``p``
    scope is locked, and its misses count in the ``p`` scope only."""
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
    _is_locked = getattr(store, "is_locked", None)
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
        # Fast paths first, both O(1), both refused for a locked-out account
        # (the same rules as /rest — subsonic._resolve_user):
        #   1. the Subsonic API password — a constant-time compare against the
        #      stored value, which is the app password set via PUT
        #      /api/me/subsonic-password, or the login password seeded ONCE on
        #      the first successful login while it was unset (never re-synced);
        #   2. a login password this process already verified with scrypt
        #      (``check_cached_password``) — one scrypt per process, then O(1).
        # Only a miss on both pays the scrypt check (~60 ms, and the
        # last-login bookkeeping) — off the loop, behind /rest's gate.
        import hmac as _hmac
        cand = store.get_by_username(u) if hasattr(store, "get_by_username") else None
        if cand and cand.enabled and not (
                _is_locked and (_is_locked(u) or _is_locked(u, "p"))):
            if (cand.subsonic_password
                    and _hmac.compare_digest(plain.encode("utf-8"),
                                             cand.subsonic_password.encode("utf-8"))):
                return
            cached = getattr(store, "check_cached_password", None)
            if cached is not None and cached(u, plain) is not None:
                return
        if await _stream_scrypt_check(store, u, plain):
            return
    if u and s is not None and t is not None:
        # Subsonic token mode.  The token is md5(subsonic_password + salt);
        # we recompute and constant-time compare.  Same convention every
        # Subsonic-compatible server uses — see subsonic._resolve_user —
        # including its own lockout scope: wrong tokens (or an unknown user)
        # count as failed guesses, and a locked-out user is refused even with
        # a right one.  A right token for a disabled account is refused
        # without counting as a guess.
        if _is_locked and _is_locked(u, "token"):
            raise HTTPException(401, "Sign in to stream tracks.")
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
        note = getattr(store, "note_failed_attempt", None)
        if note is not None:
            note(u, "token")
    raise HTTPException(401, "Sign in to stream tracks.")


async def _resolve_play_source(track_id: str, track, *,
                               lane: str = "stream") -> tuple[Path, str, bool, str | None]:
    """Turn a track's stored path into a LOCAL file a renderer can open:
    remote shares are fetched to the remote cache, archive members extracted
    (with their Amiga name and companion halves restored), loose remote uade
    modules / AdLib tunes materialized beside their companions.

    Returns ``(path, ext, uade_named, pin_id)``.  ``pin_id`` is set when an
    extraction was pinned against eviction — the caller MUST ``_zip_unpin``
    it once done with ``path``.  Shared by playback and prewarm so a prewarm
    renders exactly what playback would (a render prepared differently from
    the one playback expects poisons the shared cache entry).

    ``lane`` is the remote-read priority lane every fetch uses: playback keeps
    ``"stream"``; a prewarm passes ``"scan"`` so speculative downloads never
    ride the lane a listener's play is waiting on.
    """
    path_str = track.path
    # ZIP-cache pin holder.  Initialise BEFORE the path-resolution branches
    # so the cleanup function (line ~2445) can reference it regardless of
    # which branch ran.  Without this default the remote (smb:// / ftp://)
    # branch never set the variable → UnboundLocalError → every remote
    # track 500'd on first byte (validation finding 2026-05-21).
    _zip_track_id_for_unpin: str | None = None

    if is_remote_path(path_str) and "::" in path_str:
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
        zip_rel, _member = remote_path.split("::", 1)
        if source is None and not (
                get_cache().peek_cached(scan_root, zip_rel)
                or _remote_zip.cached_subset(scan_root, zip_rel, _member) is not None):
            raise _offline_error(scan_root)     # (cached bytes play while it's down)
        loop = asyncio.get_running_loop()
        try:
            # Only the member's bytes (and its companions) when the archive
            # isn't cached — not the whole archive (``core.remote_zip``).
            _local_zip = await loop.run_in_executor(
                None, functools.partial(
                    _remote_zip.archive_for_member, scan_root, zip_rel, _member,
                    source, lane=lane, companion=_archive_companion_filter(_member)),
            )
        except Exception as exc:
            if source is None:                  # its cached copy went meanwhile
                raise _offline_error(scan_root)
            if _is_file_not_found(exc):
                raise HTTPException(404, "Archive missing on source (rescan to refresh)")
            log.warning("Remote archive fetch failed for %s: %s", path_str, exc)
            raise HTTPException(502, "Could not fetch archive from network share")
        path = await _get_or_extract_zip_member(
            f"{_local_zip}::{_member}", track_id,
            bank_fallback=(_make_zip_bank_fallback(remote=(zip_rel, source), lane=lane)
                           if source is not None else None),
        )
        if path is None:
            raise HTTPException(404, "Track missing inside the archive")
        _zip_pin(track_id)
        _zip_track_id_for_unpin = track_id
    elif is_remote_path(path_str):
        from soniqboom.core.filesource import get_source, parse_remote_path
        from soniqboom.core.remote_cache import get_cache
        scan_root, remote_path = parse_remote_path(path_str)
        if not remote_path:
            raise HTTPException(400, "Remote path is malformed")
        source = get_source(scan_root)
        if source is None:
            # Not connected (down since startup): a copy in the remote cache
            # still plays.
            cached = get_cache().get_cached(scan_root, remote_path)
            if cached is None:
                raise _offline_error(scan_root)

        # Try once; on failure ask the source to rebuild its connection and
        # retry ONE more time.  The source's own _connect already does
        # short inline retries, so this is the second tier: a brand-new
        # TCP session in case the pooled connection has been torn down
        # by the peer (FTP idle timeout, SMB session expire, router NAT flush).
        if source is None:
            path = cached
        else:
            loop = asyncio.get_running_loop()
            try:
                path = await loop.run_in_executor(
                    None, functools.partial(get_cache().fetch, scan_root, remote_path,
                                            source, lane=lane),
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
                            None, functools.partial(get_cache().fetch, scan_root,
                                                    remote_path, source, lane=lane),
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
            # An archive on a drive / mount that is offline is not "missing".
            root = await _offline_root_for(track)
            if root:
                raise _offline_error(root)
            raise HTTPException(410, "ZIP archive not found or unreadable")
        # Pin the extract for the duration of the response so eviction
        # can't unlink a file we're mid-stream.  Unpin runs in the
        # response's BackgroundTask below.
        _zip_pin(track_id)
        _zip_track_id_for_unpin = track_id
    else:
        path = Path(path_str)
        if not path.exists():
            # An ejected drive / dropped mount is not a missing file: say the
            # source is offline (checked only on this failure path).
            root = await _offline_root_for(track)
            if root:
                raise _offline_error(root)
            raise HTTPException(410, f"File not found on disk: {track.path}")

    # Amiga prefix-form names (mdat.song) carry no token extension — detect by
    # name so they route to uade below.  ``_render_ident`` also keeps AdLib
    # (AMUSIC ``star.amd`` etc.) out of the uade path — see its docstring.
    ext, _uade_named = _render_ident(path_str, track)

    # Loose (non-zip) uade modules on a remote share: materialize module +
    # companion halves (TFMX smpl.X …) into one dir, mirroring AdLib below.
    if (is_remote_path(path_str) and "::" not in path_str
            and (_uade_named or ext in _UADE_EXTS) and ext != ".ahx"):
        try:
            from soniqboom.core.filesource import get_source, parse_remote_path
            _sr, _rp = parse_remote_path(path_str)
            _src = get_source(_sr)
            if _src is not None:
                _mat = await _materialize_loose_remote_uade(track_id, _sr, _rp, _src,
                                                            lane=lane)
                if _mat is not None:
                    path = _mat
                    _zip_pin(track_id)
                    _zip_track_id_for_unpin = track_id
        except Exception as exc:
            log.info("Loose UADE companion materialize failed for %s: %s", path_str, exc)

    # Loose (non-zip) AdLib tunes on a remote share need their companion bank
    # materialized in the same dir; the per-file fetch above split them apart.
    # Re-point ``path`` to a dir holding tune + bank (no-op if no bank sibling).
    if (is_remote_path(path_str) and "::" not in path_str
            and ext in _ADLIB_COMPANION_GLOBS):
        try:
            from soniqboom.core.filesource import get_source, parse_remote_path
            _sr, _rp = parse_remote_path(path_str)
            _src = get_source(_sr)
            if _src is not None:
                _mat = await _materialize_loose_remote_adlib(
                    track_id, _sr, _rp, _src, _ADLIB_COMPANION_GLOBS[ext], lane=lane,
                )
                if _mat is not None:
                    path = _mat
                    # Pin so a concurrent clear/eviction can't rmtree the bank
                    # mid-render (mirrors the zip-member play paths).
                    _zip_pin(track_id)
                    _zip_track_id_for_unpin = track_id
        except Exception as exc:
            log.info("Loose AdLib companion materialize failed for %s: %s", path_str, exc)

    return path, ext, _uade_named, _zip_track_id_for_unpin


# ── Client-requested delivery changes (format= / maxBitRate= / time offset) ──
# Subsonic clients may ask for another codec, a bitrate cap or a start offset
# (``timeOffset``).  Unconstrained requests — every web-UI play — never reach
# any of this: they keep the direct / Range / progressive paths unchanged.

class _Xform:
    """What a client asked to change about a RENDERED track's audio: another
    codec (``format=``), a bitrate cap (kbps) and/or a start offset (s).
    Rendered formats always render to WAV first; this says what happens to
    that WAV before it is sent."""

    __slots__ = ("codec", "bitrate", "seek")

    def __init__(self, codec: str | None = None, bitrate: int = 0,
                 seek: float = 0.0) -> None:
        self.codec = codec
        self.bitrate = int(bitrate or 0)
        self.seek = float(seek or 0.0)

    @property
    def active(self) -> bool:
        return bool(self.codec) or self.seek > 0


_RENDERED_WAV_KBPS = 1411       # 44.1 kHz / 16-bit / stereo PCM


def _rendered_xform(target_format: str | None, max_bitrate_kbps: int,
                    seek: float) -> _Xform:
    """The delivery change a request asks of a rendered (WAV) track.

    ``format=`` mp3/ogg/flac is honoured (flac is lossless — no bitrate); a
    cap below the WAV's 1411 kbps needs a lossy codec, so it picks mp3 unless
    mp3/ogg was asked for.  ``format=raw`` means "no transcoding": plain WAV,
    no offset (the Subsonic time offset applies to transcoded streams)."""
    want = (target_format or "").strip().lower()
    if want == "raw":
        return _Xform()
    codec = want if want in TRANSCODE_MIME else None
    cap = max_bitrate_kbps if 0 < max_bitrate_kbps < _RENDERED_WAV_KBPS else 0
    if cap and codec in (None, "flac"):
        codec = "mp3"
    return _Xform(codec, cap if codec and codec != "flac" else 0, seek)


_FFMPEG_ENCODERS: "set[str] | None" = None


async def _ffmpeg_encoders() -> set[str]:
    """Encoder names this ffmpeg build offers (probed once, off the loop)."""
    global _FFMPEG_ENCODERS
    if _FFMPEG_ENCODERS is None:
        def _probe() -> set[str]:
            try:
                out = forksafe.run(              # a worker thread: never a fork
                    [settings.ffmpeg_path or "ffmpeg", "-hide_banner", "-encoders"],
                    capture_output=True, timeout=15, text=True).stdout
            except Exception:
                return set()
            names = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "AVS.":
                    names.add(parts[1])
            return names
        _FFMPEG_ENCODERS = await asyncio.to_thread(_probe)
    return _FFMPEG_ENCODERS


async def _ffmpeg_encode_args(fmt: str, *, bitrate_kbps: int | None = None,
                              target_rate: int | None = None,
                              src_is_dsd: bool = False,
                              label: str = "") -> list[str]:
    """ffmpeg output arguments for ``fmt`` (flac / mp3 / ogg), shared by the
    cached transcode and the live (time-offset) pipe so the two never drift.

    ``ogg`` means Vorbis where this ffmpeg has libvorbis, else Opus-in-Ogg at
    48 kHz (the only encoder builds without libvorbis have; ``-acodec ogg`` —
    what this used to pass — names no encoder at all)."""
    args: list[str] = []
    acodec = "flac" if fmt == "flac" else fmt
    eff_rate = target_rate
    if fmt == "ogg":
        encs = await _ffmpeg_encoders()
        if "libvorbis" in encs or not encs:
            acodec = "libvorbis"
        elif "libopus" in encs:
            acodec, eff_rate = "libopus", 48000
        else:
            acodec = "vorbis"
            args += ["-strict", "-2"]
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
    _LOSSY_MAX_RATE = {"mp3": 48000, "ogg": 48000, "opus": 48000, "aac": 48000}
    if eff_rate and fmt in _LOSSY_MAX_RATE and eff_rate > _LOSSY_MAX_RATE[fmt]:
        log.info("Transcode: clamping %s output rate %d → %d Hz (encoder limit; "
                 "source %s)", fmt, eff_rate, _LOSSY_MAX_RATE[fmt], label)
        eff_rate = _LOSSY_MAX_RATE[fmt]
    if eff_rate:
        args += ["-ar", str(eff_rate)]
    if bitrate_kbps and fmt != "flac":
        # FLAC is lossless — bitrate is determined by content, not a knob.
        args += ["-b:a", f"{bitrate_kbps}k"]
    if src_is_dsd:
        # Same chain as ``_pump_pcm_to_wav`` — see that function's comment
        # for the full rationale.  The ``highpass=f=20`` is the load-
        # bearing fix: DSD's bit pattern for certain near-silence
        # segments decodes to a -1.0 DC rail instead of zero, which the
        # browser silences as a DC-bias speaker-protection event.
        args += ["-af", "highpass=f=20,lowpass=f=40000,volume=-6dB"]
    if fmt == "flac":
        # Cached output worth taking the time to compress properly —
        # level 5 is the FLAC reference default and produces ~30 % smaller
        # files than level 0 for ~3-5 % more encode time at this scale.
        args += ["-compression_level", "5"]
    args += ["-acodec", acodec]
    return args


# Background fills of the transcode cache, by cache key — one per key, so the
# requests of one cold play never queue a second encode of the same thing.
_TRANSCODE_FILLS: dict[str, "asyncio.Task"] = {}


def _fill_transcode_in_background(key: str, make, *, zip_pin_id: str | None = None,
                                  source_key: str | None = None) -> None:
    """Fill the transcode cache entry ``key`` off-request, while the client is
    already being answered from a live ffmpeg pipe: ``make()`` returns the
    ``conversion_cache.get_or_render`` coroutine that encodes the file.  Runs
    at ``PRIO_NEXT`` under the background render gate (a play is under way,
    but the listener isn't waiting on this), once per key.  Holds the zip
    extract pin (``zip_pin_id``) and the rendered-WAV cache pin
    (``source_key``) until it ends, so neither source is evicted mid-encode.
    Later requests — seeks, replays — then get the cached file with Range."""
    old = _TRANSCODE_FILLS.get(key)
    if old is not None and not old.done():
        return
    from soniqboom.core.conversion_cache import pin, unpin
    if zip_pin_id:
        _zip_pin(zip_pin_id)
    if source_key:
        pin(source_key)

    async def _run():
        try:
            async with _bg_render_sem.slot(PRIO_NEXT):
                await make()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("Background transcode fill failed for %s: %s", key, exc)
        finally:
            if source_key:
                unpin(source_key)
            if zip_pin_id:
                try:
                    _zip_unpin(zip_pin_id)
                except Exception:
                    pass
            if _TRANSCODE_FILLS.get(key) is task:
                _TRANSCODE_FILLS.pop(key, None)

    task = asyncio.ensure_future(_run())
    _TRANSCODE_FILLS[key] = task
    _bg_keep(task)


# Layer III bitrates (kbps) — MPEG-1 (output at 32-48 kHz) and MPEG-2/2.5
# (lower rates).  LAME encodes a CBR request at the nearest entry of its table,
# ties going down (measured at 44.1 kHz: 100→96, 104→96, 144→128, 150→160,
# 400→320, 8→32; at 22.05 kHz: 140→144, 320→160).
_MP3_KBPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MP3_KBPS_LSF = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_MP3_DEFAULT_KBPS = 128          # ffmpeg's libmp3lame default (measured)


def _mp3_cbr_kbps(requested: int, out_rate: int | None = None) -> int:
    """The CBR bitrate LAME encodes for ``requested`` kbps at output sample
    rate ``out_rate``; with the rate unknown, the larger of the two tables'
    answers, so an estimate built on it never falls short."""
    want = requested or _MP3_DEFAULT_KBPS

    def nearest(table):
        return min(table, key=lambda v: (abs(v - want), v))
    if out_rate:
        return nearest(_MP3_KBPS if out_rate >= 32000 else _MP3_KBPS_LSF)
    return max(nearest(_MP3_KBPS), nearest(_MP3_KBPS_LSF))


def _estimated_transcode_length(src: Path, xf: _Xform, copy: bool,
                                target_rate: int | None = None,
                                src_seconds: float | None = None) -> int | None:
    """Content-Length to announce for a live transcode when the client asked
    for an estimate (``_estimate_len_ctx``), else None.  Only for MP3: LAME's
    CBR output is (duration × bitrate) to within ~0.05 % (measured 1.0003-
    1.0005 incl. its tag and delay frames) — Opus/Vorbis VBR ran 13-28 % over
    its target and FLAC is content-dependent, so those stay chunked.  A
    rendered WAV's own header gives the exact length and sample rate.
    Pitched slightly high (+0.5 % and 16 KB): the body is then zero-padded
    to it, where truncation would cut the end of the track.  ``src_seconds``:
    the exact length of a rendered (44.1 kHz) WAV that is still rendering —
    its file can't say yet."""
    hint = _estimate_len_ctx.get()
    if hint is None or copy or (xf.codec or "") != "mp3":
        return None
    dur, rate = hint, None
    layout = (_wav_layout(src) if src.suffix.lower() == ".wav" and src_seconds is None
              else None)
    if src_seconds is not None:
        dur, rate = float(src_seconds), _SID_WAV_RATE
    elif layout is not None and layout["rate"] > 0 and layout["block_align"] > 0:
        dur = layout["data_size"] / layout["block_align"] / layout["rate"]
        rate = layout["rate"]
    if target_rate:
        rate = min(int(target_rate), 48000)          # _ffmpeg_encode_args' clamp
    remaining = dur - xf.seek
    if not remaining > 0:
        return None
    return int(remaining * _mp3_cbr_kbps(xf.bitrate, rate) * 125 * 1.005) + 16384


async def _exactly(gen, total: int):
    """Yield exactly ``total`` bytes of the transcode ``gen``: cut at
    ``total`` (the encode is then stopped) or zero-padded when it ends short
    — the declared Content-Length must be met to the byte.  An encode that
    produced nothing is not padded: the short body fails the response."""
    sent = 0
    try:
        async for chunk in gen:
            if sent + len(chunk) >= total:
                yield chunk[:total - sent]
                sent = total
                break
            yield chunk
            sent += len(chunk)
    finally:
        await gen.aclose()
    if sent == 0:
        return
    while sent < total:
        n = min(65536, total - sent)
        yield bytes(n)
        sent += n


def _live_transcode_response(src: Path, xf: _Xform, headers: dict,
                             background, *, copy: bool = False,
                             target_rate: int | None = None, src_feed=None,
                             src_seconds: float | None = None) -> Response:
    """Stream ``src`` from ``xf.seek`` seconds through a live ffmpeg pipe in
    ``xf.codec`` (``copy``: remux an already-transcoded file without
    re-encoding).  First bytes in well under a second, no Content-Length —
    neither a time-offset start nor a cold transcode waits for a whole
    encode — except for a client that asked for an estimated length
    (``estimated_content_length``): an MP3 then carries one, and exactly that
    many bytes.  ``src_feed`` / ``src_seconds``: the source is a render still
    rendering, read as it grows (``_transcode_stream``), and its exact
    length when known."""
    hdrs = _accel_off(dict(headers or {}))
    hdrs["Accept-Ranges"] = "none"
    if xf.seek > 0:
        hdrs["X-Time-Offset"] = f"{xf.seek:.3f}"
    body = _transcode_stream(src, xf.seek, target_rate, codec=xf.codec,
                             bitrate_kbps=xf.bitrate or None, copy=copy,
                             src_feed=src_feed)
    est = _estimated_transcode_length(src, xf, copy, target_rate, src_seconds)
    if est is not None:
        hdrs["Content-Length"] = str(est)
        hdrs["X-Content-Length-Estimated"] = "1"
        body = _exactly(body, est)
    return _CleanupStreamingResponse(
        body, media_type=TRANSCODE_MIME.get(xf.codec or "", "audio/flac"),
        headers=hdrs, background=background)


async def _cached_rendered_transcode(track_id: str, xf: _Xform, source_key: str) -> "Path | None":
    """The cached transcode ``_serve_rendered`` would serve for the render of
    ``source_key`` in ``xf``'s codec / bitrate, if there is one."""
    from soniqboom.core.conversion_cache import get_cached, rendered_transcode_variant
    return await get_cached(_ck(track_id, "transcoded", 0, codec=xf.codec,
                                bitrate=xf.bitrate or None,
                                variant=rendered_transcode_variant(source_key)))


async def _serve_rendered_transcode_hit(request: Request, path: Path, xf: _Xform,
                                        headers: dict, background) -> Response:
    """A cached transcode of a render, with Range — as ``_serve_rendered``
    answers one."""
    hdrs = dict(headers or {})
    hdrs.update({"X-Transcoded": "1", "X-Original-Codec": "wav",
                 "X-Target-Codec": xf.codec, "X-Transcode-Cache": "hit"})
    return await _range_file_response(request, path, TRANSCODE_MIME[xf.codec],
                                      headers=hdrs, background=background)


async def _transcode_growing_render(request: Request, *, track_id: str, feed, xf: _Xform,
                                    source_key: str, rendered, headers: dict,
                                    background, src_seconds: float | None) -> Response:
    """A client asked for another codec (``xf.codec``, no time offset) of a
    render that is still running: answer at once from a live encode of the
    growing WAV (``feed``, read from byte 0); once the render is cached
    (``rendered()`` — an awaitable of ``(path, hit)``), the transcode cache
    entry is filled from it in the background (``_fill_transcode_in_background``),
    so later requests — seeks, replays — get that file with Range."""
    from soniqboom.core.conversion_cache import get_or_render, rendered_transcode_variant
    variant = rendered_transcode_variant(source_key)
    tkey = _ck(track_id, "transcoded", 0, codec=xf.codec,
               bitrate=xf.bitrate or None, variant=variant)

    async def _fill_once_rendered():
        try:
            wav, _ = await rendered()
        except Exception:
            return          # the render failed: the stream has said so
        _fill_transcode_in_background(
            tkey, lambda: get_or_render(
                track_id=track_id, format_type="transcoded", subsong=0,
                codec=xf.codec, bitrate=xf.bitrate or None, variant=variant,
                render_fn=lambda: _render_to_transcoded_flac(
                    wav, codec=xf.codec, bitrate_kbps=xf.bitrate or None)),
            source_key=source_key)
    _bg_keep(asyncio.ensure_future(_fill_once_rendered()))
    hdrs = dict(headers or {})
    hdrs.update({"X-Transcoded": "1", "X-Original-Codec": "wav",
                 "X-Target-Codec": xf.codec, "X-Transcode-Cache": "miss"})
    return _live_transcode_response(Path(f"{track_id}.wav"), xf, hdrs, background,
                                    src_feed=feed, src_seconds=src_seconds)


async def _wav_offset_response(request: Request, wav_path: Path, seek: float,
                               headers: dict | None, background) -> Response:
    """A finished WAV served from ``seek`` seconds on: a fresh header sized to
    the remaining audio, then the file from the matching frame — O(1), no
    decoding.  Range requests are answered against that shorter resource."""
    layout = await asyncio.to_thread(_wav_layout, wav_path)
    if layout is None:
        return await _range_file_response(request, wav_path, "audio/wav",
                                          headers=headers, background=background)
    ba = max(1, layout["block_align"])
    frames = layout["data_size"] // ba
    skip = min(frames, max(0, int(round(seek * layout["rate"]))))
    off = layout["data_offset"] + skip * ba
    data_len = (frames - skip) * ba
    head = _wav_header_for(layout, data_len)
    total = len(head) + data_len
    hdrs = dict(headers or {})
    hdrs["Accept-Ranges"] = "bytes"
    hdrs["X-Time-Offset"] = f"{skip / max(1, layout['rate']):.3f}"
    rng = _parse_audio_range(request.headers.get("range"), total)
    if rng is None:
        hdrs["Content-Range"] = f"bytes */{total}"
        return Response(status_code=416, media_type="audio/wav", headers=hdrs,
                        background=background)
    start, end_excl, is_range = rng
    hdrs["Content-Length"] = str(end_excl - start)
    if is_range:
        hdrs["Content-Range"] = f"bytes {start}-{end_excl - 1}/{total}"
    hl = len(head)

    async def _gen():
        if start < hl:
            yield head[start:min(end_excl, hl)]
        fpos = off + max(start, hl) - hl
        fend = off + end_excl - hl
        if fpos >= fend:
            return
        fd = await asyncio.to_thread(os.open, str(wav_path), os.O_RDONLY)
        try:
            while fpos < fend:
                chunk = await asyncio.to_thread(
                    os.pread, fd, min(_RANGE_STREAMING_CHUNK, fend - fpos), fpos)
                if not chunk:
                    break
                yield chunk
                fpos += len(chunk)
        finally:
            os.close(fd)

    return StreamingResponse(_gen(), status_code=206 if is_range else 200,
                             media_type="audio/wav", headers=hdrs,
                             background=background)


async def _serve_rendered(request: Request, wav_path: Path, *, headers: dict,
                          background, xf: "_Xform | None", track_id: str,
                          source_key: str) -> Response:
    """Final step of every rendered-format branch: the cached WAV as is (the
    Range fast path), or — only when a client asked — transcoded to its codec
    / bitrate (cached per render + codec + bitrate) and/or from a time offset.

    ``source_key`` is the rendered WAV's cache key: pinned while ffmpeg reads
    it, and part of the transcode's key so subsongs / SID lengths never
    share a transcode."""
    if xf is None or not xf.active:
        return await _range_file_response(request, wav_path, "audio/wav",
                                          headers=headers, background=background)
    if not xf.codec:
        return await _wav_offset_response(request, wav_path, xf.seek, headers, background)
    from soniqboom.core.conversion_cache import (
        get_or_render, get_cached, pin, unpin, rendered_transcode_variant,
    )
    hdrs = dict(headers or {})
    hdrs.update({"X-Transcoded": "1", "X-Original-Codec": "wav",
                 "X-Target-Codec": xf.codec})
    if xf.seek > 0:
        pin(source_key)
        return _live_transcode_response(
            wav_path, xf, hdrs,
            _compose_backgrounds(BackgroundTask(unpin, source_key), background))
    variant = rendered_transcode_variant(source_key)
    tkey = _ck(track_id, "transcoded", 0, codec=xf.codec,
               bitrate=xf.bitrate or None, variant=variant)
    out = await get_cached(tkey)
    if out is not None:
        hdrs["X-Transcode-Cache"] = "hit"
        return await _range_file_response(request, out, TRANSCODE_MIME[xf.codec],
                                          headers=hdrs, background=background)
    # Not cached yet: answer NOW from a live encode of the WAV, and fill the
    # cache from a second encode in the background (a whole encode used to
    # sit in front of the first byte — seconds, more on a small NAS).
    wav = Path(wav_path)
    _fill_transcode_in_background(
        tkey, lambda: get_or_render(
            track_id=track_id, format_type="transcoded", subsong=0,
            codec=xf.codec, bitrate=xf.bitrate or None, variant=variant,
            render_fn=lambda: _render_to_transcoded_flac(
                wav, codec=xf.codec, bitrate_kbps=xf.bitrate or None)),
        source_key=source_key)
    hdrs["X-Transcode-Cache"] = "miss"
    pin(source_key)
    return _live_transcode_response(
        wav, xf, hdrs,
        _compose_backgrounds(BackgroundTask(unpin, source_key), background))


async def _source_kbps(track, path: Path, ext: str) -> int:
    """A direct-served file's bitrate in kbps: the scanned value, else size /
    duration; 0 when unknown."""
    try:
        br = int(getattr(track, "bitrate", 0) or 0)
    except (TypeError, ValueError):
        br = 0
    if br > 0:
        return br // 1000 if br > 10_000 else br
    try:
        dur = float(getattr(track, "duration", 0) or 0)
        if dur > 0:
            size, _ = await _cached_stat(path)
            return int(size * 8 / dur / 1000)
    except (OSError, TypeError, ValueError):
        pass
    return 0


# Lossy direct-served sources keep their own codec when a bitrate cap forces
# a transcode — what getTranscodeDecision promised the client.
_LOSSY_SRC_CODEC = {".mp3": "mp3", ".ogg": "ogg", ".opus": "ogg"}
_MP4_EXTS = (".m4a", ".aac", ".mp4", ".m4b", ".m4r", ".3gp")


def delivered_format(path_str: str, fmt_label: str = "",
                     requested: str | None = None) -> "tuple[str, str] | None":
    """``(suffix, mime)`` of what ``stream_track`` sends for a track and an
    optional requested codec (``format=``), or None when the file itself is
    served (direct play).  Name-only — no file IO — so it is cheap enough for
    every song in a Subsonic listing; memoised per extension / label family /
    request.  Mirrors ``stream_track``'s routing (bitrate caps aside):

      * rendered formats (SID, Amiga, trackers, chip formats, MIDI …) → WAV,
        or the requested mp3 / ogg / flac;
      * NATIVE files → as is, unless another supported codec was requested;
      * AAC in an MP4 container → as is (unless a codec was requested); ALAC
        (per the stored format label) → like the next case;
      * everything else (DSD, AIFF, ALAC, APE, WavPack, …) → WAV, or the
        requested codec (an unsupported request → the server default).
    """
    ext, uade_named = _render_ident(path_str or "")
    fam = ((fmt_label or "").split("/", 1)[0].strip().upper()
           if ext in _MP4_EXTS else "")
    req = (requested or "").strip().lower() or None
    return _delivered_format_cached(
        ext, bool(uade_named), fam, req,
        (settings.transcode_format or "flac").lower())


@functools.lru_cache(maxsize=2048)
def _delivered_format_cached(ext: str, uade_named: bool, fam: str,
                             req: "str | None", default_codec: str):
    if _is_rendered_ext(ext, uade_named):
        if req in TRANSCODE_MIME:
            return (req, TRANSCODE_MIME[req])
        return ("wav", "audio/wav")
    if ext in NATIVE:
        if req in TRANSCODE_MIME and req != ext.lstrip("."):
            return (req, TRANSCODE_MIME[req])
        return None
    if ext in _MP4_EXTS and fam != "ALAC":
        if req in TRANSCODE_MIME:
            return (req, TRANSCODE_MIME[req])
        return None
    if req is None or req == "wav":
        return ("wav", "audio/wav")
    codec = req if req in TRANSCODE_MIME else default_codec
    return (codec, TRANSCODE_MIME.get(codec, "audio/flac"))


def _sid_target_seconds(track, subsong: int, start: "int | None" = None) -> int:
    """Per-tune SID render length (the HVSC length of the tune wire
    ``subsong`` plays — ``hvsc_lengths`` is in tune order, see
    ``sid_wire_tune`` — else the stored duration, else the default), clamped
    5..3600 — the value every SID path keys its cache on.  The start song is
    ``start`` when given, else ``sid_start_song_known`` (an async caller
    holding the file reads it first with ``sid_start_song``)."""
    target_dur = settings.sid_default_duration
    meta = (track if isinstance(track, dict)
            else track.__dict__ if hasattr(track, "__dict__") else {})
    hvsc_lengths = meta.get("hvsc_lengths") or []
    tid = meta.get("id") or getattr(track, "id", "") or ""
    idx = sid_wire_tune(subsong, start if start is not None else sid_start_song_known(tid, track),
                        meta.get("subsongs") or len(hvsc_lengths)) - 1
    if hvsc_lengths and 0 <= idx < len(hvsc_lengths):
        target_dur = int(round(float(hvsc_lengths[idx])))
    elif meta.get("duration") and float(meta["duration"]) > 0:
        target_dur = int(round(float(meta["duration"])))
    return max(5, min(int(target_dur), 3600))


async def _offline_root_for(track) -> str | None:
    """The scan root ``track`` lives under when that root is OFFLINE — its
    cached status says so, or a bounded reachability probe (≤ 4 s, on its own
    pool) fails; else None.  Only called once a file turned out missing, so a
    successful play never pays for it."""
    try:
        from soniqboom.core import data as _data
        from soniqboom.core.store import get_store
        dirs = get_store().list_scan_dirs()
    except Exception:
        return None
    outer = (getattr(track, "path", "") or "").split("::", 1)[0]
    rh = getattr(track, "scan_root_hash", "") or ""
    sd = None
    if rh:
        sd = next((d for d in dirs if d.get("path_hash") == rh), None)
    if sd is None:
        cands = [d for d in dirs if d.get("path")
                 and (outer == d["path"] or outer.startswith(d["path"].rstrip("/") + "/"))]
        sd = max(cands, key=lambda d: len(d["path"]), default=None)
    if sd is None:
        return None
    if sd.get("status") == "unavailable":
        return sd["path"]
    try:
        _p, ok = await _data._probe_scan_dir(sd)
    except Exception:
        return None
    return None if ok else sd["path"]


def _ogg_needs_transcode(ext: str, request: Request) -> bool:
    """Ogg/Opus: some clients (Safari < 18.4) can't decode it — route those to
    the transcoder (→ WAV, which every browser plays) instead of a native
    .ogg/.opus that fails silently.  Prefer the client's DECLARED capability
    (sb_caps), keyed by extension — ``.opus`` is Opus; ``.ogg`` is usually
    Vorbis but may be Opus, so accept EITHER (uses the vorbis cap too).
    Fall back to the Safari-version UA heuristic when nothing was declared."""
    if ext == ".opus":
        _ogg_sup = _client_supports("opus", request)
    elif ext == ".ogg":
        _o = _client_supports("opus", request)
        _v = _client_supports("vorbis", request)
        _ogg_sup = None if (_o is None and _v is None) else (bool(_o) or bool(_v))
    else:
        return False
    return (_ogg_sup is False) if _ogg_sup is not None else _safari_lacks_ogg(request)


# ── Remote files: play while they download ─────────────────────────────────
# A remote track served as-is (mp3 / flac / wav / ogg / opus) used to wait
# for the whole file to land in the remote cache before its first byte.  Now
# the download streams into the cache and the response reads the growing
# file (``RemoteCache.open_progressive``): exact Content-Length, 206 answers
# against the final size, and a seek that lands well past what has arrived is
# answered straight from the share until the download catches up.
_FAR_SEEK_MIN_BYTES = 2 * 1024 * 1024   # never go direct for a smaller gap…
_FAR_SEEK_WAIT_S = 1.0                  # …or one the download closes this fast
_FAR_SEEK_OPEN_S = 3.0                  # a share that can't open a read this fast: wait
# Direct share reads run on their own threads: an open stuck waiting for a
# pooled FTP connection must not tie up the default executor.
_DIRECT_READ_POOL: "concurrent.futures.ThreadPoolExecutor | None" = None


def _direct_read_pool():
    global _DIRECT_READ_POOL
    if _DIRECT_READ_POOL is None:
        import concurrent.futures
        _DIRECT_READ_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="remote-direct")
    return _DIRECT_READ_POOL


def _share_marked_down(scan_root: str) -> bool:
    """The health monitor has flagged this share unavailable (it keeps the
    source registered while it retries)."""
    try:
        from soniqboom.core.store import get_store
        return any(sd.get("path") == scan_root and sd.get("status") == "unavailable"
                   for sd in get_store().list_scan_dirs())
    except Exception:
        return False


def _range_bounds(request: Request, size: int) -> tuple[int, int, bool]:
    """``(start, end, ranged)`` of a single-range request against ``size``
    bytes — the parse ``_growing_file_range_response`` applies
    (``_parse_audio_range``); an unsatisfiable range reads as the whole file
    here (that response answers it with a 416)."""
    rng = _parse_audio_range(request.headers.get("range"), size)
    if rng is None:
        return 0, size - 1, False
    return rng[0], rng[1] - 1, rng[2]


async def _maybe_remote_progressive(request: Request, track, *, target_format: str | None,
                                    max_bitrate_kbps: int,
                                    force_transcode: bool) -> Response | None:
    """Serve a remote track that plays as-is while its download runs — or
    None for everything else (cached, archive members, rendered / transcoded
    formats, an unconnected share, a failed start), which the regular
    ``_resolve_play_source`` path then handles exactly as before."""
    path_str = track.path or ""
    if (request.method != "GET" or force_transcode or "::" in path_str
            or not is_remote_path(path_str)):
        return None
    ext, uade_named = _render_ident(path_str, track)
    if ext not in NATIVE or uade_named:
        return None
    want = (target_format or "").strip().lower()
    if want and want != "raw" and want in TRANSCODE_MIME and want != ext.lstrip("."):
        return None                     # another codec asked for → transcode
    if max_bitrate_kbps > 0 and want != "raw":
        return None                     # a bitrate cap may need a transcode
    if _ogg_needs_transcode(ext, request):
        return None
    from soniqboom.core.filesource import get_source, parse_remote_path
    from soniqboom.core.remote_cache import ProgressiveRead, get_cache
    try:
        scan_root, remote_path = parse_remote_path(path_str)
    except ValueError:
        return None
    source = get_source(scan_root) if remote_path else None
    if source is None:
        return None
    cache = get_cache()
    if cache.peek_cached(scan_root, remote_path) or _share_marked_down(scan_root):
        return None                     # cached / known down: the regular path
    try:
        got = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(cache.open_progressive, scan_root, remote_path,
                                    source, lane="stream"))
    except Exception as exc:
        # The regular path retries and maps it (404 / reconnect / 502).
        log.info("Progressive start for %s failed (%s: %s)", path_str,
                 type(exc).__name__, exc)
        return None
    if not isinstance(got, ProgressiveRead):
        return await _range_file_response(request, got, media_type=NATIVE[ext])
    if got.size <= 0 or (got.done and not got.ok):
        os.close(got.fd)                # empty, or failed just now → regular path
        return None
    return await _remote_progressive_response(
        request, got, NATIVE[ext], source, remote_path)


async def _remote_progressive_response(request: Request, handle, media_type: str,
                                       source, remote_path: str) -> Response:
    loop = asyncio.get_running_loop()
    size = handle.size
    fd = _FdHandle(handle.fd)
    complete = asyncio.Event()
    data_ev = asyncio.Event()
    flags = {"no_pad_on_failure": True, "never_pad": True, "clean_exit": None,
             "subscribers": 1}

    def _wake() -> None:
        if handle.done:
            flags["clean_exit"] = handle.ok
            complete.set()
        data_ev.set()
        data_ev.clear()

    def _on_progress() -> None:                 # runs on the download thread
        try:
            loop.call_soon_threadsafe(_wake)
        except RuntimeError:                    # loop closed during shutdown
            pass

    handle.add_listener(_on_progress)
    release = BackgroundTask(handle.remove_listener, _on_progress)
    headers = {"X-Remote-Stream": "progressive"}
    start, end, ranged = _range_bounds(request, size)
    if ranged and start > 0 and not handle.done:
        gap = start - handle.written
        if gap > max(_FAR_SEEK_MIN_BYTES, handle.rate * _FAR_SEEK_WAIT_S):
            resp = await _remote_direct_range(
                handle, size, start, end, media_type, source, remote_path, fd, release)
            if resp is not None:
                return resp
    return await _growing_file_range_response(
        request, handle.path, size, complete, media_type, headers=headers,
        data_event=data_ev, inflight=flags, background_task=release, fd=fd,
    )


async def _remote_direct_range(handle, size: int, start: int, end: int, media_type: str,
                               source, remote_path: str, fd: "_FdHandle",
                               release) -> Response | None:
    """Answer a seek far past the downloaded prefix straight from the share,
    switching to the local copy — for good — once the download has caught
    up (when the reader then outruns the download it waits for it, it never
    re-opens the share; only a download that FAILED or stopped moving for
    ``_GROWING_READ_TIMEOUT`` sends it back to the share, to the end).
    None when the share can't open a read promptly (e.g. an FTP pool with no
    free stream connection) — the caller then waits on the download instead.

    Every share stream this opens is closed exactly once, also when the
    request is cancelled while an open is still running in its thread."""
    from soniqboom.core.filesource import borrow_wait

    loop = asyncio.get_running_loop()
    pool = _direct_read_pool()

    def _open(off: int, cap: "float | None"):
        if cap is None:
            return source.open_stream(remote_path, offset=off, lane="stream",
                                      length=end + 1 - off)
        # A pooled-FTP open gives up with the caller instead of staying
        # queued as a priority stream-lane waiter (holding back the server's
        # scan / browse borrows) for the pool's full 60 s.
        with borrow_wait(cap):
            return source.open_stream(remote_path, offset=off, lane="stream",
                                      length=end + 1 - off)

    def _close_late(f) -> None:
        """Close the stream an abandoned open still produced."""
        try:
            if f.cancelled() or f.exception() is not None:
                return
            st_late = f.result()
        except BaseException:
            return
        try:
            pool.submit(st_late.close)
        except RuntimeError:                    # pool shut down
            st_late.close()

    fut = loop.run_in_executor(pool, _open, start, _FAR_SEEK_OPEN_S)
    try:
        st = await asyncio.wait_for(asyncio.shield(fut), timeout=_FAR_SEEK_OPEN_S)
    except asyncio.CancelledError:
        fut.add_done_callback(_close_late)
        raise
    except Exception as exc:
        log.info("Direct read of %s at %d unavailable (%s) — waiting on the download",
                 remote_path, start, type(exc).__name__)
        fut.add_done_callback(_close_late)
        return None
    if st.size is not None and int(st.size) != size:
        try:
            pool.submit(st.close)
        except RuntimeError:                    # pool shut down
            st.close()
        return None
    # The open share stream, closed by the body — or by the response's
    # cleanup when the body never runs (client gone before it started).  A
    # close waits for a read still running in a worker thread (cancelled
    # request): closing a stream mid-read races the read on its socket.
    held: dict = {"st": st, "rd": None}
    held_lock = threading.Lock()      # the body (loop) and the cleanup (a thread)

    def _close_held() -> None:
        with held_lock:
            s_, held["st"] = held["st"], None
            rd = held["rd"]
        if s_ is None:
            return

        def _go(_f=None) -> None:
            try:
                pool.submit(s_.close)
            except RuntimeError:
                s_.close()
        if rd is not None and not rd.done():
            rd.add_done_callback(_go)
        else:
            _go()

    progressed = asyncio.Event()

    def _on_download() -> None:                 # runs on the download thread
        try:
            loop.call_soon_threadsafe(progressed.set)
        except RuntimeError:                    # loop closed during shutdown
            pass

    add_listener = getattr(handle, "add_listener", None)
    if add_listener is not None:
        add_listener(_on_download)

    async def _share_read(pos: int, n: int) -> bytes:
        if held["st"] is None:
            f = loop.run_in_executor(pool, _open, pos, None)
            try:
                held["st"] = await asyncio.shield(f)
            except asyncio.CancelledError:
                f.add_done_callback(_close_late)
                raise
        # A concurrent future, not ``run_in_executor``'s wrapper: a cancelled
        # request cancels the wrapper at once, while the read keeps running.
        rd = pool.submit(held["st"].read, n)
        held["rd"] = rd
        return await asyncio.wrap_future(rd)

    async def _body():
        pos = start
        local = False            # reading the downloaded copy (and staying there)
        back_to_share = False    # the download failed / stalled: share reads to the end
        seen, seen_at = -1, time.monotonic()
        try:
            while pos <= end:
                if not local and not back_to_share and pos < handle.written:
                    local = True
                    _close_held()
                if local:
                    written = handle.written
                    if pos >= written:
                        if written != seen:
                            seen, seen_at = written, time.monotonic()
                        if ((handle.done and not handle.ok)
                                or time.monotonic() - seen_at > _GROWING_READ_TIMEOUT):
                            # The download failed or stopped moving: finish
                            # from the share (once — no way back).
                            local, back_to_share = False, True
                            continue
                        progressed.clear()
                        if pos >= handle.written and not handle.done:
                            try:
                                await asyncio.wait_for(
                                    progressed.wait(),
                                    timeout=1.0 if add_listener is not None else 0.05)
                            except asyncio.TimeoutError:
                                pass
                        continue
                    n = min(_RANGE_STREAMING_CHUNK, handle.written - pos, end + 1 - pos)
                    chunk = await asyncio.to_thread(os.pread, fd.fd, n, pos)
                else:
                    chunk = await _share_read(pos, min(_RANGE_STREAMING_CHUNK, end + 1 - pos))
                if not chunk:
                    log.warning("Direct read of %s ended at %d of %d", remote_path,
                                pos, end + 1)
                    return                      # short body → the client retries
                yield chunk
                pos += len(chunk)
        finally:
            remove = getattr(handle, "remove_listener", None)
            if remove is not None:
                remove(_on_download)
            _close_held()
            fd.close()

    def _cleanup() -> None:
        remove = getattr(handle, "remove_listener", None)
        if remove is not None:
            remove(_on_download)
        _close_held()

    headers = _accel_off({
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
        "X-Remote-Stream": "direct",
    })
    return _CleanupStreamingResponse(
        _body(), status_code=206, media_type=media_type, headers=headers,
        background=_compose_backgrounds(release, BackgroundTask(fd.close),
                                        BackgroundTask(_cleanup)),
    )


def _offline_error(root: str) -> HTTPException:
    from soniqboom.core.filesource import credentials_refused
    if credentials_refused(root):
        # ``get_source`` hides a share whose sign-in is backing off: no play
        # logs in with credentials the server refused (lockout risk).
        return HTTPException(
            503, f"Sign-in refused: the server of {root} refused its credentials — "
                 "update them or press Reconnect to play this track")
    return HTTPException(
        503, f"Source offline: {root} isn't connected — reconnect it to play this track")


@router.get("/{track_id}")
async def stream_track(
    track_id: str,
    request: Request,
    seek: float = Query(default=0.0, ge=0.0, description="Start position in seconds"),
    subsong: int | None = Query(default=None, ge=0,
                                description="Tune (wire index) of a multi-tune file; "
                                            "absent = the file's default tune"),
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
    # would have been an anonymous-stream bypass).  The Subsonic byte routes
    # set the same flag after authenticating the caller themselves.
    if not _cast_internal_bypass_ctx.get():
        await _require_stream_auth(request, sb_session, u, p, s, t)
    track = await get_track(track_id)
    if not track:
        # On-demand ingestion: if a file path was provided, extract metadata
        # and upsert to store so playback can proceed immediately.
        if file_path:
            track = await _ingest_on_demand(track_id, file_path)
        if not track:
            raise HTTPException(404, "Track not found")

    # A remote track that plays as-is starts while its download runs.
    _prog = await _maybe_remote_progressive(
        request, track, target_format=target_format,
        max_bitrate_kbps=max_bitrate_kbps, force_transcode=force_transcode)
    if _prog is not None:
        return _prog

    _zip_tmp: Path | None = None  # temp file to clean up after streaming
    path, ext, _uade_named, _zip_track_id_for_unpin = await _resolve_play_source(track_id, track)

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
        #
        # Which renders play while they run (progressive SID; ``_serve_live``
        # for the rest) and which are awaited whole was decided by measured
        # render speed (one 2026-10 Apple-silicon host, a ~3-minute tune each,
        # whole render): SID 9.7 s, libgme Mega Drive VGM 9.5 s, Dreamcast PSF
        # 9.1 s, GBA PSF 1.9 s, AdLib (Nuked OPL3) 1.5-2.0 s, MIDI 1.3-1.6 s,
        # SNDH 1.0-1.3 s, SPC 0.6 s — live.  Awaited, their whole render being
        # about as quick as a live start: openmpt trackers 0.14 s, HVL 0.31 s,
        # sc68 0.39 s (6.5 minutes of disk), YM 0.5 s (6.5 minutes).
        from soniqboom.core.conversion_cache import get_or_render

        # What a (Subsonic) client asked to change about a rendered track's
        # WAV — another codec, a bitrate cap, a time offset.  Inactive for
        # every web-UI play; see ``_serve_rendered``.
        xf = _rendered_xform(target_format, max_bitrate_kbps, seek)
        # Our own web UI (session cookie, no Subsonic auth in play, not the
        # cast byte server / an in-process Subsonic call) may be served
        # growing, still-rendering files.
        _web_session = (request.method == "GET"
                        and bool(sb_session)
                        and not (u or s or t)
                        and not _cast_internal_bypass_ctx.get())

        _c64 = ext in _SID_EXTS and _is_c64_sid(path)
        # ``.psf`` is also Amiga SoundFactory's suffix: without the PSF magic
        # the file is a module, and uade plays it (like a magic-less .sid).
        if (ext == ".psf" and not _uade_named
                and not await asyncio.to_thread(_psf_has_magic, path)):
            _uade_named = True
        # The tune: the one the request names (SID / SNDH map that wire with
        # the file's start song themselves; every other renderer below takes
        # it as the tune index), or for a bare play the file's default tune —
        # for a multi-tune Amiga / libgme / sc68 file its first tune that
        # isn't empty, probed once.
        _wire = explicit_wire(subsong, request)
        if _wire is None:
            track = await _bare_play_track(
                track_id, track, path, _probe_family(ext, _uade_named, path, c64=_c64))
        subsong = tune_index(track_id, track, _wire)

        if _c64:
            from soniqboom.core.conversion_cache import (
                _cache_key, find_shorter_sid_entry,
                start_background_render, get_cached, known_silent,
            )
            # Prefer per-track HVSC duration over the global default.  The
            # track record may carry ``hvsc_lengths`` (a list of per-subsong
            # durations) and/or a ``duration`` value already patched by the
            # HVSC rescan endpoint.  Fall back to the safety-cap default.
            # Clamped 5..3600 so we never feed sidplayfp -t0 (HVSC/meta
            # values are semi-trusted — QA residual #1).  The file is local
            # here: its header says which tune each wire plays (once per
            # track, remembered for the O(1) paths).
            await sid_start_song(track_id, track, path)
            target_dur = _sid_target_seconds(track, subsong)

            full_key = _cache_key(track_id, "sid", subsong, duration=target_dur)

            # 1) Exact cache hit (correct duration)
            exact = await get_cached(full_key)
            if exact:
                # Ensure the retro per-voice VU meter exists (background, dedup'd)
                # — covers SIDs cached before this feature and cache-hit replays,
                # which never reach the render-path triggers below.
                _spawn_sid_vu(full_key, exact, path, subsong, target_dur)
                return await _serve_rendered(
                    request, exact,
                    headers={"X-Rendered": "sidplayfp", "X-Cache": "hit",
                             "X-SID-Target-Seconds": str(target_dur)},
                    background=_bg, xf=xf, track_id=track_id, source_key=full_key,
                )

            # 2) Shorter version available — serve it now, render full in background
            # (not when the client asked for another codec / offset: that
            # needs the full render).  Our web UI only: the player moves onto
            # the full render once it is ready, where another client would
            # play the shorter tune as the whole one — it gets the full-length
            # progressive render below instead.
            shorter = (None if xf.active or not _web_session else
                       await find_shorter_sid_entry(track_id, subsong, target_dur))
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

            # 3) No cache at all.  Stream sidplayfp's still-rendering WAV so
            # playback starts in ~0.2 s instead of blocking on the full render
            # (~tune_len/15).  ``_serve_sid_progressive`` answers with the
            # exact final length (a Content-Length, and a proper 206 against it
            # for Range/seek requests), so ANY GET client can take it — the web
            # UI, Subsonic, DLNA and the cast byte-server.  The gate:
            #   • GET only — a HEAD has its own route; never spawns a render.
            #   • no audio change asked (``xf``: another codec, a bitrate cap,
            #     a time offset — those need the finished WAV).
            #   • not a tune that just rendered silent (its 422 comes at once).
            # ``_serve_sid_progressive`` returns None (→ fall through to blocking)
            # when over the concurrency cap or on an immediate render failure (so
            # the error surfaces as a real 5xx, not silent silence), and raises
            # the cache's 422 for a tune that plays only silence.
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

            if request.method == "GET" and not xf.active and not known_silent(full_key):
                _resp = await _serve_sid_progressive(
                    request, path, subsong, target_dur, full_key,
                    base_headers={"X-Rendered": "sidplayfp", "X-Cache": "miss-progressive",
                                  "X-SID-Target-Seconds": str(target_dur)},
                    background=_bg, web=_web_session,
                )
                if _resp is not None:
                    return _resp
                # else: over cap or immediate render failure — fall through to
                # the blocking path.
            elif (request.method == "GET" and xf.codec and not xf.seek
                    and not known_silent(full_key)):
                # Another codec (Subsonic ``format=`` / a bitrate cap, no time
                # offset): a live encode of the progressive render as it grows
                # — its length is exact, so an estimated Content-Length works
                # too.  Over the pool cap / an immediate failure: blocking.
                _done = await _cached_rendered_transcode(track_id, xf, full_key)
                _feed = (None if _done is not None else
                         await sid_wav_feed(path, subsong, target_dur, full_key))
                if _done is not None:
                    return await _serve_rendered_transcode_hit(
                        request, _done, xf, {"X-Rendered": "sidplayfp",
                                             "X-SID-Target-Seconds": str(target_dur)}, _bg)
                if _feed is not None:
                    _settled = _SID_PROG_DONE.get(full_key)

                    async def _sid_rendered():
                        if _settled is not None:
                            await _settled.wait()
                        _p = await get_cached(full_key)
                        if _p is None:
                            raise RuntimeError("the progressive render was not cached")
                        return _p, False
                    return await _transcode_growing_render(
                        request, track_id=track_id, feed=_feed, xf=xf, source_key=full_key,
                        rendered=_sid_rendered,
                        headers={"X-Rendered": "sidplayfp", "X-Cache": "miss-progressive",
                                 "X-SID-Target-Seconds": str(target_dur)},
                        background=_bg, src_seconds=float(target_dur))

            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="sid", subsong=subsong,
                duration=target_dur,
                render_fn=lambda: _render_sid(path, subsong=subsong, duration=target_dur),
            )
            return await _serve_rendered(
                request, cached_path,
                headers={"X-Rendered": "sidplayfp", "X-Cache": "hit" if hit else "miss",
                         "X-SID-Target-Seconds": str(target_dur)},
                background=_bg, xf=xf, track_id=track_id, source_key=full_key,
            )
        if ext in _MIDI_EXTS:
            from soniqboom.config import get_active_soundfont
            sf = get_active_soundfont()
            _sfp = str(sf) if sf else ""
            _key = _ck(track_id, "midi", 0, _sfp)
            # Played while fluidsynth renders it (length unknown until the end).
            return await _serve_live(
                request, track_id=track_id, format_type="midi", subsong=0, key=_key,
                render_fn=lambda: _render_midi(path, live_key=_key),
                cache_kw={"soundfont_path": _sfp}, web_session=_web_session,
                background=_bg, xf=xf, rendered="fluidsynth")
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
            await _backfill_rendered_duration(track_id, track, cached_path, 0.0,
                                              subsong=subsong)
            return await _serve_rendered(
                request, cached_path,
                headers={"X-Rendered": "hvl2wav", "X-Cache": "hit" if hit else "miss"},
                background=_bg, xf=xf, track_id=track_id,
                source_key=_ck(track_id, "hvl", subsong=subsong),
            )
        if ((ext in _PSF_STREAM_EXTS and not _uade_named)
                or (ext == ".dsf" and _dsf_is_dreamcast(path))):
            _key = _ck(track_id, "psf", subsong=0)
            _exp = await asyncio.to_thread(_psf_render_seconds, path)

            async def _after(cached_path):
                # Duration normally comes from the length/fade tags at scan;
                # rips without tags stored 0 — persist the rendered WAV's
                # real length.
                await _backfill_rendered_duration(track_id, track, cached_path, 0.0,
                                                  subsong=subsong)
            return await _serve_live(
                request, track_id=track_id, format_type="psf", subsong=0, key=_key,
                render_fn=lambda: _render_psf(path, live_key=_key, expected_seconds=_exp),
                web_session=_web_session, background=_bg, xf=xf, rendered="zxtune",
                after_render=_after)
        if ext in _SNDH_EXTS:
            _key = _ck(track_id, "sndh", subsong=subsong)

            async def _after(cached_path):
                # SNDH TIME tags are frequently absent; the scan stored the
                # Atari default cap in that case — backfill only refines a 0
                # duration.
                await _backfill_rendered_duration(track_id, track, cached_path, 0.0,
                                                  subsong=subsong)
            # psgplay renders exactly the length it is given: an exact live
            # render every client may stream.
            return await _serve_live(
                request, track_id=track_id, format_type="sndh", subsong=subsong,
                key=_key,
                render_fn=lambda: _render_sndh(path, subsong=subsong, track_id=track_id,
                                               live_key=_key),
                web_session=_web_session, background=_bg, xf=xf, rendered="psgplay",
                after_render=_after)
        if ext in _YM_EXTS:
            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="ym", subsong=0,
                render_fn=lambda: _render_ym(path),
            )
            return await _serve_rendered(
                request, cached_path,
                headers={"X-Rendered": "stsound", "X-Cache": "hit" if hit else "miss"},
                background=_bg, xf=xf, track_id=track_id,
                source_key=_ck(track_id, "ym", subsong=0),
            )
        if ext in _SC68_EXTS:
            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="sc68", subsong=subsong,
                render_fn=lambda: _render_sc68(path, subsong=subsong),
            )
            # sc68 durations are embedded and honoured by the renderer; the
            # scan stored 0 — persist the WAV's real length.
            await _backfill_rendered_duration(track_id, track, cached_path, 0.0,
                                              subsong=subsong)
            return await _serve_rendered(
                request, cached_path,
                headers={"X-Rendered": "sc68", "X-Cache": "hit" if hit else "miss"},
                background=_bg, xf=xf, track_id=track_id,
                source_key=_ck(track_id, "sc68", subsong=subsong),
            )
        if ext in _UADE_EXTS or _uade_named or ext in _SID_EXTS:
            # Everything uade renders: .ahx, ~350 suffix tokens (song.fc13),
            # Amiga prefix-form names (mdat.song), and magic-less .sid files
            # (Amiga SidMon — real C64 PSID/RSID returned above already).
            return await _serve_uade(
                request, track_id, track, path, subsong,
                web_session=_web_session, background=_bg,
                zip_pin_id=_zip_track_id_for_unpin, xf=xf,
            )
        # .imf is overloaded (Imago Orpheus tracker vs id/Apogee AdLib IMF) —
        # _render_imf disambiguates by content.  MUST come before _TRACKER_EXTS,
        # which still lists .imf for scanner-side detection.
        if ext == ".imf" or ext in _ADLIB_EXTS:
            # id/Apogee AdLib IMF stores the same 180s placeholder as the rest
            # of AdLib and renders to natural end via _render_adlib, so its
            # real length is backfilled too.  The placeholder gate no-ops for
            # IM10 (Imago Orpheus) .imf, which already carries a real tracker
            # duration (and renders through openmpt123 — never live).
            _fmt = "imf" if ext == ".imf" else "adlib"
            _key = _ck(track_id, _fmt, subsong=subsong)
            _exp = _stored_render_seconds(track, subsong, track_id,
                                          placeholder=_ADLIB_PLACEHOLDER_S)
            _rfn = _render_imf if ext == ".imf" else _render_adlib

            async def _after(cached_path):
                # The scanner stored a 180s placeholder; the WAV carries the
                # real length — persist it so the list stops showing "3:00".
                # The render is the authority: a stored length it disagrees
                # with (which a live start promised) is corrected too.
                await _backfill_rendered_duration(track_id, track, cached_path,
                                                  subsong=subsong, authoritative=True)
            # Played while adplay renders it, once it proves audible — an
            # empty first subsong falls back to the probe of the next ones.
            return await _serve_live(
                request, track_id=track_id, format_type=_fmt, subsong=subsong, key=_key,
                render_fn=lambda: _rfn(path, subsong=subsong, live_key=_key,
                                       expected_seconds=_exp),
                web_session=_web_session, background=_bg, xf=xf,
                rendered="adplug/openmpt123" if ext == ".imf" else "adplug",
                after_render=_after)
        if ext in _TRACKER_EXTS:
            cached_path, hit = await get_or_render(
                track_id=track_id, format_type="tracker", subsong=subsong,
                render_fn=lambda: _render_tracker(path, subsong=subsong),
            )
            # Tracker length normally comes from openmpt123 --info at scan, but
            # that falls back to 0 when the binary is unavailable — backfill from
            # the rendered WAV in that case (placeholder=0 no-ops when scan already
            # stored a real duration).
            await _backfill_rendered_duration(track_id, track, cached_path, 0.0,
                                              subsong=subsong)
            return await _serve_rendered(
                request, cached_path,
                headers={"X-Rendered": "openmpt123", "X-Cache": "hit" if hit else "miss"},
                background=_bg, xf=xf, track_id=track_id,
                source_key=_ck(track_id, "tracker", subsong=subsong),
            )
        if ext in _GME_EXTS_STREAM:
            _key = _ck(track_id, "gme", subsong=subsong)
            # A GYM's stored length is its frame count, not the render's.
            _exp = (0.0 if ext == ".gym" else _stored_render_seconds(
                track, subsong, track_id, placeholder=float(settings.sid_default_duration)))

            async def _after(cached_path):
                # GME chiptunes (NSF/SPC/GBS/…) store the sid_default_duration
                # placeholder; libgme renders to the track's natural end so the
                # WAV carries the real length — persist it so the list/modal
                # stop showing the default (e.g. "5:00").  The render is the
                # authority: a GYM's scan length is its frame count (libgme
                # trims trailing silence), and a stored length from before a
                # change of the chiptune default length — which a live start
                # promised — is corrected too.
                await _backfill_rendered_duration(
                    track_id, track, cached_path, float(settings.sid_default_duration),
                    subsong=subsong, authoritative=True)
            return await _serve_live(
                request, track_id=track_id, format_type="gme", subsong=subsong, key=_key,
                render_fn=lambda: _render_gme(path, subsong=subsong, live_key=_key,
                                              expected_seconds=_exp),
                web_session=_web_session, background=_bg, xf=xf, rendered="gme",
                after_render=_after)

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
        _old_safari_ogg = _ogg_needs_transcode(ext, request)
        # A client bitrate cap (Subsonic ``maxBitRate``) below the source's
        # bitrate also forces a transcode — only Subsonic requests carry one,
        # so a web play never pays for the bitrate lookup.
        _cap = 0
        if (max_bitrate_kbps > 0
                and (target_format or "").strip().lower() != "raw"):
            _src_kbps = await _source_kbps(track, path, ext)
            if _src_kbps > max_bitrate_kbps or (
                    _src_kbps == 0 and ext not in _LOSSY_SRC_CODEC):
                _cap = max_bitrate_kbps
        if (ext in NATIVE and not force_transcode and not _format_mismatch
                and not _old_safari_ogg and not _cap):
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
            if (detected_codec == "aac" and not _aac_mismatch and not _cap
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
            if detected_codec == "alac" and _alac_ok and not _aac_mismatch and not _cap:
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
        if _cap:
            # Under a bitrate cap: a lossy source keeps its codec (what
            # getTranscodeDecision promised), anything else goes to the
            # requested lossy codec or mp3 — FLAC has no bitrate knob.
            _want = (target_format or "").strip().lower()
            eff_codec = (_want if _want in ("mp3", "ogg")
                         else _LOSSY_SRC_CODEC.get(ext, "mp3"))
        eff_mime = TRANSCODE_MIME.get(eff_codec, "audio/flac")
        # The bitrate the encoder gets — and the cache key carries, so an
        # mp3@128 and an mp3@320 of the same track never share an entry.
        eff_br = (max_bitrate_kbps if (max_bitrate_kbps and eff_codec != "flac")
                  else 0)

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
        use_inflight = ((target_format is None or target_format.lower() == "wav")
                        and not _cap)

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
                target_channels_hint, original_codec, _bg, seek=seek,
            )

        _tx_headers = {"X-Transcoded": "1", "X-Original-Codec": original_codec,
                       "X-Target-Codec": eff_codec}
        if seek > 0:
            # A time-offset start (Subsonic ``timeOffset``) never waits for a
            # whole transcode: pipe ffmpeg from the offset — remuxing the
            # cached transcode when there is one, else encoding the source.
            from soniqboom.core.conversion_cache import get_cached as _gc
            _done = await _gc(_ck(track_id, "transcoded", 0, codec=eff_codec,
                                  target_rate=eff_rate, bitrate=eff_br or None))
            return _live_transcode_response(
                _done or path, _Xform(eff_codec, eff_br, seek), _tx_headers, _bg,
                copy=_done is not None, target_rate=None if _done else eff_rate)

        # Another codec / a bitrate cap (Subsonic ``format=`` / ``maxBitRate``):
        # the cached transcode with Range when there is one; otherwise answer
        # at once from a live encode and fill the cache in the background, so
        # a cold play never waits for the whole encode before its first byte.
        from soniqboom.core.conversion_cache import get_cached as _gc
        _tkey = _ck(track_id, "transcoded", 0, codec=eff_codec,
                    target_rate=eff_rate, bitrate=eff_br or None)
        _done = await _gc(_tkey)
        if _done is not None:
            return await _range_file_response(
                request, _done, media_type=eff_mime,
                headers={**_tx_headers, "X-Cache": "hit"},
                background=_bg,
            )
        _src_dur = float(getattr(track, "duration", 0) or 0) or None
        _fill_transcode_in_background(
            _tkey, lambda: get_or_render(
                track_id=track_id, format_type="transcoded", subsong=0,
                codec=eff_codec, target_rate=eff_rate, bitrate=eff_br or None,
                render_fn=lambda: _render_to_transcoded_flac(
                    path, target_rate=eff_rate, codec=eff_codec,
                    bitrate_kbps=eff_br or None,
                    progress_key=track_id,
                    source_duration=_src_dur,
                ),
            ),
            zip_pin_id=_zip_track_id_for_unpin)
        return _live_transcode_response(
            path, _Xform(eff_codec, eff_br, 0.0), {**_tx_headers, "X-Cache": "miss"},
            _bg, target_rate=eff_rate)
    except BaseException:
        _cleanup_tmp()
        raise


def _head_c64_start_song(track_id: str, track, path_str: str) -> "int | None":
    """For HEAD: the start song of the plain local C64 SID ``path_str`` as GET
    will use it (recorded, remembered, else read from its header — not
    remembered here), or None when it isn't a readable C64 tune.  Blocking."""
    try:
        if not os.path.isfile(path_str):
            return None
        with open(path_str, "rb") as fh:
            if fh.read(4) not in (b"PSID", b"RSID"):
                return None
    except OSError:
        return None
    rec = _recorded_start_song(track) if track is not None else None
    if rec is not None:
        return rec
    got = _SID_START_SONG.get(track_id)
    if got is not None:
        return got
    try:
        count, start = _psid_count_start(Path(path_str))
    except Exception:
        return 1
    return start if count > 0 else 1


def _peek_cached_size(cache_key: str) -> int | None:
    """Size of a finished conversion-cache entry, without touching its LRU
    position or the disk; None when not cached."""
    from soniqboom.core import conversion_cache as _cc
    with _cc._state_lock:
        ent = _cc._meta.get(cache_key)
    if not ent:
        return None
    try:
        return int(ent.get("size_bytes") or 0) or None
    except (TypeError, ValueError):
        return None


@router.head("/{track_id}")
async def stream_track_head(
    track_id: str,
    request: Request,
    subsong: int | None = Query(default=None, ge=0),
    target_format: str | None = Query(default=None, alias="format", max_length=16),
    max_bitrate_kbps: int = Query(default=0, alias="maxBitRate", ge=0, le=2_500_000),
    target_sample_rate: int = Query(default=0, alias="sampleRate", ge=0, le=384_000),
    force_transcode: bool = Query(default=False),
    sb_session: str | None = Cookie(default=None),
    u: str | None = Query(default=None),
    p: str | None = Query(default=None),
    s: str | None = Query(default=None),
    t: str | None = Query(default=None),
):
    """Headers of what GET would send, with no body and no work: some players
    and cast renderers probe a stream URL with HEAD before playing it.

    Same auth as GET.  Never renders, transcodes, extracts an archive member
    or fetches a remote file: Content-Type is predicted from the file name
    (``delivered_format``), and Content-Length is sent only when it is known
    for free — a local file served as is, a finished render / transcode
    already in the conversion cache, or a local C64 SID's render (GET
    streams it with its exact length: the tune's target seconds)."""
    if not _cast_internal_bypass_ctx.get():
        await _require_stream_auth(request, sb_session, u, p, s, t)
    track = await get_track(track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    path_str = track.path or ""
    ext, uade_named = _render_ident(path_str, track)
    wire = explicit_wire(subsong, request)
    # A bare play of a multi-tune file whose default tune isn't probed yet:
    # GET will probe first and may render another tune than tune 1 — so no
    # cached size is promised for it here.
    undecided = (wire is None and _tune_count(track) > 1 and not _header_tuned(track)
                 and default_tune_known(track_id, track) is None
                 and _probe_family(ext, uade_named) is not None)
    subsong = tune_index(track_id, track, wire)        # the key's tune
    want = (target_format or "").strip().lower() or None
    headers = {"Accept-Ranges": "bytes"}
    plain_local = not (is_remote_path(path_str) or "::" in path_str)
    size: int | None = None
    delivered = delivered_format(path_str, getattr(track, "format", "") or "", want)
    constrained = bool(max_bitrate_kbps) or bool(target_sample_rate)
    if delivered is None and not force_transcode and not constrained:
        mime = NATIVE.get(ext) or "audio/mp4"
        # An MP4-family file is served as is only once its codec is probed
        # (AAC; ALAC may be transcoded) — promise a length only for a
        # scanned AAC label.
        _fam = (getattr(track, "format", "") or "").split("/", 1)[0].strip().upper()
        if plain_local and (ext not in _MP4_EXTS or _fam == "AAC"):
            try:
                st = await asyncio.to_thread(os.stat, path_str)
                import stat as _stat
                if _stat.S_ISREG(st.st_mode):
                    size = st.st_size
            except OSError:
                raise HTTPException(410, f"File not found on disk: {track.path}")
    else:
        if delivered is None:
            delivered = ("wav", "audio/wav") if force_transcode else (
                (want if want in TRANSCODE_MIME else "mp3"),
                TRANSCODE_MIME.get(want or "", "audio/mpeg"))
        mime = delivered[1]
        c64_dur = 0
        if not constrained and delivered[0] == "wav":
            if ext in _SID_EXTS:
                # A plain local C64 tune: GET streams its render with the
                # exact length — the tune's target seconds — so that length is
                # known for free.  Its start song (which tune a bare play
                # renders, hence which length) is read as GET reads it — but
                # not remembered: a HEAD changes nothing.
                start_song = None
                if plain_local:
                    start_song = await asyncio.to_thread(_head_c64_start_song,
                                                         track_id, track, path_str)
                    if start_song is not None:
                        c64_dur = _sid_target_seconds(track, subsong, start_song)
                keys = [_ck(track_id, "sid", subsong=subsong,
                            duration=_sid_target_seconds(track, subsong, start_song)),
                        uade_cache_key_known(track_id, subsong, track)]
            elif ext in _MIDI_EXTS:
                from soniqboom.config import get_active_soundfont
                _sf = get_active_soundfont()
                keys = [_ck(track_id, "midi", 0, str(_sf) if _sf else "")]
            elif ext in _PSF_STREAM_EXTS and not uade_named:
                keys = [_ck(track_id, "psf", 0)]
            elif ext in _YM_EXTS:
                keys = [_ck(track_id, "ym", 0)]
            elif ext in _DSD_EXTS:
                keys = [_inflight_cache_key(track_id, _DSD_OUTPUT_RATE),
                        _ck(track_id, "psf", 0)]
            else:
                keys = [uade_cache_key_known(track_id, subsong, track)]
                keys += [_ck(track_id, fmt, subsong=subsong) for fmt in
                         ("tracker", "hvl", "sndh", "sc68", "gme", "adlib", "imf")]
                keys.append(_inflight_cache_key(track_id, None))
            for k in ([] if undecided else keys):
                size = _peek_cached_size(k)
                if size:
                    break
            if not size and c64_dur and not undecided:
                # What GET would answer instead of the stream: the same errors.
                from soniqboom.core.conversion_cache import known_silent, SILENT_RENDER_DETAIL
                if not _find_renderer(settings.sidplayfp_path, "sidplayfp"):
                    raise HTTPException(501, "sidplayfp not installed")
                if known_silent(keys[0]):
                    raise HTTPException(422, SILENT_RENDER_DETAIL)
                size = _WAV_HEADER_LEN + c64_dur * _SID_WAV_RATE * _SID_WAV_CHANNELS * (
                    _SID_WAV_BITS // 8)
    headers["Content-Type"] = mime
    if size:
        headers["Content-Length"] = str(size)
    resp = Response(status_code=200, headers=headers)
    if not size and "content-length" in resp.headers:
        # An unknown length is ABSENT, never "0" (that reads as an empty file).
        del resp.headers["content-length"]
    return resp
