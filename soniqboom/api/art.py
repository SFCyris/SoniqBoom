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

from soniqboom.core import art_cache, folder_art_index, folder_covers, gen_cover, igdb_cover, online_cover
from soniqboom.core.filesource import is_remote_path
from soniqboom.core.data import get_track, get_config
from soniqboom.core.metadata import cap_full_cover, id3_picture, resize_cover
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
# 1. **Shared folder-art cache** — when one track in a folder triggers a
#    folder-art fetch, the result is also cached under the folder's id
#    (``_folder_cache_key``: ``folder:{dir_hash}`` for a local folder, the
#    share root + folder for a network share) so the OTHER 999 tracks in that folder don't
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


def _folder_cache_key(path_str: str, dir_hash: "str | None") -> "str | None":
    """The cache id of the shared cover of ``path_str``'s folder.  A local
    folder: by its ``dir_hash`` (the hash of its absolute path — unique).  A
    network-share folder: by its ``core/folder_art_index`` key, which holds
    the share root — a share track's ``dir_hash`` hashes only the folder's
    path INSIDE the share, so two shares' ``/Music`` shared one cached image
    (and the first share's cover showed for the other's tracks) until 2.1.2.
    None when there is no key (no ``dir_hash`` for a local path)."""
    if path_str and is_remote_path(path_str):
        k = folder_art_index.key_for_path(path_str)
        if k:
            return f"{_FOLDER_ART_KEY_PREFIX}{k}"
        return None
    return _folder_art_cache_key(dir_hash) if dir_hash else None


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
        if ext in (".mp3", ".aiff", ".aif"):
            # AIFF's ID3 tag is a chunk of the IFF container — AIFF reads it.
            if ext == ".mp3":
                from mutagen.mp3 import MP3
                audio = MP3(path)
            else:
                from mutagen.aiff import AIFF
                audio = AIFF(path)
            pic = id3_picture(audio.tags)
            if pic is not None:
                return pic.data, pic.mime or "image/jpeg"

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

        elif ext in (".ogg", ".opus", ".oga"):
            from mutagen.oggvorbis import OggVorbis
            try:
                audio = OggVorbis(path)
            except Exception:
                from mutagen.oggopus import OggOpus
                audio = OggOpus(path)
            # (the same reader as the scan: front cover first, then COVERART)
            from soniqboom.core.metadata import vorbis_picture
            data, mime = vorbis_picture(audio.tags)
            if data:
                return data, mime

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

        elif ext in _COVER_VIA_SCAN:
            # Formats whose picture only the scan's extractor reads (ID3 in
            # WAV / DSD, ASF in WMA, APEv2 in Musepack / TTA …): read it the
            # same way, so the endpoint shows what the scan recorded — not a
            # folder image in its place.
            from soniqboom.core.metadata import extract
            uri = getattr(extract(path, "cover"), "cover_art", None)
            if uri and "," in uri:
                head, b64 = uri.split(",", 1)
                mime = head[5:].split(";", 1)[0] if head.startswith("data:") else "image/jpeg"
                return _b64.b64decode(b64), (mime or "image/jpeg")

    except Exception:
        if raise_on_error:
            raise
    return None, None


# (see ``_extract_cover``: tagged formats without their own branch there)
_COVER_VIA_SCAN = (".wav", ".wave", ".dsf", ".dff", ".wma", ".mpc", ".tta", ".ofr", ".tak")


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


def _folder_art_local_indexed(
    track_dir: Path, priority: list[str], dir_hash: str | None = None,
    gen: int | None = None,
) -> tuple[bytes, str] | tuple[None, None]:
    """In a worker thread: a local folder's image, using what the scan
    recorded (``core/folder_art_index``) — "no image" without a ``listdir``,
    a known image name opened directly.  A folder not recorded yet is listed
    here (one ``listdir``, case-insensitive names as ``_find_folder_art_local``)
    and the outcome recorded, so it is listed once, not once per track.  On a folder mounted from a NAS each listing
    is a network round trip — that is what this saves."""
    key = folder_art_index.key_for_local_dir(str(track_dir))
    entry = folder_art_index.lookup(key)
    if entry is not None:
        if entry.name is None:
            return None, None
        try:
            with open(track_dir / entry.name, "rb") as fh:
                data = fh.read()
            return data, ("image/png" if entry.name.lower().endswith(".png") else "image/jpeg")
        except OSError:
            pass                                    # gone or renamed: listed again below —
                                                    # recorded over the old entry, so the
                                                    # change is seen (``take_changed``)
    try:
        names = os.listdir(track_dir)
    except OSError:
        return None, None                           # not reachable: nothing recorded
    name = folder_art_index.pick(names, priority)
    mtime = None
    if name is not None:
        try:
            mtime = os.stat(track_dir / name).st_mtime
        except OSError:
            mtime = None
    folder_art_index.record(key, name, mtime=mtime, dir_hash=dir_hash, gen=gen)
    if name is None:
        return None, None
    try:
        with open(track_dir / name, "rb") as fh:
            data = fh.read()
    except OSError:
        return None, None
    return data, ("image/png" if name.lower().endswith(".png") else "image/jpeg")


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
    data, mime, _name, _listed = _find_folder_art_remote_named(
        remote_dir, source, priority, lane)
    return data, mime


def _find_folder_art_remote_named(
    remote_dir: str, source, priority: list[str], lane: str = "stream",
) -> tuple:
    """``_find_folder_art_remote`` that also says what it saw:
    ``(bytes, mime, name, listed)`` — ``name`` the image chosen (None: the
    folder holds none of ``priority``), ``listed`` whether the listing
    worked at all (False: nothing is known — never record "none" then).
    An image that will not download falls through to the next name; when
    none downloads, ``bytes`` is None with ``name`` still set."""
    try:
        entries = source.list_dir(remote_dir)
    except Exception:
        return None, None, None, False
    names = {e.name.lower(): e for e in entries if not e.is_dir}
    first = None
    for lname in priority:
        entry = names.get(lname)
        if entry is not None:
            first = first or entry.name
            try:
                data = source.read_file(entry.path, lane=lane)
                mime = "image/png" if lname.endswith(".png") else "image/jpeg"
                return data, mime, entry.name, True
            except Exception:
                continue
    return None, None, first, True


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


