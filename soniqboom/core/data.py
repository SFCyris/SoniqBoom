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

# Serialize index rebuilds (the background integrity sweep + both /reindex
# endpoints) so two atomic shadow-swaps can't interleave — the call that
# snapshotted earlier would otherwise swap last and regress the live indexes to
# a stale snapshot.  Created lazily per running event loop (one in the server;
# tests may spin up several).
_rebuild_lock = None
_rebuild_lock_loop = None


def _rebuild_lock_for_loop():
    import asyncio
    global _rebuild_lock, _rebuild_lock_loop
    loop = asyncio.get_running_loop()
    if _rebuild_lock is None or _rebuild_lock_loop is not loop:
        _rebuild_lock = asyncio.Lock()
        _rebuild_lock_loop = loop
    return _rebuild_lock


async def rebuild_indexes() -> dict:
    """Rebuild all indexes atomically, yielding to the event loop every 500
    tracks so HTTP requests are not blocked during large libraries AND so a
    concurrent query never sees a half-built index.

    Builds the new indexes on a SHADOW ``TrackStore`` that owns a consistent
    snapshot of ``_tracks``; the live store keeps serving its old, COMPLETE
    indexes for the entire rebuild.  The 11 sorted indexes are built by a
    single O(N log N) ``sort()`` at the shadow's ``exit_batch_mode`` (~3 s for
    270K, not ~70 s of per-track ``bisect.insort``).  The final swap is one
    synchronous block (NO ``await`` between assignments), so a reader only ever
    observes the old-complete or the new-complete index — never empty.

    This replaces the previous clear-then-refill approach, which called
    ``store.clear_indexes()`` (emptying ``_tag_format`` etc.) and refilled
    across ~540 ``await`` yields: any filter query landing in that window
    returned 0 (e.g. ``GET /tracks?format=Ken's AdLib`` right after a reindex,
    while the Galaxy legend still showed the cached "41").

    Cost: peak memory roughly doubles the index footprint transiently (shadow
    + live held together until the swap + GC).  Concurrent ``_tracks`` mutation
    during the build is excluded by the snapshot (rare — reindex starts scans
    only AFTER this returns; ``record_play``/``set_rating`` don't touch these
    indexes) and self-corrects on the next rebuild.

    Returns the pre-heal drift report from ``store._diff_indexes``
    (``{index_ok, mismatches, indexes, ...}``) so reindex endpoints and the
    background integrity sweep can detect-and-report, not just blindly cure.
    """
    import asyncio
    from soniqboom.core.store import TrackStore, INDEX_ATTRS
    store = get_store()

    async with _rebuild_lock_for_loop():
        shadow = TrackStore()
        shadow._tracks = dict(store._tracks)        # consistent snapshot for the build
        # _index_track derives _unplayed_ids from membership in _play_stats, so the
        # shadow MUST see the real play stats or it would mark every track unplayed.
        shadow._play_stats = dict(store._play_stats)
        shadow.enter_batch_mode()
        items = shadow.track_items_list()
        BATCH = 500
        for i in range(0, len(items), BATCH):
            shadow.index_tracks_batch(items[i : i + BATCH])
            await asyncio.sleep(0)
        shadow.exit_batch_mode()   # one sort() per sorted index, on the shadow
        shadow.finish_rebuild()    # builds the shadow's _word_list

        # Diagnose BEFORE healing: diff the live (possibly drifted) indexes
        # against the freshly-built shadow.  This is the drift report the reindex
        # endpoints and the background integrity sweep surface.
        report = store._diff_indexes(shadow)

        # Swap the healed indexes in ONLY when NO scan is active.  A scan is
        # "active" if EITHER ``_batch_depth`` > 0 (mid-extract) OR ``_batch_mode``
        # is True with depth 0 — the latter is a scan-exit that already
        # decremented depth to 0 (scanner.py) but is PARKED on this very rebuild
        # lock waiting to run its own rebuild (it clears ``_batch_mode`` only in
        # its post-rebuild finally).
        #
        # Why NOT swap during a scan: our shadow is a snapshot; the concurrent
        # scan is mutating the live indexes via ``_index_track``/``_unindex_track``.
        # Swapping would clobber those updates.  For the SORTED indexes that's
        # recoverable (we flag ``_sorted_dirty`` so the scan's lock-serialized
        # exit rebuilds them fresh), but the scan's exit does NOT rebuild the
        # TAG/WORD indexes — so a mid-scan swap strands stale ``_tag_*`` /
        # ``_word_index`` entries (verified on a 262k library: a reindex hammered
        # during concurrent scans left +16 orphan artists / +4 words).  So during
        # a scan we DIAGNOSE ONLY — no swap.  The heal happens on the next idle
        # reindex or the background integrity sweep (which gates on is_scanning).
        if store._batch_depth == 0 and not store._batch_mode:
            for attr in INDEX_ATTRS:
                setattr(store, attr, getattr(shadow, attr))   # atomic swap (heal)
            store._word_list_dirty = shadow._word_list_dirty
            store._sorted_dirty = False
            store._batch_mode = False
        else:
            # A scan holds the batch — leave the live indexes untouched (no
            # clobber) and flag sorted dirty so the scan's exit still refreshes
            # the sorted views.  report still reflects the pre-heal diff.
            store._sorted_dirty = True
        store._mutation_seq += 1   # invalidate seq-keyed memos (store agg cache,
                                   # subsonic _ALBUM_*_CACHE, smart.py dup memos)
        return report


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
    query: str | None = None,
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
        track_ids=track_ids, owner_user_id=owner_user_id, query=query,
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


