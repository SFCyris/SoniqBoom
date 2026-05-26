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

# Default priority order when multiple folder-art files exist in the same
# directory.  The user can override this via the ``folder_art_names`` config
# key (admin UI → System → "Folder art filenames") — a comma-separated,
# case-insensitive ordered list where the first match wins.  This default
# is what ships before anyone has touched the setting.
_FOLDER_ART_PRIORITY_DEFAULT = [
    "cover.jpg", "folder.jpg", "front.jpg", "album.jpg",
    "cover.png", "folder.png", "front.png", "album.png",
    "cover.jpeg", "folder.jpeg", "front.jpeg", "album.jpeg",
]


# ── Folder-art shared cache + placeholder ───────────────────────────────────
#
# Two perf wins layered here:
#
# 1. **Shared folder-art cache keyed by ``dir_hash``** — when one track in
#    a folder triggers a folder-art fetch, the result is also cached under
#    ``folder:{dir_hash}`` so the OTHER 999 tracks in that folder don't
#    each have to repeat the ``list_dir`` + ``read_file`` round trip.
#    For a 1000-track FLAC album with one cover.jpg, this turns the FTP
#    cost from O(1000) list_dir calls + O(1000) read_file calls into
#    O(1) of each.
#
# 2. **Placeholder fallback** — when everything else returns ``None``,
#    the endpoint serves a tiny pre-baked grey JPEG with a strong ETag
#    and ``Cache-Control: public, max-age=31536000, immutable``.  Browser
#    requests the placeholder once, then re-uses it from disk cache for
#    every other tagless track for the life of the cache.  Beats 404s
#    that the UI has to handle per-track and that flood the access log.
_FOLDER_ART_KEY_PREFIX = "folder:"

# Pre-baked at import time so the endpoint never has to compute it on the
# hot path.  200×200 dark slate JPEG, ~1.5 KB.  Used for sm/lg/full alike
# — browser CSS rescales as needed (a placeholder isn't going to look
# "fuzzy" since it's already abstract).
def _build_placeholder_bytes() -> bytes:
    """Generate a small generic placeholder JPEG once at module import."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (200, 200), (38, 42, 54))
        draw = ImageDraw.Draw(img)
        # Subtle musical-note glyph via simple shapes (no font dependency).
        # A filled circle (the note head) + a vertical bar (the stem).
        draw.ellipse((78, 110, 110, 138), fill=(120, 128, 148))
        draw.rectangle((104, 60, 112, 124), fill=(120, 128, 148))
        # Tiny flag off the stem
        draw.polygon(
            [(112, 60), (138, 72), (138, 92), (112, 80)],
            fill=(120, 128, 148),
        )
        import io as _io
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=72, optimize=True)
        return buf.getvalue()
    except Exception:
        # Fall back to the smallest valid JPEG ever (1×1 grey) if PIL is
        # somehow unavailable.  Keeps the endpoint working without crashing.
        return _b64.b64decode(
            "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB"
            "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB/9sAQwEBAQEBAQEBAQEB"
            "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB"
            "AQEB/8AAEQgAAQABAwEiAAIRAQMRAf/EABUAAQEAAAAAAAAAAAAAAAAAAAAJ/8QAFBAB"
            "AAAAAAAAAAAAAAAAAAAAAP/EABQBAQAAAAAAAAAAAAAAAAAAAAD/xAAUEQEAAAAAAAAA"
            "AAAAAAAAAAAA/9oADAMBAAIRAxEAPwBVAB//2Q=="
        )


_PLACEHOLDER_JPEG: bytes = _build_placeholder_bytes()
_PLACEHOLDER_ETAG: str = '"placeholder-v1"'


def _folder_art_cache_key(dir_hash: str) -> str:
    """Cache-id under which we store folder-level art shared across all
    tracks in the same directory.  Picked the ``folder:`` prefix because
    ``_art_path`` keys by track_id and uuid4s never start with ``folder:``,
    so there's no collision risk."""
    return f"{_FOLDER_ART_KEY_PREFIX}{dir_hash}"