# ── G: "has this track's cover changed?" from what the scan recorded ────────
#
# A track without a cached cover is checked on every request: the "no cover"
# mark is honoured only while the file and its folder images are unchanged
# (``_is_art_absent_persisted``), and the ETag carries the same stamp.  That
# check used to be a live ``stat`` of the file plus a ``scandir`` of its
# folder per request — on a folder mounted from a NAS two network round trips
# for a row that has no cover.  The scan already knows both: the file's
# ``mtime`` (kept current by every scan and the folder watcher) and the
# folder's image with its ``mtime`` (``core/folder_art_index``).  So the
# stamp now comes from those; the live check still runs, in the background,
# at most once per folder per ``_VERIFY_EVERY_S`` (``_verify_later``) — a
# file re-tagged or a cover dropped into a folder the watcher does not see
# (a network mount) is picked up there and the row told (``art_ready``).
_UNKNOWN = object()
_VERIFY_EVERY_S = 300.0
_VERIFY_MAX = 20_000
_verified: dict[str, float] = {}                # folder key → time.monotonic()


def _known_source_mtime(track):
    """The cover stamp of a LOCAL ``track`` from stored values — the larger of
    its file's ``mtime`` and its folder image's — or ``_UNKNOWN`` (a network
    share, no stored mtime, or a folder the scan has not recorded): the
    caller checks live."""
    path = getattr(track, "path", "") or ""
    if not path or is_remote_path(path):
        return _UNKNOWN
    try:
        mt = float(getattr(track, "mtime", 0) or 0)
    except (TypeError, ValueError):
        mt = 0.0
    if mt <= 0:
        return _UNKNOWN
    entry = folder_art_index.lookup(folder_art_index.key_for_path(path))
    if entry is None:
        return _UNKNOWN
    if entry.name is not None and entry.mtime:
        mt = max(mt, float(entry.mtime))
    _verify_later(track)
    return mt


async def _source_stamp(track, *, urls: bool = True) -> float | None:
    """The cover stamp of ``track`` (see above): stored values when known,
    else the live ``_source_mtime_for``.  ``urls`` False: None for an
    ``http(s)://`` path (the cover endpoint's ETag never stamped those)."""
    path = getattr(track, "path", "") or ""
    if not urls and path.startswith(("http://", "https://")):
        return None
    known = _known_source_mtime(track)
    if known is not _UNKNOWN:
        return known
    return await _source_mtime_for_async(path)


def _verify_later(track) -> None:
    """Check ``track``'s file and folder live, in the background — at most
    once per folder per ``_VERIFY_EVERY_S`` (see above)."""
    key = folder_art_index.key_for_path(getattr(track, "path", "") or "")
    if not key:
        return
    now = time.monotonic()
    last = _verified.get(key)
    if last is not None and now - last < _VERIFY_EVERY_S:
        return
    if len(_verified) >= _VERIFY_MAX:
        _verified.clear()
    _verified[key] = now
    try:
        t = asyncio.get_running_loop().create_task(_verify(track))
    except RuntimeError:
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_resolve_done)


def _verify_sync(path: str, dir_hash: str | None, gen: int) -> float | None:
    """(worker thread) Re-record the track's folder from a live listing and
    return the file's live ``mtime`` (None: not reachable)."""
    try:
        live = Path(folder_art_index.member_free(path)).stat().st_mtime
    except OSError:
        return None
    d = Path(folder_art_index.local_dir_of(path))
    try:
        names = os.listdir(d)
    except OSError:
        return live
    name = folder_art_index.pick(names, folder_art_index.folder_image_names())
    mtime = None
    if name is not None:
        try:
            mtime = os.stat(d / name).st_mtime
        except OSError:
            mtime = None
    folder_art_index.record(folder_art_index.key_for_local_dir(str(d)), name,
                            mtime=mtime, dir_hash=dir_hash, gen=gen)
    return live


def _changed_since_marked(rows: list) -> list:
    """(worker thread) The ids among ``rows`` (store rows of one folder) that
    carry a "no cover" mark older than their file's live ``mtime`` — the
    file changed after it was found without a cover.  One ``stat`` per
    marked track (an unmarked one has a cached cover or none to look for)."""
    out = []
    store = get_store()
    for r in rows:
        tid, path = r.get("id"), r.get("path") or ""
        if not tid or not path or is_remote_path(path):
            continue
        if not (store.is_art_absent(tid) or _absent_sentinel_path(tid).exists()):
            continue
        try:
            live = Path(folder_art_index.member_free(path)).stat().st_mtime
        except OSError:
            continue
        if not _is_art_absent_persisted(tid, live):
            out.append(tid)
    return out


async def _verify(track) -> None:
    await asyncio.to_thread(_verify_sync, track.path,
                            getattr(track, "dir_hash", None) or None,
                            folder_art_index.generation())
    dir_hash = getattr(track, "dir_hash", None)
    try:
        rows = get_store().filter_tracks(dir_hash=dir_hash, limit=2000) if dir_hash else []
    except Exception:                               # noqa: BLE001
        rows = []
    if not rows:
        rows = [{"id": track.id, "path": track.path}]
    for tid in await asyncio.to_thread(_changed_since_marked, rows):
        # Changed since it was found without a cover (re-tagged on a network
        # mount the watcher does not see): forget the mark and look again.
        # (A file still without a cover is marked again with a newer time,
        # so it is not read again at the next check.)
        get_store().discard_art_absent(tid)
        _clear_art_absent_persisted(tid)
        _resolve_in_background(tid)
    await announce_folder_changes()


def _stale_folder_copies(key: str, rows: list, old: "bytes | None") -> list:
    """(worker thread) The ids among ``rows`` (the tracks of folder ``key``)
    whose cached cover is a copy of the folder's image: marked so
    (``art_cache.is_from_folder``), or unmarked but byte for byte the
    folder's old shared image ``old`` (copies cached before the marks).  The
    byte rule can also catch a file whose OWN embedded picture is that very
    image (rips that embed ``folder.jpg``): dropping its cached copy only
    means it is read from the file again — ``cover_art`` is never touched.
    A share's ``dir_hash`` is the same for every share's folder of that name:
    rows of another folder are left alone."""
    out = []
    for r in rows:
        tid, path = r.get("id"), r.get("path") or ""
        if not tid or folder_art_index.key_for_path(path) != key:
            continue
        if art_cache.is_from_folder(tid):
            out.append(tid)
            continue
        if old:
            try:
                p = art_cache._art_path(tid, "full")
                if p.exists() and p.stat().st_size == len(old) and p.read_bytes() == old:
                    out.append(tid)
            except OSError:
                continue
    return out