async def ft_search_dicts(
    query: str,
    limit: int = 50,
    offset: int = 0,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> list[dict]:
    """Like :func:`ft_search`, but returns the store's plain dicts directly —
    no ``TrackMeta`` round-trip.

    ``store.filter_tracks`` already produces ``_meta_dict`` output (the exact
    ``TrackMeta`` field set minus ``embedding``), so the hot list endpoints
    (``GET /api/tracks``, ``GET /api/search``) serialize these with orjson and
    skip the per-row ``TrackMeta(**...)`` construction + Pydantic re-encode
    (~12 ms/page saved on a 2000-track page).  Values are JSON-native
    (str/int/float/bool/None/list) so orjson serializes without a default
    hook.  Callers that need validated models still use ``ft_search``.
    """
    store = get_store()
    parsed = _parse_tag_query(query)
    hide_dups = bool(store.get_config("filter_duplicates", False))
    return store.filter_tracks(**parsed, limit=limit, offset=offset,
                               filter_duplicates=hide_dups,
                               sort_by=sort_by, sort_order=sort_order)


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


async def set_scan_dir_status(path: str, status: str) -> bool:
    return get_store().set_scan_dir_status(path, status)


# ── Scan-dir availability probe ──────────────────────────────────────────────
# A registered root can go temporarily unreachable — an SMB/FTP share drops, or
# a locally-mounted drive (presented at /Volumes/… or /mnt/…) is ejected or
# stalls.  We must NEVER treat that as "the files were deleted" (the scanner's
# stale-cleanup already refuses to prune a zero-listing / errored root); instead
# we flag the root ``unavailable`` so the UI can show it greyed rather than
# silently serving an empty or half-missing folder.
import asyncio as _asyncio
import time as _time
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor

_AVAIL_PROBE_TIMEOUT = 4.0        # seconds — a mount stalled past this is "unavailable"
_AVAIL_CACHE_TTL = 10.0           # re-probe at most this often (UI polls /dirs)
_avail_last_probe = 0.0
_avail_lock = _asyncio.Lock()
# Dedicated, bounded pool for the (potentially stalling) is_dir() probes.  A hung
# mount leaves its worker blocked until the OS returns, so we isolate these from
# the shared default executor that the AOF writer, snapshot writer, art cache,
# and streaming run on — a wedged mount can't starve persistence/playback.
_probe_executor = _ThreadPoolExecutor(max_workers=4, thread_name_prefix="availprobe")

# The same remote-scheme set the rest of the app uses (admin._is_remote): SMB/FTP
# plus WebDAV, whose scan roots are http(s):// URLs.  Without WebDAV here, a
# connected DAV share would fall into the LOCAL branch and Path("http://…").is_dir()
# would always be False → falsely reported "unavailable".
_REMOTE_SCHEMES = ("smb://", "ftp://", "http://", "https://", "webdav://", "webdavs://")


def _is_remote_root(path: str) -> bool:
    return path.startswith(_REMOTE_SCHEMES)


async def _probe_scan_dir(sd: dict) -> tuple[str, bool]:
    """Return ``(path, reachable)`` for one scan dir, bounded so a hung mount
    can never block the caller."""
    path = sd.get("path", "")
    if not path:
        return path, False
    if _is_remote_root(path):
        # Remote: reachable iff its FileSource is currently registered.  Sources
        # are registered under the scan-root URL (which IS ``path``), never the
        # ``network_share_id`` slug — keying by the slug would report every
        # connected share as unavailable.  A hard disconnect removes the source;
        # transient blips keep it (it auto-reconnects) so we don't flap on loss.
        try:
            from soniqboom.core.filesource import get_source
            return path, get_source(path) is not None
        except Exception:
            log.warning("Availability probe failed for remote root %s", path, exc_info=True)
            return path, False
    # Local (including a network drive mounted into the local namespace):
    # is_dir() on a stalled mount can block, so run it on the dedicated pool with
    # a hard timeout.
    from pathlib import Path as _Path
    loop = _asyncio.get_running_loop()
    try:
        ok = await _asyncio.wait_for(
            loop.run_in_executor(_probe_executor, lambda: _Path(path).is_dir()),
            timeout=_AVAIL_PROBE_TIMEOUT,
        )
        return path, bool(ok)
    except _asyncio.TimeoutError:
        return path, False           # stalled mount → treat as unavailable
    except OSError:
        return path, False           # ENOENT / ESTALE / EIO … → unavailable
    except Exception:
        log.warning("Availability probe failed for local root %s", path, exc_info=True)
        return path, False


async def refresh_scan_dir_availability(force: bool = False) -> list[dict]:
    """Probe every registered scan dir's reachability and update its ``status``
    to 'ok'/'unavailable' (only writing on an actual change).  Cached for a few
    seconds so rapid UI polls don't re-stat a slow mount every time.  Returns
    the fresh scan-dir list."""
    global _avail_last_probe
    store = get_store()
    now = _time.monotonic()
    if not force and (now - _avail_last_probe) < _AVAIL_CACHE_TTL:
        return store.list_scan_dirs()
    async with _avail_lock:
        # Re-check under the lock — another request may have just probed.
        now = _time.monotonic()
        if not force and (now - _avail_last_probe) < _AVAIL_CACHE_TTL:
            return store.list_scan_dirs()
        dirs = store.list_scan_dirs()
        results = await _asyncio.gather(*[_probe_scan_dir(sd) for sd in dirs])
        reachable = dict(results)
        for sd in dirs:
            p = sd.get("path", "")
            want = "ok" if reachable.get(p) else "unavailable"
            if store.set_scan_dir_status(p, want):
                log.info("Scan dir %s is now %s", p, want)
        _avail_last_probe = _time.monotonic()
        return store.list_scan_dirs()


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