def _parse_folder_art_names(csv: str | None) -> list[str]:
    """Split a CSV folder-art filename list into an ordered, lower-cased
    priority list.

    Rules:
      * Whitespace around each entry is stripped.
      * Entries are lower-cased so matching against directory listings can
        be done case-insensitively on every platform (macOS happens to be
        case-insensitive by default, Linux is not — this keeps behaviour
        consistent).
      * Empty entries are dropped.
      * Duplicates are dropped, **first occurrence wins** — preserves the
        user-supplied order.
      * Empty / missing CSV falls back to ``_FOLDER_ART_PRIORITY_DEFAULT``
        so a freshly-installed server keeps the historical behaviour.
    """
    if not csv:
        return list(_FOLDER_ART_PRIORITY_DEFAULT)
    seen: set[str] = set()
    out: list[str] = []
    for raw in csv.split(","):
        name = raw.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out or list(_FOLDER_ART_PRIORITY_DEFAULT)

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


def _find_folder_art_local(
    track_dir: Path, priority: list[str],
) -> tuple[bytes, str] | tuple[None, None]:
    """Scan a local directory for cover/folder art images.

    ``priority`` is a lower-cased list of filenames in preference order
    (first match wins), typically produced by ``_parse_folder_art_names``
    from the ``folder_art_names`` admin setting.

    Returns (image_bytes, mime) for the highest-priority match, or
    (None, None).

    Implementation: one ``os.listdir`` + a lower-cased name lookup, rather
    than N ``os.path.exists`` probes against each candidate name.  Why:
      * ``os.path.exists`` is case-sensitive on Linux, so a user who
        configures ``"FOLDER.jpg"`` would never match an actual file
        named ``folder.jpg`` on a case-sensitive filesystem.  The
        single-listdir + case-folded lookup keeps the "case insensitive
        names" contract consistent across macOS and Linux.
      * On a 5 000-file directory the cost is one syscall instead of N;
        the previous N-exists path was faster only because the directory
        was usually tiny.  For the configurable-priority case the
        worst-case priority list is also unbounded, so the per-name
        approach scales worse than the listdir approach as the list
        grows.
    """
    import os as _os
    try:
        if not track_dir.is_dir():
            return None, None
    except OSError:
        return None, None
    try:
        # Case-insensitive lookup: map lowercased filename → real filename
        # (preserving on-disk case for the actual open()).  We iterate
        # ``priority`` rather than the directory so the order the admin
        # configured wins, not whatever order the filesystem returns.
        entries = {e.lower(): e for e in _os.listdir(track_dir)}
    except OSError:
        return None, None
    for lname in priority:
        actual = entries.get(lname)
        if not actual:
            continue
        candidate = track_dir / actual
        try:
            with open(candidate, "rb") as fh:
                data = fh.read()
            mime = "image/png" if lname.endswith(".png") else "image/jpeg"
            return data, mime
        except OSError:
            continue
    return None, None


def _find_folder_art_remote(
    scan_root: str, remote_dir: str, source, priority: list[str],
    *, lane: str = "stream",
) -> tuple[bytes, str] | tuple[None, None]:
    """Check a remote directory via FileSource for cover/folder art images.

    See ``_find_folder_art_local`` for the meaning of ``priority``.

    ``lane`` is forwarded to ``source.read_file`` so callers can pick the
    priority bucket on backends with priority pools (FTP).  Default
    ``"stream"`` matches the on-demand art endpoint (the cover is needed
    NOW for a playback render).  The scanner uses ``"scan"`` for
    prefetch warming so concurrent file extracts share the same lane
    budget rather than fighting playback for the 2-wide stream lane.

    Returns (image_bytes, mime) or (None, None).
    """
    try:
        entries = source.list_dir(remote_dir)
    except Exception:
        return None, None
    names = {e.name.lower(): e for e in entries if not e.is_dir}
    for lname in priority:
        entry = names.get(lname)
        if entry is not None:
            try:
                data = source.read_file(entry.path, lane=lane)
                mime = "image/png" if lname.endswith(".png") else "image/jpeg"
                return data, mime
            except Exception:
                continue
    return None, None


def _absent_sentinel_path(track_id: str) -> Path:
    """Path to the 0-byte ``.absent`` sentinel for *track_id*.

    Layout mirrors the art cache: ``<art_cache>/full/<id[:2]>/<id>.absent``.
    A sentinel file means "we already tried, and there is no art" — surviving
    process restart so we don't re-run mutagen extraction on every cold boot
    for tagless tracks.
    """
    from soniqboom.config import get_art_cache_dir
    prefix = (track_id[:2] or "__").lower()
    return get_art_cache_dir() / "full" / prefix / f"{track_id}.absent"