async def refresh_changed_folders() -> int:
    """Folders whose image was replaced or removed since the last call
    (``folder_art_index.take_changed``): ``refresh_folder`` for each.  A
    folder whose refresh fails is queued again (``folder_art_index.requeue``).
    Returns how many tracks were told."""
    changes = folder_art_index.take_changed()
    sent = 0
    for i, (key, dir_hash) in enumerate(changes):
        try:
            sent += await refresh_folder(key, dir_hash)
        except Exception:                           # noqa: BLE001
            log.debug("folder covers of %s not refreshed", key, exc_info=True)
            folder_art_index.requeue(changes[i:])
            break
    return sent


async def refresh_folder(key: str, dir_hash: str, old: "bytes | None" = None) -> int:
    """Folder ``key``'s image was replaced or removed: its shared cover and
    every track's copy of it (``_stale_folder_copies``) are dropped and
    ``art_ready`` is sent for those tracks, so they show the new image, or
    none — or, for a file that carries its own picture, that picture again
    (read from the file; its ``cover_art`` is never changed here).  ``old``:
    the folder's previous shared image, when the caller already replaced it
    (a folder job).  Before 2.1.2 such copies stayed until the art cache was
    cleared."""
    from soniqboom.api.library import _broadcast
    ids = [f"{_FOLDER_ART_KEY_PREFIX}{key}"] if folder_art_index.is_remote_key(key) else []
    ids.append(_folder_art_cache_key(dir_hash))          # (a share folder's pre-2.1.2 id too)
    if old is None:
        for cid in ids:
            old = old or await art_cache.get_art(cid, "full")
        await art_cache.delete_art_batch(ids)
    folder_covers.forget_folder(key)
    try:
        rows = get_store().filter_tracks(dir_hash=dir_hash, limit=_REFRESH_MAX_ROWS)
    except Exception:                               # noqa: BLE001
        rows = []
    stale = await asyncio.to_thread(_stale_folder_copies, key, rows, old)
    if stale:
        await art_cache.delete_art_batch(stale)
    sent = 0
    for tid in stale:
        get_store().discard_art_absent(tid)
        _clear_art_absent_persisted(tid)
        await _broadcast({"event": "art_ready", "track_id": tid})
        sent += 1
    return sent


# (a share's ``dir_hash`` is shared by every share's folder of that name:
# the rows are filtered by their real folder in ``_stale_folder_copies``)
_REFRESH_MAX_ROWS = 20_000


async def announce_folder_changes() -> int:
    """Send ``art_ready`` for the tracks of every folder that gained an image
    since the last call (``folder_art_index.take_flips``) — their list rows
    were told "no cover" and ask again.  At most 2000 tracks per call.  Also
    refreshes the folders whose image was replaced or removed
    (``refresh_changed_folders``)."""
    try:
        await refresh_changed_folders()
    except Exception:                               # noqa: BLE001
        log.debug("changed folder covers not refreshed", exc_info=True)
    hashes = folder_art_index.take_flips()
    if not hashes:
        return 0
    from soniqboom.api.library import _broadcast
    sent = 0
    for h in hashes:
        try:
            rows = get_store().filter_tracks(dir_hash=h, limit=2000 - sent)
        except Exception:                           # noqa: BLE001
            continue
        for r in rows:
            tid = r.get("id") if isinstance(r, dict) else getattr(r, "id", None)
            if not tid:
                continue
            # (both marks: the stored one is still "valid" when the image kept
            # an older date — a Finder / ``cp -p`` copy)
            get_store().discard_art_absent(tid)
            _clear_art_absent_persisted(tid)
            await _broadcast({"event": "art_ready", "track_id": tid})
            sent += 1
        if sent >= 2000:
            break
    return sent


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
        if is_remote_path(path_str):
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


async def cover_thumb(track_id: str, bucket: str, *, resolve: bool = True,
                      wait_folder: float = 0.0) -> bytes | None:
    """A track's cover as the ``bucket`` (``sm`` / ``lg``) JPEG thumbnail: the
    cached one, else made from the cached full cover — or, with ``resolve``,
    from the cover ``_resolve_full_art`` finds (the thumbnail is then cached).
    None when there is none (``resolve`` False: none cached)."""
    thumb = await art_cache.get_art(track_id, bucket)
    if thumb:
        return thumb
    if resolve:
        full, _mime = await _resolve_full_art(track_id, wait_folder=wait_folder)
    else:
        full = await art_cache.get_art(track_id, "full")
    if not full:
        return None
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, resize_cover, full, _SIZE_MAP[bucket])
    if data:
        t = loop.create_task(art_cache.store_art(track_id, data, bucket))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_art_write_done)
    return data or None


def _read_if_present(path: Path, read) -> tuple:
    """In a worker thread: ``(present, data, mime, failed)`` — whether
    ``path`` exists (on a network drive a stat is a round trip: never on the
    loop) and, when it does, ``read()``'s ``(data, mime)`` or whether it
    raised."""
    if not path.exists():
        return False, None, None, False
    try:
        data, mime = read()
    except Exception:
        return True, None, None, True
    return True, data, mime, False


# How long a caller that gets no ``art_ready`` (Subsonic, DLNA, cast) waits
# for a network share's folder image being fetched (``folder_covers.wait``).
FOLDER_WAIT_S = 3.0


