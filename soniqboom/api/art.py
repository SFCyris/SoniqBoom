# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cover art endpoint — extracts embedded artwork from audio files.

Fallback chain: embedded art → folder art (folder.jpg / cover.jpg) → 404.
"""
from __future__ import annotations

import asyncio
import base64 as _b64
import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from soniqboom.core import art_cache
from soniqboom.core.data import get_track, get_config
from soniqboom.core.metadata import resize_cover, cap_full_cover
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


def _extract_cover(path: Path, *, raise_on_error: bool = False) -> tuple[bytes, str] | tuple[None, None]:
    """Return (image_bytes, mime_type) from an audio file, or (None, None).

    With ``raise_on_error=True`` a read/parse failure (a truncated file, or a
    flaky mount that drops mid-read) PROPAGATES instead of being swallowed as
    "no art".  The caller uses that distinction to avoid caching a false
    negative for a file it simply couldn't read — the difference between
    "opened it, genuinely no cover" and "couldn't open it".
    """
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
        if raise_on_error:
            raise
    return None, None


def _extract_cover_from_zip(virtual_path: str, *, raise_on_error: bool = False) -> tuple[bytes, str] | tuple[None, None]:
    """Extract cover art from a file inside a (possibly nested) ZIP archive.

    ``raise_on_error=True`` propagates a read failure (unreadable/partial
    archive) instead of returning it as "no art" — see ``_extract_cover``.
    """
    import tempfile
    tmp_path: Path | None = None
    try:
        from soniqboom.core.scanner import _read_from_zip_path
        data, member_name = _read_from_zip_path(virtual_path)
        suffix = Path(member_name).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)   # bind BEFORE write so a failed write still cleans up
            tmp.write(data)
        return _extract_cover(tmp_path, raise_on_error=raise_on_error)
    except Exception:
        if raise_on_error:
            raise
        return None, None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


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
            # Unknown source mtime → we CANNOT prove the sentinel is still
            # valid, so don't honour it: re-attempt instead.  This is the
            # common case for remote (FTP/SMB) tracks not currently in the
            # local cache, where the old ``return True`` permanently locked out
            # folder art that arrived after the sentinel was first written.
            # The re-attempt is cheap — embedded extraction only reads an
            # existing local cache copy (never downloads), and folder art comes
            # from the warm per-directory cache.
            return False
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


# Per-directory memo for the folder-art mtime scan.  A fast list scroll fires
# one art request per visible row, and many rows share a directory; without a
# memo each would re-``scandir`` the whole folder.  Short TTL so a cover
# dropped in is still noticed within ~a couple of seconds.  Accessed from
# executor threads — dict get/set are individually atomic under the GIL and a
# check-then-scan race only costs a redundant scan, so no lock is needed.
_FOLDER_MTIME_MEMO: dict[str, tuple[float | None, float]] = {}
_FOLDER_MTIME_TTL = 2.0            # seconds
_FOLDER_MTIME_MEMO_CAP = 4096     # bounded; cleared wholesale when exceeded


def _newest_folder_art_mtime(track_dir: Path) -> float | None:
    """Newest mtime among folder-art candidate images in *track_dir*, or None.

    Case-insensitive match against the built-in folder-art names via a single
    ``scandir`` (mirrors ``_find_folder_art_local``'s case-folding contract),
    memoized per directory for ``_FOLDER_MTIME_TTL`` seconds.  Folding this into
    the negative-cache freshness signal is what lets a cover image DROPPED INTO
    the folder after a "no art" miss invalidate that miss — the audio file's own
    mtime never changes when a sibling file is added, so the file mtime alone
    can't see it.  MUST stay off the event loop (see ``_source_mtime_for_async``)
    since ``scandir`` can block on a hung mount.
    """
    key = str(track_dir)
    now = time.monotonic()
    hit = _FOLDER_MTIME_MEMO.get(key)
    if hit is not None and hit[1] > now:
        return hit[0]
    newest: float | None = None
    try:
        with os.scandir(track_dir) as it:
            for entry in it:
                if entry.name.lower() in _FOLDER_ART_NAMES:
                    try:
                        m = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    if newest is None or m > newest:
                        newest = m
    except OSError:
        newest = None
    if len(_FOLDER_MTIME_MEMO) >= _FOLDER_MTIME_MEMO_CAP:
        _FOLDER_MTIME_MEMO.clear()
    _FOLDER_MTIME_MEMO[key] = (newest, now + _FOLDER_MTIME_TTL)
    return newest


async def _source_mtime_for_async(path_str: str) -> float | None:
    """Off-loop wrapper for :func:`_source_mtime_for`.

    ``_source_mtime_for`` does ``stat`` + ``scandir`` I/O that can block for the
    full mount timeout on a hung/flaky ``/Volumes`` mount.  Running it in the
    default thread-pool keeps the event loop (and every other client) responsive
    while one request waits on a bad mount.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _source_mtime_for, path_str)