def _is_art_absent_persisted(track_id: str, source_mtime: float | None = None) -> bool:
    """Cheap on-disk check for a previously-recorded ``no art available``.

    The negative cache is also kept in-memory in ``store._art_absent``;
    this just gives us a persistent layer so a restart doesn't lose it.

    ``source_mtime``, when supplied, is the modification time of the
    underlying audio file (or its cached copy for remote sources).  The
    sentinel is honoured ONLY if the source hasn't been updated since the
    sentinel was written — otherwise we let the next extract retry.
    This fixes the false-negative we hit when an FTP/SMB cached file was
    incomplete on the first extract attempt and the sentinel got written
    against a partial download; once the cache finishes populating, the
    sentinel becomes stale and would otherwise lock us out of the art
    forever.
    """
    try:
        sentinel = _absent_sentinel_path(track_id)
        if not sentinel.exists():
            return False
        if source_mtime is None:
            return True
        try:
            sentinel_mtime = sentinel.stat().st_mtime
        except OSError:
            return False
        # Sentinel valid only when source hasn't changed since it was
        # written.  Small float tolerance avoids re-extracting on FS that
        # rounds mtimes to whole seconds (HFS+, FAT32).
        return source_mtime <= (sentinel_mtime + 0.5)
    except OSError:
        return False


def _source_mtime_for(path_str: str) -> float | None:
    """Best-effort mtime of the on-disk file we'd extract art from.

    For FTP/SMB tracks this is the cached local copy's mtime (the thing
    that bumps when a partial download is replaced by a full one); for
    ZIP-archived tracks it's the outer archive's mtime; for local files
    the file itself.  Returns ``None`` when nothing is on disk yet.
    """
    try:
        if path_str.startswith(("smb://", "ftp://")):
            from soniqboom.core.remote_cache import get_cache
            from soniqboom.core.filesource import parse_remote_path
            scan_root, remote_path = parse_remote_path(path_str)
            if not remote_path:
                return None
            cache = get_cache()
            try:
                local_path = cache.get_cached(scan_root, remote_path)
            except Exception:
                return None
            if local_path and local_path.exists():
                return local_path.stat().st_mtime
            return None
        if "::" in path_str:
            outer_zip = Path(path_str.split("::")[0])
            if outer_zip.exists():
                return outer_zip.stat().st_mtime
            return None
        p = Path(path_str)
        if p.exists():
            return p.stat().st_mtime
        return None
    except OSError:
        return None


def _mark_art_absent_persisted(track_id: str) -> None:
    """Write a 0-byte sentinel so a future cold boot remembers the miss.

    Best-effort: any OS error is logged and swallowed because the
    in-memory marker still applies for the current process lifetime.
    """
    try:
        sentinel = _absent_sentinel_path(track_id)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        # ``open(..., 'xb')`` would race with a concurrent attempt; use
        # ``touch`` semantics so the second writer is a no-op rather than
        # an error.  ``exist_ok=True`` mirrors the parent mkdir call.
        sentinel.touch(exist_ok=True)
    except OSError as exc:
        log.debug("Could not write absent-sentinel for %s: %s", track_id, exc)


def _clear_art_absent_persisted(track_id: str) -> None:
    """Remove the sentinel — used after a successful extract so the next
    request actually serves the new art."""
    try:
        _absent_sentinel_path(track_id).unlink(missing_ok=True)
    except OSError:
        pass


def _make_etag(track_id: str, size: str, mtime: float | None = None) -> str:
    """Build a deterministic ETag value from track id, size, and (optionally)
    the underlying art file's mtime.

    Including mtime means a re-extracted cover (same track id, new bytes)
    busts the client's cached 304 — previously the client kept serving the
    stale image until manual cache flush because the etag was identity-
    derived only.
    """
    if mtime is None:
        return f'"{track_id}:{size}"'
    return f'"{track_id}:{size}:{int(mtime)}"'