async def _resolve_full_art(track_id: str, *, wait_folder: float = 0.0,
                            ) -> tuple[bytes, str] | tuple[None, None]:
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
    src_mtime = await _source_stamp(track)
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

    if is_remote_path(path_str):
        # Remote track — try to extract from cached local copy
        from soniqboom.core.remote_cache import get_cache
        from soniqboom.core.filesource import get_source, parse_remote_path
        scan_root, remote_path = parse_remote_path(path_str)
        if not remote_path:
            return None, None
        source = get_source(scan_root)
        cache = get_cache()
        try:
            from soniqboom.core.filesource import is_member_path, split_member
            if is_member_path(remote_path):
                # Composite remote-archive member — split the "::" tail FIRST so
                # we look up (and extract from) the OUTER archive, not the whole
                # "archive.zip::member" string (which is never a cache key, so the
                # embedded cover was silently never extracted).  The cover lives
                # inside the member, so extract it from that member within the
                # locally-cached archive.  Mirror the non-blocking get_cached
                # semantics of the plain-remote branch below (no forced fetch —
                # the is_remote backfill covers the not-yet-cached case).
                arc_rel, member = split_member(remote_path)
                local_path = cache.get_cached(scan_root, arc_rel)
                if local_path:
                    _p, data, mime, _f = await loop.run_in_executor(
                        None, _read_if_present, local_path,
                        lambda: _extract_cover_from_zip(f"{local_path}::{member}"))
            else:
                local_path = cache.get_cached(scan_root, remote_path)
                if local_path:
                    _p, data, mime, _f = await loop.run_in_executor(
                        None, _read_if_present, local_path, lambda: _extract_cover(local_path))
        except Exception:
            pass
    elif '::' in path_str:
        outer_zip = Path(path_str.split('::')[0])
        source_present, data, mime, read_failed = await loop.run_in_executor(
            None, _read_if_present, outer_zip,
            lambda: _extract_cover_from_zip(path_str, raise_on_error=True))
    else:
        path = Path(path_str)
        source_present, data, mime, read_failed = await loop.run_in_executor(
            None, _read_if_present, path, lambda: _extract_cover(path, raise_on_error=True))

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
    if use_folder_art and is_remote_path(path_str) and getattr(track, "cover_art", None):
        # The scan saw a picture IN the file of this share track: its own
        # cover is recovered (the backfill below) and the folder's image is
        # not put in its place — unless that recovery just found none.
        from soniqboom.core import art_backfill
        if not art_backfill.failed_recently(track_id):
            use_folder_art = False
    if use_folder_art:
        # Pass dir_hash so the SHARED folder-art cache can short-
        # circuit the list_dir + read_file round trip for every
        # other track in the same directory.
        folder_data, folder_mime = await _try_folder_art(
            path_str, loop, dir_hash=getattr(track, "dir_hash", None) or None,
            track=track,
        )
        if not folder_data and wait_folder > 0 and folder_covers.pending(track):
            # (a caller that cannot be told later: wait for the folder's job,
            # then take what it cached)
            await folder_covers.wait(track, wait_folder)
            folder_data, folder_mime = await _try_folder_art(
                path_str, loop, dir_hash=getattr(track, "dir_hash", None) or None,
                track=track,
            )
        if folder_data:
            _clear_art_absent_persisted(track_id)
            _persist_and_notify(track_id, folder_data, from_folder=True)
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
    is_remote = is_remote_path(path_str)
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
    *, dir_hash: str | None = None, track=None,
) -> tuple[bytes, str] | tuple[None, None]:
    """Attempt to find folder art for the track at *path_str*.

    What the scan recorded about the folder comes first
    (``core/folder_art_index``): a folder known to hold no image is answered
    without touching it, one known to hold ``cover.jpg`` is read without
    listing it.  A NETWORK-share folder that is not in the shared cache yet
    is never fetched here when ``track`` is given: its job is queued
    (``core/folder_covers``) and this returns nothing at once — the request
    answers "no cover yet" (not to be cached) and ``art_ready`` follows.
    Without ``track`` (no caller left that needs it, kept for safety) a
    network folder is still looked up in place.

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
    cache_key = _folder_cache_key(path_str, dir_hash)
    if cache_key:
        cached = await art_cache.get_art(cache_key, "full")
        if cached:
            mime = "image/png" if cached[:4] == b"\x89PNG" else "image/jpeg"
            return cached, mime

    csv = await get_config("folder_art_names", "")
    priority = _parse_folder_art_names(csv if isinstance(csv, str) else "")

    data: bytes | None = None
    mime: str | None = None

    if is_remote_path(path_str):
        # Remote path — directory listing via FileSource
        from soniqboom.core import catalogue
        if catalogue.is_catalogue_path(path_str):
            return None, None       # a catalogue's folders hold no art we may fetch
        entry = folder_art_index.lookup(folder_art_index.key_for_path(path_str))
        if entry is not None and entry.name is None:
            return None, None       # the scan saw this folder: it holds no image
        if track is not None:
            folder_covers.request(track)
            return None, None       # fetched in the background; art_ready follows
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

    else:
        # A local folder (an archive member: its archive's folder).
        track_dir = Path(folder_art_index.local_dir_of(path_str))
        data, mime = await loop.run_in_executor(
            None, _folder_art_local_indexed, track_dir, priority,
            dir_hash, folder_art_index.generation(),
        )

    # ── Populate shared dir cache for future hits in this folder ──────
    # ``store_art`` runs fire-and-forget; the response to THIS caller
    # doesn't wait for the write to land on disk.  Subsequent tracks
    # in the same dir benefit from the cache as soon as the write
    # completes (typically a few milliseconds later).
    if data and cache_key:
        try:
            async def _store_capped(bytes_=data, key=cache_key):
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


def _bg_art_write_done(t: "asyncio.Task") -> None:
    """Drop the strong ref and surface a failed background write (a full
    disk) in the log instead of asyncio's "exception was never retrieved"."""
    _bg_tasks.discard(t)
    if not t.cancelled() and t.exception() is not None:
        log.warning("Art cache write failed: %s", t.exception())


# Network-share covers copied in the background for a revalidated "no cover
# yet" answer (``_fill_remote_art_later``): track id → when, at most once per
# ``_BG_RESOLVE_EVERY_S`` per track (a list revalidates each row every 60 s).
_bg_resolved: dict[str, float] = {}
_BG_RESOLVE_EVERY_S = 60.0
_BG_RESOLVED_MAX = 20_000


async def _fill_remote_art_later(track_id: str, track) -> None:
    """For a revalidated "no cover yet" of network-share ``track``: start the
    embedded cover's backfill, or — when its folder's cover is already in the
    art cache — copy that one in the background (``_resolve_full_art``)."""
    if getattr(track, "cover_art", None):
        from soniqboom.core.art_backfill import request_backfill
        request_backfill(track)                 # (coalesced; backs off after a failure)
        return
    key = _folder_cache_key(track.path, getattr(track, "dir_hash", None))
    if key and await art_cache.get_art(key, "full"):
        _resolve_in_background(track_id)
        return
    if not _use_folder_art_now():
        return
    entry = folder_art_index.lookup(folder_art_index.key_for_path(track.path))
    if entry is None or entry.name is not None:
        folder_covers.request(track)            # (one job per folder; art_ready follows)


def _resolve_in_background(track_id: str) -> None:
    """``_resolve_full_art`` off the request: it caches what it finds and
    sends ``art_ready``."""
    now = time.monotonic()
    last = _bg_resolved.get(track_id)
    if last is not None and now - last < _BG_RESOLVE_EVERY_S:
        return
    if len(_bg_resolved) >= _BG_RESOLVED_MAX:
        for k in [k for k, v in _bg_resolved.items() if now - v >= _BG_RESOLVE_EVERY_S]:
            del _bg_resolved[k]
        if len(_bg_resolved) >= _BG_RESOLVED_MAX:
            _bg_resolved.clear()
    _bg_resolved[track_id] = now
    try:
        t = asyncio.get_running_loop().create_task(_resolve_full_art(track_id))
    except RuntimeError:
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_resolve_done)


