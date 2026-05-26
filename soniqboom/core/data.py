# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Data layer — thin async wrapper over the in-memory TrackStore.

Every function delegates to TrackStore synchronous methods and returns
immediately.  The async signatures let callers use ``await`` uniformly
without caring that the backing store is in-process.
"""
from __future__ import annotations

import hashlib
import logging
import re

from soniqboom.core.store import get_store
from soniqboom.models.track import Track, TrackMeta

log = logging.getLogger(__name__)

UPSERT_BATCH = 1_000


# ── Index management ───────────────────────────────────────────────────────

async def rebuild_indexes() -> None:
    """Rebuild all indexes, yielding to the event loop every 500 tracks
    so HTTP requests are not blocked during large libraries.

    Wraps the work in ``enter_batch_mode`` / ``exit_batch_mode`` so
    the 9 sorted indexes (year, added_at, duration, bpm, title,
    artist, album_artist, album, format) are built by a single
    O(N log N) ``list.sort()`` at the end instead of N ×
    ``bisect.insort`` per track — which is O(N) per call (memmove
    on a contiguous list) and produces O(N²) wall-clock for the
    full rebuild.

    For a 270K-track library that's the difference between ~3 s
    and ~70 s — the "Rebuilding schema and scanning all folders…"
    stage that felt frozen before each ``/admin/reindex`` scan
    actually started.  ``store.rebuild_indexes`` (the sync version
    used at startup) already had this wrap; this brings the async
    path called by ``/admin/reindex`` in line.
    """
    import asyncio
    store = get_store()
    store.clear_indexes()
    store.enter_batch_mode()
    try:
        items = store.track_items_list()
        BATCH = 500
        for i in range(0, len(items), BATCH):
            store.index_tracks_batch(items[i : i + BATCH])
            await asyncio.sleep(0)
    finally:
        # exit_batch_mode triggers the single sort() of every sorted
        # index — this is where the win lands.  In a finally so a
        # task cancel mid-rebuild can't leave the store in batch mode
        # forever (which would silently break subsequent inserts).
        store.exit_batch_mode()
    store.finish_rebuild()


# ── Hash helpers ─────────────────────────────────────────────────────────────

def path_hash(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:16]


async def store_hash_lookup(value: str) -> str:
    return get_store().store_hash_lookup(value)


async def store_hash_lookups_batch(values: list[str]) -> dict[str, str]:
    if not values:
        return {}
    # hashlib releases the GIL during SHA-256, so threading helps
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, get_store().store_hash_lookups_batch, values,
    )


async def resolve_hash(h: str) -> str | None:
    return get_store().resolve_hash(h)


async def list_hash_lookups() -> dict[str, str]:
    return get_store().list_hash_lookups()


# ── Waveform helpers ─────────────────────────────────────────────────────────

async def store_waveform(track_id: str, amplitudes: list[float]) -> None:
    get_store().store_waveform(track_id, amplitudes)


async def get_waveform(track_id: str) -> list[float] | None:
    return get_store().get_waveform(track_id)


async def waveform_exists_batch(track_ids: list[str]) -> dict[str, bool]:
    if not track_ids:
        return {}
    return get_store().waveform_exists_batch(track_ids)


async def store_waveforms_batch(mapping: dict[str, list[float]]) -> None:
    if not mapping:
        return
    get_store().store_waveforms_batch(mapping)


# ── Playlist helpers ─────────────────────────────────────────────────────────

async def create_playlist(
    name: str | None = None,
    *,
    playlist_id: str | None = None,
    track_ids: list[str] | None = None,
    owner_user_id: str | None = None,
    # Back-compat shim: older callers used positional ``playlist_id, name``.
    _legacy_first_positional: str | None = None,
    _legacy_second_positional: str | None = None,
) -> dict:
    """Create a playlist.  New callers should pass ``name=`` as the only
    positional arg and supply ``owner_user_id`` so the playlist is
    private to that user.  Legacy callers using ``create_playlist(id, name)``
    still work via the back-compat alias."""
    if playlist_id is None:
        import uuid
        playlist_id = str(uuid.uuid4())
    return get_store().create_playlist(
        playlist_id, name or "New playlist",
        track_ids=track_ids, owner_user_id=owner_user_id,
    )


async def get_playlist(playlist_id: str) -> dict | None:
    return get_store().get_playlist(playlist_id)


async def list_playlists(user_id: str | None = None) -> list[dict]:
    """Return playlists visible to ``user_id`` (or all when None)."""
    return get_store().list_playlists_for_user(user_id)


async def update_playlist(playlist_id: str, updates: dict) -> dict | None:
    return get_store().update_playlist(playlist_id, updates)


async def delete_playlist(playlist_id: str) -> bool:
    return get_store().delete_playlist(playlist_id)


# ── Ratings ──────────────────────────────────────────────────────────────────

async def set_rating(track_id: str, rating: int) -> None:
    get_store().set_rating(track_id, rating)


async def get_rating(track_id: str) -> int:
    return get_store().get_rating(track_id)


async def get_ratings_batch(track_ids: list[str]) -> dict[str, int]:
    if not track_ids:
        return {}
    return get_store().get_ratings_batch(track_ids)


async def get_all_ratings() -> dict[str, int]:
    return get_store().get_all_ratings()


# ── Play stats ───────────────────────────────────────────────────────────────

async def record_play(track_id: str) -> dict:
    return get_store().record_play(track_id)


async def get_play_stats(track_id: str) -> dict:
    return get_store().get_play_stats(track_id)


async def get_play_stats_batch(track_ids: list[str]) -> dict[str, dict]:
    if not track_ids:
        return {}
    return get_store().get_play_stats_batch(track_ids)


async def get_all_play_stats() -> dict[str, dict]:
    return get_store().get_all_play_stats()


# ── Track CRUD ───────────────────────────────────────────────────────────────

async def upsert_track(track: Track) -> None:
    get_store().upsert_track(track.model_dump())


async def upsert_tracks_batch(tracks: list[Track]) -> int:
    if not tracks:
        return 0
    store = get_store()
    dicts = [t.model_dump() for t in tracks]
    for d in dicts:
        emb = d.get("embedding")
        if not emb or all(v == 0.0 for v in emb):
            d.pop("embedding", None)
    return store.upsert_tracks_batch(dicts)


async def get_track(track_id: str) -> Track | None:
    d = get_store().get_track(track_id)
    if not d:
        return None
    try:
        return Track(**d)
    except Exception:
        return None


async def get_tracks_batch(track_ids: list[str]) -> list[Track | None]:
    if not track_ids:
        return []
    store = get_store()
    results: list[Track | None] = []
    for d in store.get_tracks_batch(track_ids):
        if d:
            try:
                results.append(Track(**d))
            except Exception:
                results.append(None)
        else:
            results.append(None)
    return results


async def delete_track(track_id: str) -> bool:
    return get_store().delete_track(track_id)


async def track_count() -> int:
    return get_store().track_count()


async def scan_all_tracks_meta() -> list[TrackMeta]:
    store = get_store()
    metas: list[TrackMeta] = []
    for d in store.all_tracks():
        try:
            metas.append(TrackMeta(**{
                k: v for k, v in d.items()
                if k in TrackMeta.model_fields and k != "embedding"
            }))
        except Exception:
            continue
    return metas


async def get_track_ids_for_scan_root(root_path: str) -> set[str]:
    h = path_hash(root_path)
    return get_store().get_track_ids_for_scan_root(h)


async def delete_track_ids(track_ids: list[str]) -> int:
    """Remove tracks from the store AND their cached art from disk.

    The art cache has no time-based eviction, so without cleaning it here the
    thumbnails orphan forever whenever a track is removed (scanner orphan
    sweep, folder removal, junk purge, etc.).
    """
    if not track_ids:
        return 0
    deleted = get_store().delete_track_ids(track_ids)
    if deleted:
        # Fire-and-forget: freeing bytes isn't on the critical path.
        try:
            from soniqboom.core.art_cache import delete_art_batch
            _touched, freed = await delete_art_batch(track_ids)
            if freed:
                import logging
                logging.getLogger(__name__).info(
                    "Reclaimed %d bytes of art cache for %d deleted track(s)",
                    freed, deleted,
                )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Art-cache cleanup failed: %s", exc)
    return deleted


# ── Search ───────────────────────────────────────────────────────────────────

async def ft_search(
    query: str,
    limit: int = 50,
    offset: int = 0,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> list[TrackMeta]:
    """Search using the in-memory inverted index.

    Accepts tag-filter syntax like ``@artist_tag:{value}`` which is
    translated to tag-index lookups on the TrackStore.

    ``sort_by`` / ``sort_order`` are forwarded to
    :py:meth:`TrackStore.filter_tracks` to select which pre-computed sorted
    index drives the paginated walk.  Default (None) preserves the
    historical "newest first" ordering.
    """
    store = get_store()
    parsed = _parse_tag_query(query)
    hide_dups = bool(store.get_config("filter_duplicates", False))
    dicts = store.filter_tracks(**parsed, limit=limit, offset=offset,
                                filter_duplicates=hide_dups,
                                sort_by=sort_by, sort_order=sort_order)
    metas: list[TrackMeta] = []
    for d in dicts:
        try:
            metas.append(TrackMeta(**{
                k: v for k, v in d.items()
                if k in TrackMeta.model_fields and k != "embedding"
            }))
        except Exception:
            continue
    return metas


_TAG_RE = re.compile(r'@(\w+):\{([^}]*)\}')
_YEAR_RE = re.compile(r'@year:\[([^\]]+)\]')
_UNESCAPE_RE = re.compile(r'\\(.)')


def _parse_tag_query(query: str) -> dict:
    """Translate tag-filter query syntax to TrackStore.filter_tracks kwargs.

    Handles:
      @artist_tag:{value}        -> artist="value"
      @album_artist_tag:{value}  -> album_artist="value"
      @album_tag:{value}         -> album="value"
      @genre:{value}             -> genre="value"
      @format:{value}            -> format_="value"
      @dir_hash:{value}          -> dir_hash="value"
      @scan_root_hash:{value}    -> scan_root_hash="value"
      @year:[min max]            -> year_min=min, year_max=max
      *                          -> all tracks
      plain text                 -> query="text"
    """
    if not query or query.strip() == "*":
        return {}

    kwargs: dict = {}
    remaining = query

    _FIELD_MAP = {
        "artist_tag": "artist",
        "album_artist_tag": "album_artist",
        "album_tag": "album",
        "genre": "genre",
        "format": "format_",
        "dir_hash": "dir_hash",
        "scan_root_hash": "scan_root_hash",
    }

    for match in _TAG_RE.finditer(query):
        field, value = match.group(1), match.group(2)
        value = _UNESCAPE_RE.sub(r'\1', value)
        kwarg_name = _FIELD_MAP.get(field)
        if kwarg_name:
            kwargs[kwarg_name] = value
        remaining = remaining.replace(match.group(0), "")

    for match in _YEAR_RE.finditer(query):
        parts = match.group(1).split()
        if len(parts) == 2:
            lo, hi = parts
            # ``(`` / ``)`` mark exclusive bounds (RediSearch syntax).  The
            # previous code stripped the bracket but kept the value inclusive,
            # so ``>2020`` quietly matched 2020 itself.  Years are integers,
            # so bump by ±1 to convert exclusive → inclusive.
            if lo not in ("-inf", "("):
                try:
                    if lo.startswith("("):
                        kwargs["year_min"] = int(lo[1:]) + 1
                    else:
                        kwargs["year_min"] = int(lo)
                except ValueError:
                    pass
            if hi not in ("+inf", ")"):
                try:
                    if hi.endswith(")"):
                        kwargs["year_max"] = int(hi[:-1]) - 1
                    else:
                        kwargs["year_max"] = int(hi)
                except ValueError:
                    pass
        remaining = remaining.replace(match.group(0), "")

    text = _UNESCAPE_RE.sub(r'\1', remaining.strip())
    if text and text != "*":
        kwargs["query"] = text

    return kwargs


async def tracks_by_dir(dir_path: str, limit: int = 1000) -> list[TrackMeta]:
    h = path_hash(dir_path)
    store = get_store()
    dicts = store.filter_tracks(dir_hash=h, limit=limit)
    return [
        TrackMeta(**{k: v for k, v in d.items() if k in TrackMeta.model_fields and k != "embedding"})
        for d in dicts
    ]


async def tracks_by_scan_root(root_path: str, limit: int = 5000) -> list[TrackMeta]:
    h = path_hash(root_path)
    store = get_store()
    dicts = store.filter_tracks(scan_root_hash=h, limit=limit)
    return [
        TrackMeta(**{k: v for k, v in d.items() if k in TrackMeta.model_fields and k != "embedding"})
        for d in dicts
    ]


# ── Scan directory CRUD ──────────────────────────────────────────────────────

async def upsert_scan_dir(path: str, track_count_val: int | None = None,
                         network_share_id: str | None = None,
                         status: str = "ok") -> dict:
    store = get_store()
    store.store_hash_lookup(path)
    return store.upsert_scan_dir(path, track_count_val,
                                 network_share_id=network_share_id,
                                 status=status)


async def list_scan_dirs() -> list[dict]:
    return get_store().list_scan_dirs()


async def delete_scan_dir(path: str) -> bool:
    return get_store().delete_scan_dir(path)


async def delete_tracks_by_scan_root(root_path: str) -> int:
    """Delete every track under a scan root, plus its cached art."""
    h = path_hash(root_path)
    store = get_store()
    ids = list(store.get_track_ids_for_scan_root(h))
    if not ids:
        return 0
    # Route through the async wrapper so art-cache cleanup runs too.
    return await delete_track_ids(ids)


# ── Config helpers ───────────────────────────────────────────────────────────

async def set_config(key: str, value) -> None:
    get_store().set_config(key, value)


async def get_config(key: str, default=None):
    return get_store().get_config(key, default)
