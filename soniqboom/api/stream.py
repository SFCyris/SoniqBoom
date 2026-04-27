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
import logging
import shutil
import tempfile
import uuid as _uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from soniqboom.config import settings
from soniqboom.core.data import get_track

log = logging.getLogger(__name__)


# ── Range-aware file serving ────────────────────────────────────────────────
# Starlette's FileResponse does NOT handle HTTP Range requests.  Browsers
# rely on Range for audio seeking (audio.currentTime = X triggers a
# Range: bytes=X- request).  Without 206 support, every seek restarts the
# stream from byte 0.

def _range_file_response(
    request: Request,
    file_path: Path | str,
    media_type: str,
    headers: dict[str, str] | None = None,
    background: BackgroundTask | None = None,
) -> Response:
    """Serve a file with HTTP Range support (single-range only)."""
    file_path = Path(file_path)
    stat = file_path.stat()
    total = stat.st_size
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

    # Read the requested byte range
    with open(file_path, "rb") as f:
        f.seek(start)
        data = f.read(length)

    extra["Content-Range"] = f"bytes {start}-{end}/{total}"
    extra["Content-Length"] = str(length)
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
_TRACKER_EXTS = {
    ".mod", ".s3m", ".xm", ".it", ".mtm", ".med", ".oct",
    ".669", ".dbm", ".ahx", ".hvl", ".ult", ".stm", ".far",
    ".amf", ".gdm", ".imf", ".okt", ".sfx", ".wow", ".dsm",
}


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


# ── SID rendering ─────────────────────────────────────────────────────────────

async def _render_sid(path: Path, subsong: int = 0) -> Path:
    """Render SID file to a temp WAV via sidplayfp and return the path."""
    binary = _find_renderer(settings.sidplayfp_path, "sidplayfp")
    if not binary:
        raise HTTPException(501, "sidplayfp not installed")

    duration = settings.sid_default_duration
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()

    cmd = [binary]
    if subsong > 0:
        cmd.append(f"-o{subsong}")      # sidplayfp flags: no space between flag and value
    cmd.extend([
        f"-t{duration}",
        f"-w{tmp_wav.name}",
        str(path),
    ])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return Path(tmp_wav.name)


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

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return Path(tmp_wav.name)


# ── Tracker module rendering ─────────────────────────────────────────────────

async def _render_tracker(path: Path, subsong: int = 0) -> Path:
    """Render tracker module to a temp WAV via openmpt123 and return the path."""
    binary = _find_renderer(settings.openmpt123_path, "openmpt123")
    if not binary:
        raise HTTPException(501, "openmpt123 not installed")

    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()

    cmd = [binary, "--batch", "--quiet", "--force", "-o", tmp_wav.name]
    if subsong > 0:
        cmd.extend(["--subsong", str(subsong)])
    cmd.extend(["--", str(path)])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
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
    while ffprobe inspects the file.
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
        stdout, _ = await proc.communicate()
        return stdout.decode().strip().lower() or None
    except Exception:
        return None