def _source_mtime_for(path_str: str) -> float | None:
    """Best-effort mtime of the on-disk file we'd extract art from.

    For LOCAL and ZIP tracks the returned value also folds in the newest
    folder-art candidate mtime in the track's directory (via
    ``_newest_folder_art_mtime``), so a cover image added to the folder AFTER a
    "no art" miss makes this value newer than the sentinel and re-opens the
    art.  (Remote folder art is handled by the remote backfill path instead.)

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
                mt = outer_zip.stat().st_mtime
                folder_mt = _newest_folder_art_mtime(outer_zip.parent)
                return max(mt, folder_mt) if folder_mt is not None else mt
            return None
        p = Path(path_str)
        if p.exists():
            mt = p.stat().st_mtime
            folder_mt = _newest_folder_art_mtime(p.parent)
            return max(mt, folder_mt) if folder_mt is not None else mt
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


def purge_absent_sentinels() -> int:
    """Delete every on-disk ``.absent`` sentinel; returns how many were removed.

    Used when a CONFIG change (enabling folder art, or reordering
    ``folder_art_names``) should force ALL previously-"no art" tracks to be
    re-evaluated.  The per-file mtime guard alone can't cover this case: the
    folder image may have PRE-DATED the sentinel, so its mtime isn't newer.
    Pairs with the in-memory ``store.clear_art_absent()`` at the same sites.
    """
    from soniqboom.config import get_art_cache_dir
    base = get_art_cache_dir() / "full"
    removed = 0
    try:
        for sub in base.iterdir():
            if not sub.is_dir():
                continue
            for sentinel in sub.glob("*.absent"):
                try:
                    sentinel.unlink()
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


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

    track = await get_track(track_id)
    if not track:
        # No track row (e.g. deleted between the list render and this art
        # fetch) → remember the miss.  There's no source file to mtime-guard
        # against, so it's a plain sentinel.  (The old ``_is_art_absent_persisted``
        # pre-check here was dead once that helper started returning False for a
        # None mtime — this is the same outcome without the dead branch.)
        get_store().mark_art_absent(track_id)
        _mark_art_absent_persisted(track_id)
        return None, None

    path_str = track.path

    # ── Negative cache (single mtime-guarded authority) ───────────────────
    # Honour a recorded "no art" miss ONLY when the source is UNCHANGED since
    # the miss.  ``_source_mtime_for`` folds in BOTH the audio file's mtime and
    # the newest folder-art candidate in its directory, so this one gate
    # replaces the old bare in-memory short-circuit (which had no invalidation)
    # AND the file-only persistent check.  The payoff:
    #   * a re-tagged file (its mtime bumps) OR a folder.jpg dropped in later
    #     (the folder mtime bumps) makes the miss STALE → we fall through and
    #     re-extract, so newly-available art actually appears;
    #   * a genuinely-tagless, unchanged file stays cached (fast path);
    #   * an offline mount yields src_mtime=None → not honoured → we re-attempt
    #     (and the read guard below keeps us from poisoning it).
    src_mtime = await _source_mtime_for_async(path_str)
    if _is_art_absent_persisted(track_id, src_mtime):
        get_store().mark_art_absent(track_id)          # keep / refresh the hint
        return None, None
    # Miss is absent or stale — drop any lingering in-memory hint so a marker
    # set before the source changed can't keep blocking, then re-extract.
    if get_store().is_art_absent(track_id):
        get_store().discard_art_absent(track_id)

    loop = asyncio.get_running_loop()

    # ── Step 1: try embedded art from the audio file ──────────────────────
    # Both _extract_cover and _extract_cover_from_zip do blocking mutagen /
    # zipfile I/O — run them in the default thread-pool so the event loop
    # stays responsive while other requests are served.
    data, mime = None, None
    # Did we actually open a PRESENT source file/archive, and did the read
    # SUCCEED?  We only write a negative sentinel when a genuinely-accessible
    # file was read cleanly and had no art — never when the file was
    # unreachable (offline /Volumes mount, cold remote cache) OR present-by-stat
    # but unreadable-by-content (a mount that drops mid-read, an EIO on a flaky
    # external disk).  Both are transient; poisoning them locks real art out
    # until a manual cache clear — the exact bug class we're closing.
    source_present = False
    read_failed = False

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
            if "::" in remote_path:
                # Composite remote-archive member — split the "::" tail FIRST so
                # we look up (and extract from) the OUTER archive, not the whole
                # "archive.zip::member" string (which is never a cache key, so the
                # embedded cover was silently never extracted).  The cover lives
                # inside the member, so extract it from that member within the
                # locally-cached archive.  Mirror the non-blocking get_cached
                # semantics of the plain-remote branch below (no forced fetch —
                # the is_remote backfill covers the not-yet-cached case).
                arc_rel, member = remote_path.split("::", 1)
                local_path = cache.get_cached(scan_root, arc_rel)
                if local_path and local_path.exists():
                    data, mime = await loop.run_in_executor(
                        None, lambda: _extract_cover_from_zip(f"{local_path}::{member}")
                    )
            else:
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
            source_present = True
            try:
                data, mime = await loop.run_in_executor(
                    None, lambda: _extract_cover_from_zip(path_str, raise_on_error=True)
                )
            except Exception:
                read_failed = True
    else:
        path = Path(path_str)
        if path.exists():
            source_present = True
            try:
                data, mime = await loop.run_in_executor(
                    None, lambda: _extract_cover(path, raise_on_error=True)
                )
            except Exception:
                read_failed = True

    if data:
        # Clear any prior absent-sentinel — the source must have been
        # updated since we last gave up on it.
        _clear_art_absent_persisted(track_id)
        # Cache (full + thumbs) and BROADCAST art_ready so the list refreshes —
        # the player owns this response's bytes, but the list <img> for the same
        # track only learns the art exists via the broadcast.
        _persist_and_notify(track_id, data)
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
            _persist_and_notify(track_id, folder_data)
            return folder_data, folder_mime or "image/jpeg"

    # No art found.  Remember it to skip repeated extraction — BUT only when we
    # actually opened a PRESENT local / zip file and it had no embedded and no
    # folder art.  Two failure modes must NOT poison the negative cache:
    #
    #   * Remote (FTP/SMB) tracks — the folder-art lookup can come up empty for
    #     transient reasons (cold folder cache, an FTP listing hiccup), and a
    #     sentinel written against an unknowable source mtime locks the art out
    #     forever (the Linux FTP "no art" bug).
    #   * A LOCAL file that wasn't accessible this pass (``source_present`` is
    #     False) — e.g. an offline ``/Volumes`` mount — OR that was present but
    #     could not be READ (``read_failed`` — a mount that dropped mid-read, an
    #     EIO).  Both are the SAME transient failure: the file's mtime never
    #     changes across the outage, so once it recovers the (newer) sentinel
    #     stays "valid" and the mtime guard in ``_is_art_absent_persisted`` can
    #     never invalidate it — real embedded art locked out until a cache clear.
    #
    # Re-attempts in both cases are cheap: a track that DOES have art is served
    # from the positive art cache after the first resolve, and an absent local
    # file fails ``path.exists()`` in microseconds.
    is_remote = path_str.startswith(("ftp://", "smb://"))
    if is_remote:
        # Remote track with no art resolvable from local state.  If the scan
        # recorded an embedded cover (``cover_art`` URL set) but the bytes
        # aren't cached, recover them surgically in the BACKGROUND (fetch just
        # the moov/tag-header, not the whole file) and push ``art_ready`` when
        # done — so this request never blocks on a remote read.  The backfill
        # coalesces + bounds itself.
        try:
            if getattr(track, "cover_art", None):
                from soniqboom.core.art_backfill import request_backfill
                request_backfill(track)
        except Exception:
            pass
    elif source_present and not read_failed:
        # Genuinely-present local / zip file that we READ cleanly and which had
        # no embedded and no folder art — a real "tagless" track; cache the miss
        # so we don't re-run mutagen on every request.
        get_store().mark_art_absent(track_id)
        _mark_art_absent_persisted(track_id)
    # else: source not accessible this pass, or present-but-unreadable
    # (read_failed) → transient; don't poison, re-attempt once it's readable.
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
            async def _store_capped(bytes_=data, key=_folder_art_cache_key(dir_hash)):
                loop = asyncio.get_running_loop()
                capped = await loop.run_in_executor(None, cap_full_cover, bytes_)
                await art_cache.store_art(key, capped, "full")
            asyncio.create_task(_store_capped())
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
    # AWAIT the disk write (was fire-and-forget) so callers — especially the
    # backfill, which broadcasts ``art_ready`` right after — can rely on the
    # thumbnails actually being on disk before a client re-requests them.
    await art_cache.store_thumbs_batch({track_id: sm_bytes}, {track_id: lg_bytes})
    return {"sm": sm_bytes, "lg": lg_bytes}


# Strong refs to in-flight persist/notify tasks — without this, asyncio only
# holds a weak ref and the GC can cancel the write/broadcast mid-flight.
_bg_tasks: set[asyncio.Task] = set()


def _persist_and_notify(track_id: str, data: bytes) -> None:
    """Cache freshly-extracted art (full + thumbs), update the DB cover ref, and
    THEN broadcast ``art_ready`` so list/grid ``<img>`` elements refresh.

    Fire-and-forget but GC-safe (held in ``_bg_tasks``).  The broadcast was the
    missing link: ``_resolve_full_art`` cached art on demand but never told
    clients, so a track resolved by the player never refreshed its list row.
    Broadcasting only AFTER the thumbs are on disk means the client's refetch
    hits the cache instead of re-extracting (or getting a placeholder).
    """
    async def _job():
        try:
            # Cap the stored 'full' — nothing renders it at native resolution
            # (see cap_full_cover); off-loop since it may re-encode a large
            # image.  Thumbs derive from the same (capped) source — a 1024px
            # cap is still well above the 550px lg thumbnail.
            loop = asyncio.get_running_loop()
            full = await loop.run_in_executor(None, cap_full_cover, data)
            await art_cache.store_art(track_id, full, "full")
            await _generate_and_cache_thumbs(track_id, full)
            await _update_track_cover_ref(track_id)
        except Exception:
            log.debug("art persist failed for %s", track_id, exc_info=True)
        try:
            from soniqboom.api.library import _broadcast
            await _broadcast({"event": "art_ready", "track_id": track_id})
        except Exception:
            pass
    try:
        t = asyncio.get_running_loop().create_task(_job())
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    except RuntimeError:
        pass


@router.get("/{track_id}")
async def cover_art(
    track_id: str,
    request: Request,
    size: str = Query("sm", pattern="^(sm|lg|full)$"),
    fallback: str = Query("placeholder", pattern="^(placeholder|404)$"),
):
    """Serve cover art with optional thumbnail sizing and ETag caching.

    Query params:
        size — ``sm`` (200px, default), ``lg`` (550px), or ``full`` (original).
        fallback — what to return when no real art exists:
            * ``placeholder`` (default) — a tiny grey JPEG with the
              ``X-SoniqBoom-Art: placeholder`` header.  Cheaply cacheable
              by ETag so every tagless track in a library shares one
              download.  This is what e.g. Subsonic clients expect.
            * ``404`` — a plain 404 with cache headers.  The frontend
              track-list and player-bar IMG loaders pass this so the
              browser's image pipeline reports ``onerror`` and the
              format-specific emoji stays visible behind the IMG.
              Crucially, the IMG path keeps lazy-loading, image-priority
              queueing, and bitmap-level cache coalescing — all of which
              the alternative (fetch + blob + ObjectURL to read the
              X-SoniqBoom-Art header) silently loses, regressing
              folder-open latency from ~50 ms to several seconds on
              large directories of tagless tracks (regression D14).

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
        # No cached art yet → derive a freshness stamp from the source so
        # If-None-Match works on the first request.  Use the SAME folder-aware
        # mtime the negative cache uses (audio file + newest folder-art
        # candidate): otherwise a folder.jpg dropped in later wouldn't change
        # the ETag, the client's 60s revalidation would 304, and the newly
        # available art would never be fetched even though the server can now
        # resolve it.
        try:
            track = await get_track(track_id)
            if track and not track.path.startswith(
                ("http://", "https://"),
            ):
                mtime = await _source_mtime_for_async(track.path)
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

        # Nothing resolved.  Default: serve the cached placeholder
        # (strong ETag → one download covers every tagless track).
        # When fallback=404, return a 404 with cache headers so the
        # browser's IMG.onerror fires and the UI's format emoji stays
        # visible — without paying the bitmap-decode + blob-alloc cost
        # of fetching the placeholder body just to inspect a header.
        if fallback == "404":
            return _no_art_404(etag)
        return _placeholder_response()

    # --- Full size ----------------------------------------------------------
    # ``_resolve_full_art`` already caches full + thumbs and broadcasts
    # art_ready on a fresh extraction (see _persist_and_notify), so no extra
    # thumb task is needed here.
    full_data, mime = await _resolve_full_art(track_id)
    if full_data:
        return _etag_response(full_data, mime or "image/jpeg", etag)

    if fallback == "404":
        return _no_art_404(etag)
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


def _no_art_404(etag: str = "") -> Response:
    """Return a 404 with cache headers for the ``fallback=404`` path.

    The frontend track-list IMG loader uses this so the browser's
    ``onerror`` handler fires for tagless tracks while the format-
    appropriate emoji stays visible behind the IMG.  The 404 is cacheable
    so scrolling past the same track-id re-uses it without a round trip —
    BUT it must be REVALIDATABLE, not ``immutable``: for remote tracks the
    cover is filled in later (on play, or by the background backfill), and
    an ``immutable`` 404 would pin the empty result for a year so the list
    could never recover without a hard reload.  A short TTL plus the
    per-track ETag means a re-request after the art lands picks it up (the
    cached-art mtime changes the ETag, so the 304 short-circuit no longer
    fires and the real cover is served).
    """
    headers = {
        "Cache-Control": "public, max-age=60, must-revalidate",
        "X-SoniqBoom-Art": "none",
    }
    if etag:
        headers["ETag"] = etag
    return Response(status_code=404, headers=headers)
