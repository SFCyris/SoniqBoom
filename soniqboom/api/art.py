# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cover art endpoint — extracts embedded artwork from audio files.

Fallback chain: embedded art → folder art (folder.jpg / cover.jpg) → 404.
"""
from __future__ import annotations

import asyncio
import base64 as _b64
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from soniqboom.core import art_cache
from soniqboom.core.data import get_track, get_config
from soniqboom.core.metadata import resize_cover
from soniqboom.core.store import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/art", tags=["art"])

_FOLDER_ART_NAMES = frozenset({
    "folder.jpg", "cover.jpg", "front.jpg", "album.jpg",
    "folder.png", "cover.png", "front.png", "album.png",
    "folder.jpeg", "cover.jpeg", "front.jpeg", "album.jpeg",
})

# Priority order when multiple folder art files exist in the same directory
_FOLDER_ART_PRIORITY = [
    "cover.jpg", "folder.jpg", "front.jpg", "album.jpg",
    "cover.png", "folder.png", "front.png", "album.png",
    "cover.jpeg", "folder.jpeg", "front.jpeg", "album.jpeg",
]

_SIZE_MAP = {
    "sm": 200,
    "lg": 550,
}


def _extract_cover(path: Path) -> tuple[bytes, str] | tuple[None, None]:
    """Return (image_bytes, mime_type) from an audio file, or (None, None)."""
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.mp3 import MP3
            audio = MP3(path)
            for tag in (audio.tags or {}).values():
                if hasattr(tag, "data") and hasattr(tag, "mime") and tag.data:
                    mime = (tag.mime[0] if isinstance(tag.mime, list) else tag.mime) or "image/jpeg"
                    return tag.data, mime

        elif ext in (".m4a", ".aac", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(path)
            covers = (audio.tags or {}).get("covr", [])
            if covers:
                fmt  = getattr(covers[0], "imageformat", MP4Cover.FORMAT_JPEG)
                mime = "image/png" if fmt == MP4Cover.FORMAT_PNG else "image/jpeg"
                return bytes(covers[0]), mime

        elif ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(path)
            if audio.pictures:
                p = audio.pictures[0]
                return p.data, (p.mime or "image/jpeg")

        elif ext in (".ogg", ".opus"):
            import struct
            from mutagen.oggvorbis import OggVorbis
            try:
                audio = OggVorbis(path)
            except Exception:
                from mutagen.oggopus import OggOpus
                audio = OggOpus(path)
            for b64 in audio.get("metadata_block_picture", []):
                raw = _b64.b64decode(b64)
                # FLAC PICTURE block: [4B type][4B mime_len][mime][4B desc_len][desc][4B w][4B h][4B depth][4B colors][4B data_len][data]
                off = 4
                mime_len = struct.unpack(">I", raw[off:off+4])[0]; off += 4
                mime = raw[off:off+mime_len].decode(); off += mime_len
                desc_len = struct.unpack(">I", raw[off:off+4])[0]; off += 4 + desc_len
                off += 16  # width, height, depth, colors
                data_len = struct.unpack(">I", raw[off:off+4])[0]; off += 4
                return raw[off:off+data_len], (mime or "image/jpeg")

        elif ext in (".wv", ".ape"):
            from mutagen.apev2 import APEv2
            tags = APEv2(path)
            item = tags.get("Cover Art (Front)")
            if item:
                # APEv2 cover: null-terminated filename then raw bytes
                data = bytes(item.value)
                null = data.find(b"\x00")
                if null != -1:
                    return data[null+1:], "image/jpeg"

    except Exception:
        pass
    return None, None


def _extract_cover_from_zip(virtual_path: str) -> tuple[bytes, str] | tuple[None, None]:
    """Extract cover art from a file inside a (possibly nested) ZIP archive."""
    import tempfile
    try:
        from soniqboom.core.scanner import _read_from_zip_path
        data, member_name = _read_from_zip_path(virtual_path)
        suffix = Path(member_name).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            return _extract_cover(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception:
        return None, None


def _find_folder_art_local(track_dir: Path) -> tuple[bytes, str] | tuple[None, None]:
    """Scan a local directory for cover/folder art images.

    Returns (image_bytes, mime) for the highest-priority match, or (None, None).
    """
    if not track_dir.is_dir():
        return None, None
    # Build a set of existing filenames (lowered) for fast lookup
    try:
        existing = {e.name.lower(): e for e in track_dir.iterdir() if e.is_file()}
    except OSError:
        return None, None
    for name in _FOLDER_ART_PRIORITY:
        entry = existing.get(name)
        if entry is not None:
            try:
                data = entry.read_bytes()
                mime = "image/png" if name.endswith(".png") else "image/jpeg"
                return data, mime
            except OSError:
                continue
    return None, None


def _find_folder_art_remote(
    scan_root: str, remote_dir: str, source
) -> tuple[bytes, str] | tuple[None, None]:
    """Check a remote directory via FileSource for cover/folder art images.

    Returns (image_bytes, mime) or (None, None).
    """
    try:
        entries = source.list_dir(remote_dir)
    except Exception:
        return None, None
    names = {e.name.lower(): e for e in entries if not e.is_dir}
    for name in _FOLDER_ART_PRIORITY:
        entry = names.get(name)
        if entry is not None:
            try:
                data = source.read_file(entry.path)
                mime = "image/png" if name.endswith(".png") else "image/jpeg"
                return data, mime
            except Exception:
                continue
    return None, None


def _make_etag(track_id: str, size: str) -> str:
    """Build a deterministic ETag value from track id and size."""
    return f'"{track_id}:{size}"'


def _etag_response(
    data: bytes,
    media_type: str,
    etag: str,
) -> Response:
    """Build a Response with ETag and cache headers."""
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Cache-Control": "max-age=86400, immutable",
            "ETag": etag,
        },
    )


async def _resolve_full_art(track_id: str) -> tuple[bytes, str] | tuple[None, None]:
    """Return full-size cover bytes and mime, trying filesystem cache then file extraction.

    Fallback chain:
      1. Art cache (already extracted/stored)
      2. Embedded art from the audio file (mutagen extraction)
      3. Folder art (folder.jpg / cover.jpg) — if enabled via ``use_folder_art`` config
      4. Negative cache → 404

    On extraction, caches to the filesystem and generates + caches thumbnails.
    """
    # Try filesystem cache first
    cached = await art_cache.get_art(track_id, "full")
    if cached:
        # Detect mime from magic bytes; default to JPEG
        mime = "image/png" if cached[:4] == b"\x89PNG" else "image/jpeg"
        return cached, mime

    # Negative cache: skip extraction for tracks known to have no art
    if get_store().is_art_absent(track_id):
        return None, None

    track = await get_track(track_id)
    if not track:
        get_store().mark_art_absent(track_id)
        return None, None

    loop = asyncio.get_running_loop()
    path_str = track.path

    # ── Step 1: try embedded art from the audio file ──────────────────────
    # Both _extract_cover and _extract_cover_from_zip do blocking mutagen /
    # zipfile I/O — run them in the default thread-pool so the event loop
    # stays responsive while other requests are served.
    data, mime = None, None

    if path_str.startswith(("smb://", "ftp://")):
        # Remote track — try to extract from cached local copy
        from soniqboom.core.remote_cache import get_cache
        from soniqboom.core.filesource import get_source
        sep = path_str.index(":", 6)
        scan_root, remote_path = path_str[:sep], path_str[sep + 1:]
        source = get_source(scan_root)
        cache = get_cache()
        try:
            local_path = cache.get_cached(scan_root, remote_path)
            if local_path and local_path.exists():
                data, mime = await loop.run_in_executor(
                    None, _extract_cover, local_path
                )
        except Exception:
            pass
    elif '::' in path_str:
        outer_zip = Path(path_str.split('::')[0])
        if outer_zip.exists():
            data, mime = await loop.run_in_executor(
                None, _extract_cover_from_zip, path_str
            )
    else:
        path = Path(path_str)
        if path.exists():
            data, mime = await loop.run_in_executor(
                None, _extract_cover, path
            )

    if data:
        asyncio.create_task(art_cache.store_art(track_id, data, "full"))
        asyncio.create_task(_generate_and_cache_thumbs(track_id, data))
        asyncio.create_task(_update_track_cover_ref(track_id))
        return data, mime or "image/jpeg"

    # ── Step 2: try folder art (folder.jpg / cover.jpg) ───────────────────
    use_folder_art = await get_config("use_folder_art", True)
    if use_folder_art:
        folder_data, folder_mime = await _try_folder_art(path_str, loop)
        if folder_data:
            asyncio.create_task(art_cache.store_art(track_id, folder_data, "full"))
            asyncio.create_task(_generate_and_cache_thumbs(track_id, folder_data))
            asyncio.create_task(_update_track_cover_ref(track_id))
            return folder_data, folder_mime or "image/jpeg"

    # No art found — remember this to avoid repeated extraction attempts
    get_store().mark_art_absent(track_id)
    return None, None


async def _try_folder_art(
    path_str: str, loop: asyncio.AbstractEventLoop
) -> tuple[bytes, str] | tuple[None, None]:
    """Attempt to find folder art for the track at *path_str*.

    Supports local paths, ZIP paths (uses outer directory), and remote paths
    (smb:// / ftp://).
    """
    if path_str.startswith(("smb://", "ftp://")):
        # Remote path — directory listing via FileSource
        from soniqboom.core.filesource import get_source
        sep = path_str.index(":", 6)
        scan_root, remote_path = path_str[:sep], path_str[sep + 1:]
        source = get_source(scan_root)
        if source is None:
            return None, None
        # Parent directory of the remote file
        remote_dir = remote_path.rsplit("/", 1)[0] or "/"
        try:
            return await loop.run_in_executor(
                None, _find_folder_art_remote, scan_root, remote_dir, source
            )
        except Exception:
            return None, None

    elif '::' in path_str:
        # ZIP path — use the parent directory of the outer ZIP file
        outer_zip = Path(path_str.split('::')[0])
        track_dir = outer_zip.parent
        return await loop.run_in_executor(None, _find_folder_art_local, track_dir)

    else:
        track_dir = Path(path_str).parent
        return await loop.run_in_executor(None, _find_folder_art_local, track_dir)


async def _update_track_cover_ref(track_id: str) -> None:
    """Set the cover_art URL reference on the track document."""
    try:
        get_store().update_track_fields(track_id, {"cover_art": f"/api/art/{track_id}"})
    except Exception:
        pass


async def _generate_and_cache_thumbs(track_id: str, full_data: bytes) -> dict[str, bytes]:
    """Resize full art into sm/lg thumbnails, cache them, and return the mapping.

    PIL image decoding + JPEG re-encoding is CPU-bound; running it in the
    thread-pool keeps the event loop free for other requests.
    """
    loop = asyncio.get_running_loop()
    sm_bytes, lg_bytes = await loop.run_in_executor(
        None, lambda: (resize_cover(full_data, 200), resize_cover(full_data, 550))
    )
    asyncio.create_task(
        art_cache.store_thumbs_batch({track_id: sm_bytes}, {track_id: lg_bytes})
    )
    return {"sm": sm_bytes, "lg": lg_bytes}


@router.get("/{track_id}")
async def cover_art(
    track_id: str,
    request: Request,
    size: str = Query("sm", pattern="^(sm|lg|full)$"),
):
    """Serve cover art with optional thumbnail sizing and ETag caching.

    Query params:
        size — ``sm`` (200px, default), ``lg`` (550px), or ``full`` (original).
    """
    etag = _make_etag(track_id, size)

    # --- ETag: return 304 if client already has this version ----------------
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})

    # --- Thumbnails (sm / lg) ----------------------------------------------
    if size in _SIZE_MAP:
        # Check dedicated thumbnail cache first
        thumb = await art_cache.get_art(track_id, size)
        if thumb:
            return _etag_response(thumb, "image/jpeg", etag)

        # Fallback: get full art, resize, cache, and serve
        full_data, mime = await _resolve_full_art(track_id)
        if full_data:
            thumbs = await _generate_and_cache_thumbs(track_id, full_data)
            return _etag_response(thumbs[size], "image/jpeg", etag)

        raise HTTPException(404, "No artwork found")

    # --- Full size ----------------------------------------------------------
    full_data, mime = await _resolve_full_art(track_id)
    if full_data:
        # Also generate thumbs on first full-size hit so future thumb
        # requests are fast.
        asyncio.create_task(_generate_and_cache_thumbs(track_id, full_data))
        return _etag_response(full_data, mime or "image/jpeg", etag)

    raise HTTPException(404, "No artwork found")