async def _transcode_stream(path: Path, seek_sec: float = 0.0):
    """Yield chunks from ffmpeg transcoding to the configured output format.

    seek_sec > 0 uses a fast pre-input seek (-ss before -i) so the user can
    jump to any position in a transcoded stream without re-decoding from start.
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
        "-f", fmt,
        "-acodec", codec,
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while chunk := await proc.stdout.read(65536):
            yield chunk
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


@router.get("/{track_id}/render-status")
async def render_status(
    track_id: str,
    subsong: int = Query(default=0, ge=0),
):
    """Check SID render state for progressive playback.

    Returns the configured target duration, what's currently cached, and
    whether the full-duration version is ready.
    """
    from soniqboom.core.conversion_cache import (
        is_cache_ready, _cache_key, get_cached, find_shorter_sid_entry,
    )
    target_dur = settings.sid_default_duration
    full_key = _cache_key(track_id, "sid", subsong)
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


async def _ingest_on_demand(track_id: str, file_path: str):
    """Extract metadata for a single file and upsert to store on-the-fly.

    Called when the stream endpoint receives a track_id that isn't in the
    store yet, but a ``path`` query parameter was provided (e.g. from the
    fstree browser).  This lets users play files immediately without waiting
    for a full library scan.

    Security: the path must hash to the expected track_id (uuid5) to prevent
    arbitrary file access.
    """
    from soniqboom.core.data import list_scan_dirs, path_hash, upsert_track
    from soniqboom.core.metadata import extract
    from soniqboom.models.track import Track

    # Verify the path produces the expected track_id
    expected_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, file_path))
    if expected_id != track_id:
        log.warning("On-demand ingest: path hash mismatch for %s", track_id)
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


@router.get("/{track_id}")
async def stream_track(
    track_id: str,
    request: Request,
    seek: float = Query(default=0.0, ge=0.0, description="Start position in seconds"),
    subsong: int = Query(default=0, ge=0, description="Sub-song index (SID/tracker)"),
    file_path: str | None = Query(default=None, alias="path",
                                  description="File path for on-demand ingestion"),
):
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

    if path_str.startswith(("smb://", "ftp://")):
        sep = path_str.index(":", 6)
        scan_root, remote_path = path_str[:sep], path_str[sep + 1:]
        from soniqboom.core.filesource import get_source
        from soniqboom.core.remote_cache import get_cache
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
                    raise HTTPException(502, f"Could not fetch remote file: {exc2}")
            else:
                log.warning("Remote fetch failed for %s: %s", path_str, exc)
                raise HTTPException(502, f"Could not fetch remote file: {exc}")
    elif '::' in path_str:
        # ZIP-contained file (supports nested zips via outer.zip::inner.zip::track.mod)
        parts = path_str.split('::')
        outer_zip = Path(parts[0])
        if not outer_zip.exists():
            raise HTTPException(410, f"ZIP archive not found: {outer_zip}")
        try:
            from soniqboom.core.scanner import _read_from_zip_path
            data, member_name = _read_from_zip_path(path_str)
        except Exception as exc:
            raise HTTPException(410, f"Cannot read from ZIP: {exc}")
        suffix = Path(member_name).suffix
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(data)
        tmp.close()
        path = Path(tmp.name)
        _zip_tmp = path
    else:
        path = Path(path_str)
        if not path.exists():
            raise HTTPException(410, f"File not found on disk: {track.path}")

    ext = Path(path_str.split('::')[-1] if '::' in path_str else path_str).suffix.lower()

    def _cleanup_tmp():
        if _zip_tmp is not None:
            _zip_tmp.unlink(missing_ok=True)

    _bg = BackgroundTask(_cleanup_tmp) if _zip_tmp else None

    # ── Rendered formats: SID / MIDI / Tracker ───────────────────────────────
    # These are cached as WAV files so repeat playback is instant.
    # On cache miss, the renderer runs and the result is stored for next time.
    from soniqboom.core.conversion_cache import get_or_render
    _zip_bg = BackgroundTask(_cleanup_paths, _zip_tmp) if _zip_tmp else None

    if ext in _SID_EXTS:
        from soniqboom.core.conversion_cache import (
            _cache_key, find_shorter_sid_entry,
            start_background_render, get_cached,
        )
        target_dur = settings.sid_default_duration
        full_key = _cache_key(track_id, "sid", subsong)

        # 1) Exact cache hit (correct duration)
        exact = await get_cached(full_key)
        if exact:
            return _range_file_response(
                request, exact, media_type="audio/wav",
                headers={"X-Rendered": "sidplayfp", "X-Cache": "hit",
                         "X-SID-Target-Seconds": str(target_dur)},
                background=_zip_bg,
            )

        # 2) Shorter version available — serve it now, render full in background
        shorter = await find_shorter_sid_entry(track_id, subsong, target_dur)
        if shorter:
            short_path, short_dur = shorter
            await start_background_render(
                full_key, "sid", lambda: _render_sid(path, subsong=subsong),
            )
            return _range_file_response(
                request, short_path, media_type="audio/wav",
                headers={"X-Rendered": "sidplayfp", "X-Cache": "partial",
                         "X-SID-Cached-Seconds": str(short_dur),
                         "X-SID-Target-Seconds": str(target_dur)},
                background=_zip_bg,
            )

        # 3) No cache at all — render synchronously
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="sid", subsong=subsong,
            render_fn=lambda: _render_sid(path, subsong=subsong),
        )
        return _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "sidplayfp", "X-Cache": "hit" if hit else "miss",
                     "X-SID-Target-Seconds": str(target_dur)},
            background=_zip_bg,
        )
    if ext in _MIDI_EXTS:
        from soniqboom.config import get_active_soundfont
        sf = get_active_soundfont()
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="midi", subsong=0,
            render_fn=lambda: _render_midi(path),
            soundfont_path=str(sf) if sf else "",
        )
        return _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "fluidsynth", "X-Cache": "hit" if hit else "miss"},
            background=_zip_bg,
        )
    if ext in _TRACKER_EXTS:
        cached_path, hit = await get_or_render(
            track_id=track_id, format_type="tracker", subsong=subsong,
            render_fn=lambda: _render_tracker(path, subsong=subsong),
        )
        return _range_file_response(
            request, cached_path, media_type="audio/wav",
            headers={"X-Rendered": "openmpt123", "X-Cache": "hit" if hit else "miss"},
            background=_zip_bg,
        )

    # ── Native: serve directly with Range support ─────────────────────────────
    if ext in NATIVE:
        return _range_file_response(
            request, path, media_type=NATIVE[ext],
            background=_bg,
        )

    # ── .m4a / .aac: probe codec first ───────────────────────────────────────
    # AAC in .m4a → browsers can play it natively (serve directly).
    # ALAC in .m4a → must transcode (Chrome/Firefox cannot decode ALAC).
    # Probe result is reused in the transcode header to avoid a second call.
    detected_codec: str | None = None
    if ext in (".m4a", ".aac"):
        detected_codec = await _probe_codec(path)
        if detected_codec == "aac":
            return _range_file_response(
                request, path, media_type="audio/mp4",
                background=_bg,
            )
        # Safari decodes ALAC natively; transcoding to FLAC would break it,
        # since Safari doesn't support raw audio/flac in <audio>.
        if detected_codec == "alac" and _is_safari(request):
            return _range_file_response(
                request, path, media_type="audio/mp4",
                background=_bg,
            )
        # ALAC on non-Safari, or unknown → fall through to transcode

    # ── Transcode ─────────────────────────────────────────────────────────────
    mime = TRANSCODE_MIME.get(settings.transcode_format, "audio/flac")
    # Use already-probed codec if available; otherwise use the file extension
    # as a best-effort label (avoids a second ffprobe call for the header).
    codec_label = detected_codec or ext.lstrip(".") or "unknown"
    return StreamingResponse(
        _transcode_stream(path, seek_sec=seek),
        media_type=mime,
        headers={"X-Transcoded": "1", "X-Original-Codec": codec_label},
        background=_bg,
    )