def _art_cached_mtime(track_id: str, size: str) -> float | None:
    """Look up the mtime of the cached art bytes for *track_id* at *size*.

    Returns ``None`` if no cached file exists.  Used by the ETag helper to
    embed the underlying byte's freshness without holding the bytes
    themselves in memory.
    """
    try:
        from soniqboom.core import art_cache as _ac
        # ``_art_path`` returns the on-disk path even when the file
        # doesn't exist yet; we just stat it conditionally.
        path = _ac._art_path(track_id, size)
    except Exception:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


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

    # Negative cache (in-memory) — skip extraction for tracks known to
    # have no art in the current process.
    if get_store().is_art_absent(track_id):
        return None, None

    track = await get_track(track_id)
    if not track:
        # No track row → respect any pre-existing sentinel verbatim and
        # write a fresh one for next time.
        if _is_art_absent_persisted(track_id):
            get_store().mark_art_absent(track_id)
            return None, None
        get_store().mark_art_absent(track_id)
        _mark_art_absent_persisted(track_id)
        return None, None

    path_str = track.path

    # Persistent negative cache — survives restart so we don't re-run
    # mutagen extraction on every cold boot for tagless tracks.  Honour
    # the sentinel only when the underlying source hasn't been updated
    # since it was written; otherwise a partial FTP/SMB download that
    # produced a tagless first-extract would lock the art out forever.
    if _is_art_absent_persisted(track_id, _source_mtime_for(path_str)):
        get_store().mark_art_absent(track_id)
        return None, None

    loop = asyncio.get_running_loop()

    # ── Step 1: try embedded art from the audio file ──────────────────────
    # Both _extract_cover and _extract_cover_from_zip do blocking mutagen /
    # zipfile I/O — run them in the default thread-pool so the event loop
    # stays responsive while other requests are served.
    data, mime = None, None

    if path_str.startswith(("smb://", "ftp://")):
        # Remote track — try to extract from cached local copy
        from soniqboom.core.remote_cache import get_cache
        from soniqboom.core.filesource import get_source, parse_remote_path
        scan_root, remote_path = parse_remote_path(path_str)
        if not remote_path:
            return None, None
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
        # Clear any prior absent-sentinel — the source must have been
        # updated since we last gave up on it.
        _clear_art_absent_persisted(track_id)
        asyncio.create_task(art_cache.store_art(track_id, data, "full"))
        asyncio.create_task(_generate_and_cache_thumbs(track_id, data))
        asyncio.create_task(_update_track_cover_ref(track_id))
        return data, mime or "image/jpeg"

    # ── Step 2: try folder art (folder.jpg / cover.jpg) ───────────────────
    use_folder_art = await get_config("use_folder_art", True)
    if use_folder_art:
        # Pass dir_hash so the SHARED folder-art cache can short-
        # circuit the list_dir + read_file round trip for every
        # other track in the same directory.
        folder_data, folder_mime = await _try_folder_art(
            path_str, loop, dir_hash=getattr(track, "dir_hash", None) or None,
        )
        if folder_data:
            _clear_art_absent_persisted(track_id)
            asyncio.create_task(art_cache.store_art(track_id, folder_data, "full"))
            asyncio.create_task(_generate_and_cache_thumbs(track_id, folder_data))
            asyncio.create_task(_update_track_cover_ref(track_id))
            return folder_data, folder_mime or "image/jpeg"

    # No art found — remember this to avoid repeated extraction attempts
    # (both in-memory for this process and on-disk for future restarts).
    get_store().mark_art_absent(track_id)
    _mark_art_absent_persisted(track_id)
    return None, None