def _bg_resolve_done(t: "asyncio.Task") -> None:
    _bg_tasks.discard(t)
    if not t.cancelled() and t.exception() is not None:
        log.debug("Background cover lookup failed: %s", t.exception())


def _persist_and_notify(track_id: str, data: bytes, *, from_folder: bool = False) -> None:
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
            # Where it came from (``art_cache.mark_from_folder``) — noted
            # BEFORE the copy is written, so a stop in between leaves no
            # unmarked copy: a copy of the folder's image goes when that image
            # changes; the file's own picture stays.
            await loop.run_in_executor(
                None, art_cache.mark_from_folder if from_folder else art_cache.unmark_from_folder,
                track_id)
            await art_cache.store_art(track_id, full, "full")
            await _generate_and_cache_thumbs(track_id, full)
            if not from_folder:
                # (``cover_art`` says "the file carries a picture" — what the
                # scan recorded.  A copy of the folder's image must not claim
                # it: a share track would then wait for an embedded cover that
                # is not there, and its folder could never be told apart.)
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


# ── F: which list rows can skip their cover request ─────────────────────────
#
# A list window asks one cover per row.  Over HTTP/1.1 a browser runs about
# six requests at once, so 30 rows of tracks WITHOUT a cover (most of a
# retro-music library) queue in front of the rows that have one — and each
# "no cover" answer is asked again every minute.  The web app now sends the
# ids of its rows that carry no embedded cover in ONE request; the ids
# answered here as known to have no cover at all are not requested as
# images.  Rows with an embedded cover keep the plain image request: the
# browser's own cache serves those without any request.
#
# "Known" is deliberately narrow — anything that might still produce a
# cover is not listed, and the row asks as before:
#   * no embedded cover (the scan saw none) and none cached for the track;
#   * its folder recorded as holding no image (``core/folder_art_index``),
#     or folder art switched off — a folder not recorded yet is not known;
#   * no folder job under way (``core/folder_covers``);
#   * no online cover, and none that may still come: the track is not looked
#     up online, or its "none" is a lasting one, or lookups cannot run.
# A row told "no cover" asks again when ``art_ready`` (its cover appeared —
# also sent for a folder that gains an image, ``announce_folder_changes``)
# or ``art_refresh`` (a setting changed) arrives.
_STATUS_MAX_IDS = 300
# Ogg Vorbis / Opus: scans before 2.1.2 did not record their pictures, and
# a file not changed since is not read again — so an empty ``cover_art`` on
# such a row proves nothing.  It is answered "no cover" only once the file
# itself was read without one (its "no cover" mark, ``_absent_sentinel_path``
# or the in-memory one); until then the row asks its image as before.
_SCAN_BLIND_COVER = (".ogg", ".opus", ".oga")


def _no_cover_candidates(ids: "list[str]", use_folder: bool) -> "list[str]":
    """(worker thread — the ``art_exists`` checks stat the cache folder)
    The ids whose OWN cover is known to be missing (all but the online
    check)."""
    store = get_store()
    out = []
    for tid in ids:
        d = store.get_track(tid)
        if not d or d.get("cover_art"):
            continue
        path = d.get("path") or ""
        if (folder_art_index.member_free(path).lower().endswith(_SCAN_BLIND_COVER)
                or path.lower().endswith(_SCAN_BLIND_COVER)) and not (
                    store.is_art_absent(tid) or _absent_sentinel_path(tid).exists()):
            continue
        if art_cache.art_exists(tid, "sm") or art_cache.art_exists(tid, "full"):
            continue
        if use_folder:
            key = _folder_cache_key(path, d.get("dir_hash"))
            if key and art_cache.art_exists(key, "full"):
                continue
            entry = folder_art_index.lookup(folder_art_index.key_for_path(path))
            if entry is None or entry.name is not None:
                continue
        out.append(tid)
    return out


@router.post("/status")
async def art_status(request: Request):
    """``{"ids": [...]}`` → ``{"none": [...]}``: the ids (at most 300) known
    to have no cover at all — their rows need not ask (see above)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Expected JSON")
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        raise HTTPException(400, "Expected ids")
    ids = [str(i) for i in ids[:_STATUS_MAX_IDS] if isinstance(i, str) and i]
    use_folder = _use_folder_art_now()
    candidates = await asyncio.to_thread(_no_cover_candidates, ids, use_folder)
    none: list[str] = []
    lookups_off = online_cover.lookups_off()
    generated = gen_cover.enabled_now()
    for tid in candidates:
        track = await get_track(tid)
        if track is None or folder_covers.pending(track):
            continue
        if generated and gen_cover.applies(track):
            continue                        # a generated cover (or a box cover) is always there
        if igdb_cover.configured():
            box_sub, box = await igdb_cover.stored(track)
            if (box and box.get("ok")) or igdb_cover.pending(box_sub):
                continue                    # a game box cover is there or on its way
        online = await online_cover.state(track)
        if online.hit or (online.open and not lookups_off):
            continue                        # an online cover is there or may come
        none.append(tid)
        # A local folder's "no image" is checked live in the background (at
        # most once per folder per ``_VERIFY_EVERY_S``): a cover dropped in
        # later — the folder watcher does not watch pictures — reaches these
        # rows by ``art_ready`` (``announce_folder_changes``).
        if not is_remote_path(track.path):
            _verify_later(track)
    return {"none": none}


@router.get("/{track_id}/{size}.jpg")
async def cover_art_jpg(
    track_id: str,
    size: str,
    request: Request,
    fallback: str = Query("placeholder", pattern="^(placeholder|none|404)$"),
):
    """Cover art at ``/api/art/{id}/{size}.jpg`` (``size``: ``sm``, ``lg`` or
    ``full``) — the same answer as ``/api/art/{id}?size=…``.  The body is
    whatever the cover is (JPEG, PNG …) with its own ``Content-Type``."""
    # The web app's form.  Security tools that watch a reverse proxy's access
    # log (CrowdSec ``http-crawl-non_statics``, ``http-probing``) skip requests
    # for static files by their extension; a fast scroll through a big list
    # asks for dozens of covers a second (GitHub #40).
    if size not in ("sm", "lg", "full"):
        raise HTTPException(404, "Unknown size")
    return await cover_art(track_id, request, size=size, fallback=fallback)


@router.get("/{track_id}")
async def cover_art(
    track_id: str,
    request: Request,
    size: str = Query("sm", pattern="^(sm|lg|full)$"),
    fallback: str = Query("placeholder", pattern="^(placeholder|none|404)$"),
):
    """Serve cover art with optional thumbnail sizing and ETag caching.

    Query params:
        size — ``sm`` (200px, default), ``lg`` (550px), or ``full`` (original).
        fallback — what to return when no real art exists:
            * ``placeholder`` (default) — a tiny grey JPEG with the
              ``X-SoniqBoom-Art: placeholder`` header.  Cheaply cacheable
              by ETag so every tagless track in a library shares one
              download.  This is what e.g. Subsonic clients expect.
            * ``none`` (``404``, its old name, is the same) — a body-less
              ``204`` with cache headers (``_no_art_response``).  The
              frontend IMG loaders pass this so the browser's image
              pipeline reports ``onerror`` and the format-specific emoji
              stays visible behind the IMG.
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
    track = None
    online = online_cover.NOTHING
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
            if track:
                mtime = await _source_stamp(track, urls=False)
        except OSError:
            mtime = None
        except Exception:
            mtime = None
        if track is not None:
            online = await online_cover.state(track)
    # Retro tracks: a stored game box cover (core/igdb_cover.py) and the
    # generated cover (core/gen_cover.py) stand in after the online one.
    gen = await gen_cover.spec_if_on(track) if track is not None else None
    box_sub, box = await igdb_cover.stored(track) if track is not None else (None, None)
    etag = _make_etag(track_id, size, mtime)
    # The answer that carries an online cover (``_online_response``) has its
    # own tag; the track's own cover and "no cover" keep ``etag``.
    etag_online = online_cover.etag(etag, online)

    # --- ETag: return 304 if client already has this version ----------------
    # A client holding the "no cover" answer while an online cover is stored
    # is not current (``etag`` then differs from ``etag_online``).  One holding
    # the online cover is current only while the track's own cover is known to
    # be missing — a setting change (Use Folder Art, the image names) can give
    # the track a cover without changing ``etag``; the request then resolves
    # again below.
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and track is not None and (gen is not None or box_sub is not None):
        stood_in = await _stand_in_304(if_none_match, track_id, track, etag, gen, box_sub, box, mtime,
                                       online)
        if stood_in is not None:
            return stood_in
    if if_none_match and if_none_match == etag_online and (
            not online.hit or _own_cover_missing(track_id, track, mtime)) and not (
            # (a retro track's stand-in replaces "no cover": a client holding
            # that older answer gets the picture, not a 304)
            not online.hit and track is not None
            and (gen is not None or _box_shown(track_id, track, box, mtime))):
        etag = etag_online
        headers = {"ETag": etag}
        if online.hit:
            headers["Cache-Control"] = _ONLINE_CC       # the online cover still stands
        elif track is not None:
            # The client holds "no cover": the online lookup must be able to
            # start here too, or a list that got the empty answer before the
            # lookup could run (a restart, a pause) never gets the cover.
            # While it is under way the answer is asked again at every use
            # (see ``_no_art_response``); afterwards it is kept as usual.
            if _ask_online(online, track_id, track, mtime):
                headers["Cache-Control"] = _LOOKUP_PENDING_CC
            elif _own_cover_missing(track_id, track, mtime):
                headers["Cache-Control"] = _NO_ART_CC
        if mtime is None and track is not None and is_remote_path(track.path):
            # The client holds "no cover yet" for a network-share track, and
            # this ETag stays the same until its cover is cached — so what
            # would cache it must still start here, or a list that once got
            # the empty answer never gets the cover: the embedded cover's
            # backfill (when the scan saw one; it sends ``art_ready``), or the
            # folder's cover already cached for another of its tracks.  Never
            # a share listing per revalidation.
            await _fill_remote_art_later(track_id, track)
            if folder_covers.pending(track):
                headers["Cache-Control"] = _LOOKUP_PENDING_CC
        return Response(status_code=304, headers=headers)

    # --- Thumbnails (sm / lg) ----------------------------------------------
    if size in _SIZE_MAP:
        # Check dedicated thumbnail cache first
        thumb = await art_cache.get_art(track_id, size)
        if thumb:
            return _etag_response(thumb, "image/jpeg", etag)

        # Fallback: get full art, resize, cache, and serve
        full_data, mime = await _resolve_full_art(track_id)
        if full_data:
            # Resize only the bucket this request asked for (the other one is
            # made when it is asked for) and store it off the request path.
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, resize_cover, full_data, _SIZE_MAP[size])
            t = loop.create_task(art_cache.store_art(track_id, data, size))
            _bg_tasks.add(t)
            t.add_done_callback(_bg_art_write_done)
            return _etag_response(data, "image/jpeg", etag)

        # Nothing resolved.  Default: serve the cached placeholder
        # (strong ETag → one download covers every tagless track).
        # When fallback=404, return a 404 with cache headers so the
        # browser's IMG.onerror fires and the UI's format emoji stays
        # visible — without paying the bitmap-decode + blob-alloc cost
        # of fetching the placeholder body just to inspect a header.
        found, pending = await _online_response(online, track_id, track, size, etag_online, mtime)
        if found is not None:
            return found
        pending = pending or (track is not None and (folder_covers.pending(track)
                                             or _backfill_pending(track_id)))
        stand_in = await _stand_in(track_id, track, size, etag, gen, box_sub, box, mtime, pending)
        if stand_in is not None:
            return stand_in
        if fallback != "placeholder":
            return _no_art_response(etag, pending=pending)
        return _placeholder_response(_placeholder_cc(online, pending))

    # --- Full size ----------------------------------------------------------
    # ``_resolve_full_art`` already caches full + thumbs and broadcasts
    # art_ready on a fresh extraction (see _persist_and_notify), so no extra
    # thumb task is needed here.
    full_data, mime = await _resolve_full_art(track_id)
    if full_data:
        return _etag_response(full_data, mime or "image/jpeg", etag)

    found, pending = await _online_response(online, track_id, track, size, etag_online, mtime)
    if found is not None:
        return found
    pending = pending or (track is not None and (folder_covers.pending(track)
                                         or _backfill_pending(track_id)))
    stand_in = await _stand_in(track_id, track, size, etag, gen, box_sub, box, mtime, pending)
    if stand_in is not None:
        return stand_in
    if fallback != "placeholder":
        return _no_art_response(etag, pending=pending)
    return _placeholder_response(_placeholder_cc(online, pending))