async def _try_folder_art(
    path_str: str, loop: asyncio.AbstractEventLoop,
    *, dir_hash: str | None = None,
) -> tuple[bytes, str] | tuple[None, None]:
    """Attempt to find folder art for the track at *path_str*.

    Supports local paths, ZIP paths (uses outer directory), and remote paths
    (smb:// / ftp://).

    ``dir_hash`` (when supplied — populated by the scanner) enables the
    SHARED folder-art cache: the first track in a directory pays the
    ``list_dir`` + ``read_file`` cost; subsequent tracks in the same
    directory get the cover from cache for free.  Massive win for
    1000-track FLAC albums on FTP shares — turns O(N) folder-art
    fetches into O(1).
    """
    # ── Shared dir cache hit ───────────────────────────────────────────
    if dir_hash:
        cache_key = _folder_art_cache_key(dir_hash)
        cached = await art_cache.get_art(cache_key, "full")
        if cached:
            mime = "image/png" if cached[:4] == b"\x89PNG" else "image/jpeg"
            return cached, mime

    csv = await get_config("folder_art_names", "")
    priority = _parse_folder_art_names(csv if isinstance(csv, str) else "")

    data: bytes | None = None
    mime: str | None = None

    if path_str.startswith(("smb://", "ftp://")):
        # Remote path — directory listing via FileSource
        from soniqboom.core.filesource import get_source, parse_remote_path
        scan_root, remote_path = parse_remote_path(path_str)
        if not remote_path:
            return None, None
        source = get_source(scan_root)
        if source is None:
            return None, None
        # Parent directory of the remote file
        remote_dir = remote_path.rsplit("/", 1)[0] or "/"
        try:
            data, mime = await loop.run_in_executor(
                None, _find_folder_art_remote, scan_root, remote_dir, source,
                priority,
            )
        except Exception:
            data, mime = None, None

    elif '::' in path_str:
        # ZIP path — use the parent directory of the outer ZIP file
        outer_zip = Path(path_str.split('::')[0])
        track_dir = outer_zip.parent
        data, mime = await loop.run_in_executor(
            None, _find_folder_art_local, track_dir, priority,
        )

    else:
        track_dir = Path(path_str).parent
        data, mime = await loop.run_in_executor(
            None, _find_folder_art_local, track_dir, priority,
        )

    # ── Populate shared dir cache for future hits in this folder ──────
    # ``store_art`` runs fire-and-forget; the response to THIS caller
    # doesn't wait for the write to land on disk.  Subsequent tracks
    # in the same dir benefit from the cache as soon as the write
    # completes (typically a few milliseconds later).
    if data and dir_hash:
        try:
            asyncio.create_task(
                art_cache.store_art(_folder_art_cache_key(dir_hash), data, "full"),
            )
        except Exception:
            pass

    return data, mime


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

    The ETag now mixes in the underlying art file's mtime so a re-extracted
    cover busts the client's cached 304 — previously the client kept serving
    the stale image until a manual cache flush.
    """
    # Look up the cached art mtime to mix into the ETag.  If the art isn't
    # cached yet, fall back to the source track file's mtime (best
    # available freshness indicator before we actually extract).  Falling
    # through to a track-less ETag keeps the previous behaviour intact for
    # the 404 path.
    mtime = _art_cached_mtime(track_id, size)
    if mtime is None:
        # Touch the track record to find the source path's mtime as a
        # fallback — avoids breaking If-None-Match on the first request.
        try:
            track = await get_track(track_id)
            if track and not track.path.startswith(
                ("smb://", "ftp://", "http://", "https://"),
            ):
                mtime = Path(track.path).stat().st_mtime
        except OSError:
            mtime = None
        except Exception:
            mtime = None
    etag = _make_etag(track_id, size, mtime)

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

        # Nothing resolved — serve the placeholder.  Strong ETag means
        # the browser caches it once and never asks again for any
        # other tagless track.  Previous behaviour returned 404, which
        # forced the UI to handle the failure per-track AND meant
        # every scroll past a tagless track re-fired the request.
        return _placeholder_response()

    # --- Full size ----------------------------------------------------------
    full_data, mime = await _resolve_full_art(track_id)
    if full_data:
        # Also generate thumbs on first full-size hit so future thumb
        # requests are fast.
        asyncio.create_task(_generate_and_cache_thumbs(track_id, full_data))
        return _etag_response(full_data, mime or "image/jpeg", etag)

    return _placeholder_response()


def _placeholder_response() -> Response:
    """Return the pre-baked placeholder JPEG with strong cache headers.

    ETag is a constant (placeholder bytes don't change between requests)
    so the browser keeps re-using its first download forever — a single
    HTTP round trip covers every tagless track in the library.
    Immutable + 1-year max-age guarantees no revalidation.
    """
    return Response(
        content=_PLACEHOLDER_JPEG,
        media_type="image/jpeg",
        headers={
            "ETag": _PLACEHOLDER_ETAG,
            "Cache-Control": "public, max-age=31536000, immutable",
            # Hint to the UI's "is this real art?" check.  Frontend can
            # treat tracks served this header as art-less and avoid
            # showing a "View full size" affordance, etc.
            "X-SoniqBoom-Art": "placeholder",
        },
    )