@router.post("/{track_id}/intent")
async def art_intent(track_id: str):
    """The web player started ``track_id``, or its Track Info opened: look
    up its game's box cover (core/igdb_cover.py) when IGDB is set up and
    nothing is known yet — the only way such a lookup starts (never a list
    row).  ``{"queued": bool}``; ``art_ready`` follows a cover."""
    if len(track_id) > 256:
        raise HTTPException(404, "Unknown track")
    track = await get_track(track_id)
    if track is None:
        raise HTTPException(404, "Unknown track")
    return {"queued": await igdb_cover.on_intent(track, track_id)}


# ── Stand-in covers for retro tracks ─────────────────────────────────────────
# After the track's own cover and the online one: a stored game box cover
# (core/igdb_cover.py, only for a track known to have no cover of its own,
# like the online cover), else the generated cover (core/gen_cover.py), which
# stands in like the format emoji did — so also while the track's own cover
# is still on its way (then asked again at every use: ``_LOOKUP_PENDING_CC``).
# Both answers have their own ETag (``gen_cover.etag`` / ``igdb_cover.etag``
# on the track's tag), so a cover the track gains changes the tag.
_GENERATED_CC = "public, max-age=60, must-revalidate"


def _box_shown(track_id: str, track, box, source_mtime: float | None) -> bool:
    """Is the stored box cover the one to show — the track known to have no
    cover of its own (an online-collection tune: none in the file)?"""
    if not (box and box.get("ok")):
        return False
    if _own_cover_missing(track_id, track, source_mtime):
        return True
    try:
        from soniqboom.core import catalogue
        return catalogue.is_catalogue_path(track.path) and not getattr(track, "cover_art", None)
    except Exception:                               # noqa: BLE001
        return False


async def _stand_in(track_id: str, track, size: str, etag: str, gen, box_sub, box,
                    source_mtime: float | None, pending: bool) -> Response | None:
    """The box cover or the generated cover as a response, or None."""
    if track is None:
        return None
    if _box_shown(track_id, track, box, source_mtime):
        data = await igdb_cover.image(track, size)
        if data:
            return Response(content=data, media_type="image/jpeg", headers={
                "Cache-Control": _ONLINE_CC,
                "ETag": igdb_cover.etag(etag, box),
                "X-SoniqBoom-Art": "boxart",
            })
    if gen is None:
        return None
    data = await gen_cover.picture(gen, size)
    if not data:
        return None
    pending = pending or igdb_cover.pending(box_sub)
    return Response(content=data, media_type="image/jpeg", headers={
        "Cache-Control": _LOOKUP_PENDING_CC if pending else _GENERATED_CC,
        "ETag": gen_cover.etag(etag, gen),
        "X-SoniqBoom-Art": "generated",
    })


async def _stand_in_304(if_none_match: str, track_id: str, track, etag: str, gen, box_sub, box,
                        source_mtime: float | None, online) -> Response | None:
    """A 304 for a client that holds the stand-in still due, else None."""
    if _box_shown(track_id, track, box, source_mtime):
        if if_none_match == igdb_cover.etag(etag, box):
            return Response(status_code=304, headers={"ETag": if_none_match,
                                                      "Cache-Control": _ONLINE_CC})
        return None
    if gen is None or online.hit or if_none_match != gen_cover.etag(etag, gen):
        return None
    headers = {"ETag": if_none_match, "Cache-Control": _GENERATED_CC}
    if source_mtime is None and is_remote_path(track.path):
        await _fill_remote_art_later(track_id, track)   # (as for "no cover yet", below)
    if folder_covers.pending(track) or _backfill_pending(track_id) or igdb_cover.pending(box_sub):
        headers["Cache-Control"] = _LOOKUP_PENDING_CC
    return Response(status_code=304, headers=headers)


# ── Online covers (core/online_cover.py) ─────────────────────────────────────
# The fallback for a track with no cover of its own.  Served from the online
# store only: the track's record and its art-cache entry are never written, so
# a cover the track gains later takes over through the usual path.

def _own_cover_missing(track_id: str, track, source_mtime: float | None = None) -> bool:
    """Is ``track`` known to have no cover of its own?  A local file: it was
    read cleanly without one (the "no art" mark — in memory, or the stored one
    still valid for ``source_mtime``); a file that could not be read is not
    known.  A network-share file: the scan saw no picture in it AND its
    folder holds none (or could not be read just now) — see below."""
    try:
        if track is None:
            return False
        if is_remote_path(track.path):
            # No embedded cover (the scan saw none) AND no folder image: the
            # folder is known to hold none (``core/folder_art_index``), folder
            # art is switched off, or the folder could not be read just now
            # (``folder_covers.unreachable`` — the online cover stands in
            # until the share answers).  A folder not known yet is fetched
            # first (``core/folder_covers``) — an online cover must not race
            # the share's own image.
            if getattr(track, "cover_art", None):
                return False
            if not _use_folder_art_now():
                return True
            entry = folder_art_index.lookup(folder_art_index.key_for_path(track.path))
            if entry is not None:
                return entry.name is None
            return folder_covers.unreachable(track)
        return bool(get_store().is_art_absent(track_id)
                    or (source_mtime is not None
                        and _is_art_absent_persisted(track_id, source_mtime)))
    except Exception:
        return False


def _backfill_pending(track_id: str) -> bool:
    """Is the embedded cover of ``track_id`` being recovered from its share?"""
    try:
        from soniqboom.core import art_backfill
        return art_backfill.pending(track_id)
    except Exception:                               # noqa: BLE001
        return False


def _use_folder_art_now() -> bool:
    """The "Use folder art" setting, read synchronously (default on)."""
    try:
        return bool(get_store().get_config("use_folder_art", True))
    except Exception:                               # noqa: BLE001
        return True


async def ask_online_for(track_id: str) -> bool:
    """Queue the online lookup for ``track_id`` now that its own cover is
    known to be missing (a folder job found no image) — see ``_ask_online``."""
    track = await get_track(track_id)
    if not track:
        return False
    online = await online_cover.state(track)
    return _ask_online(online, track_id, track)


def _ask_online(online, track_id: str, track, source_mtime: float | None = None) -> bool:
    """Queue the online lookup for ``track`` when its answer is not known and
    its own cover is known to be missing (``_own_cover_missing``).  True when
    a lookup is queued or running (``art_ready`` follows a cover)."""
    try:
        if online.subject is None or online.known:
            return False
        if not _own_cover_missing(track_id, track, source_mtime):
            return False
        return online_cover.request(online, track_id)
    except Exception:
        log.debug("online cover request failed for %s", track_id, exc_info=True)
        return False


async def _online_response(online, track_id: str, track, size: str, etag: str,
                           source_mtime: float | None) -> tuple[Response | None, bool]:
    """``(the stored online cover of a track whose own cover resolved to
    nothing, as a response — None when there is none, whether its lookup is
    then queued or running — see ``_ask_online``)``.  Only for a track known
    to have no cover of its own (``_own_cover_missing``): one whose cover is
    merely out of reach (its folder offline, a read error) gets none."""
    if track is None or not _own_cover_missing(track_id, track, source_mtime):
        return None, False
    data = await online_cover.image(online, size)
    if data:
        return Response(content=data, media_type="image/jpeg", headers={
            "Cache-Control": _ONLINE_CC,
            "ETag": etag,
            "X-SoniqBoom-Art": "online",
        }), False
    return None, _ask_online(online, track_id, track, source_mtime)


# Cache rules of the two "no cover" answers.  While an online lookup is queued
# or running the answer is about to change, so it is asked again at every use
# (a conditional request): a cover found a moment later then reaches every
# place that asks the same URL — the player bar, the album grid — not only the
# pictures ``art_ready`` re-asks.
#
# The online cover itself is kept but checked at every page load (a 304 when
# it still stands): the track's own cover takes over as soon as it has one, and
# the cover is gone at the next load once the lookup is switched off — also in
# a browser that was closed when ``art_refresh`` was sent.
_ONLINE_CC = "no-cache"
_NO_ART_CC = "public, max-age=60, must-revalidate"
_LOOKUP_PENDING_CC = "no-cache"
_PLACEHOLDER_CC = "public, max-age=31536000, immutable"
_PLACEHOLDER_LOOKUP_CC = "max-age=3600"


def _placeholder_cc(online, pending: bool) -> str:
    """The placeholder picture's cache rule for a track in ``online`` state: a
    track that is looked up online can still gain a cover (a lookup under way,
    or a later one), so its placeholder is not kept for good."""
    if pending:
        return _LOOKUP_PENDING_CC
    if online.subject is not None:
        return _PLACEHOLDER_LOOKUP_CC
    return _PLACEHOLDER_CC


async def online_cover_for(track_id: str, size: str = "full") -> tuple[bytes | None, bool]:
    """For the other cover endpoints (Subsonic, cast), AFTER ``_resolve_full_art``
    found nothing: ``(the stored online cover as JPEG bytes — ``sm`` / ``lg`` /
    ``full`` — or None, can one still turn up soon)``.  The second is True
    while the track's answer is not known — a lookup queued or running, or
    one that has to wait (the lookups paused) — or is a short-lived "none":
    the caller must not remember the answer for long then.  Like
    ``_online_response``, only for a track known to have no cover of its own;
    a retro track gets its game box cover the same way, else its generated
    cover (the first part is then never None while those are on)."""
    try:
        track = await get_track(track_id)
        if not track:
            return None, False
        # A retro track's stand-in: its stored game box cover (own cover known
        # missing), else its generated cover — also while its own cover is
        # still on its way (then with "may come", so it is not kept long).
        gen = await gen_cover.spec_if_on(track)
        if folder_covers.pending(track) or _backfill_pending(track_id):
            # (the share's own or folder image is on its way)
            return (await gen_cover.picture(gen, size) if gen else None), True
        online = await online_cover.state(track)
        box_sub, box = await igdb_cover.stored(track)
        if _box_shown(track_id, track, box, None):
            data = await igdb_cover.image(track, size)
            if data:
                return data, False
        if not _own_cover_missing(track_id, track):       # (the mark ``_resolve_full_art`` just set)
            # (unread or unreadable: the stand-in is not kept long)
            return (await gen_cover.picture(gen, size), True) if gen else (None, False)
        data = await online_cover.image(online, size)
        if data:
            return data, False
        _ask_online(online, track_id, track)
        may_come = (online.open and not online_cover.lookups_off()) or igdb_cover.pending(box_sub)
        return (await gen_cover.picture(gen, size) if gen else None), may_come
    except Exception:
        log.debug("online cover failed for %s", track_id, exc_info=True)
        return None, False


def _placeholder_response(cache_control: str = _PLACEHOLDER_CC) -> Response:
    """Return the pre-baked placeholder JPEG with strong cache headers.

    ETag is a constant (placeholder bytes don't change between requests)
    so the browser keeps re-using its first download forever — a single
    HTTP round trip covers every tagless track in the library.
    Immutable + 1-year max-age guarantees no revalidation.  A track that is
    looked up online gets a shorter rule (``_placeholder_cc``).
    """
    return Response(
        content=_PLACEHOLDER_JPEG,
        media_type="image/jpeg",
        headers={
            "ETag": _PLACEHOLDER_ETAG,
            "Cache-Control": cache_control,
            # Hint to the UI's "is this real art?" check.  Frontend can
            # treat tracks served this header as art-less and avoid
            # showing a "View full size" affordance, etc.
            "X-SoniqBoom-Art": "placeholder",
        },
    )


def _no_art_response(etag: str = "", *, pending: bool = False) -> Response:
    """``204 No Content`` with cache headers for the ``fallback=none`` path.

    The frontend IMG loaders use this so the browser's ``onerror`` handler
    fires for tagless tracks while the format-appropriate emoji stays
    visible behind the IMG — a 204 fires ``error`` exactly like the 404 this
    used to be (Chromium, WebKit, Firefox), without counting as an error:
    reverse-proxy security tools (CrowdSec ``http-probing``, fail2ban) ban a
    client after a burst of 404s, which scrolling past art-less tracks was
    (GitHub #40).  It is cacheable
    so scrolling past the same track-id re-uses it without a round trip —
    BUT it must be REVALIDATABLE, not ``immutable``: for remote tracks the
    cover is filled in later (on play, or by the background backfill), and
    an ``immutable`` 404 would pin the empty result for a year so the list
    could never recover without a hard reload.  A short TTL plus the
    per-track ETag means a re-request after the art lands picks it up (the
    cached-art mtime changes the ETag, so the 304 short-circuit no longer
    fires and the real cover is served).  ``pending``: an online lookup for
    the track is queued or running — the answer is then asked again at every
    use instead of being kept for the minute.
    """
    headers = {
        "Cache-Control": _LOOKUP_PENDING_CC if pending else _NO_ART_CC,
        "X-SoniqBoom-Art": "none",
    }
    if etag:
        headers["ETag"] = etag
    return Response(status_code=204, headers=headers)
