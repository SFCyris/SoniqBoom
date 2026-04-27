# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""In-memory TrackStore with indexed search.

All track metadata, ratings, play stats, playlists, history, waveforms, and
scan dirs live in Python dicts.  Search is served by inverted-word, tag, and
sorted indexes that are updated incrementally on every insert/update/delete.

Thread safety: all mutations happen on the asyncio event loop (single writer).
Read-only dict lookups are GIL-atomic and safe from any thread.
"""
from __future__ import annotations

import bisect
import hashlib
import logging
import re
import time
from collections import Counter
from typing import Any, Callable

from soniqboom.models.track import Track, TrackMeta

log = logging.getLogger(__name__)

# ── Tokeniser ────────────────────────────────────────────────────────────────

_SPLIT_RE = re.compile(r"[\s\-_.,;:!?()\[\]{}\"\'+=/\\|<>@#$%^&*~`]+")
_MIN_TOKEN_LEN = 1


def _tokenize_text(text: str) -> list[str]:
    """Split text into lowercase search tokens."""
    if not text:
        return []
    return [t for t in _SPLIT_RE.split(text.lower()) if len(t) >= _MIN_TOKEN_LEN]


def _tokenize_track(track: dict) -> set[str]:
    """Extract all searchable tokens from a track dict."""
    tokens: set[str] = set()
    for field in ("title", "artist", "album_artist", "album", "composer"):
        tokens.update(_tokenize_text(track.get(field, "")))
    return tokens


# ── Sorted-list helpers (bisect-based) ───────────────────────────────────────

def _sorted_insert(lst: list[tuple], item: tuple) -> None:
    bisect.insort(lst, item)


def _sorted_remove(lst: list[tuple], item: tuple) -> None:
    i = bisect.bisect_left(lst, item)
    if i < len(lst) and lst[i] == item:
        lst.pop(i)


def _sorted_range(lst: list[tuple], lo: Any, hi: Any) -> list[str]:
    """Return track IDs where lo <= value <= hi."""
    i = bisect.bisect_left(lst, (lo,))
    j = bisect.bisect_right(lst, (hi, "\uffff"))
    return [tid for _, tid in lst[i:j]]


def _sorted_tail(lst: list[tuple], n: int) -> list[str]:
    """Return the last n track IDs (highest values)."""
    return [tid for _, tid in lst[-n:]][::-1]


# ── TrackStore ───────────────────────────────────────────────────────────────

class TrackStore:
    """Central in-memory data store with indexed search."""

    def __init__(self) -> None:
        # ── Primary data ─────────────────────────────────────────────────
        self._tracks: dict[str, dict] = {}
        self._waveforms: dict[str, list[float]] = {}
        self._ratings: dict[str, int] = {}
        self._play_stats: dict[str, dict] = {}
        self._playlists: dict[str, dict] = {}
        self._history: list[dict] = []
        self._scan_dirs: dict[str, dict] = {}
        self._hash_lookups: dict[str, str] = {}
        self._config: dict[str, Any] = {}
        self._art_absent: set[str] = set()

        # ── Inverted word index ──────────────────────────────────────────
        self._word_index: dict[str, set[str]] = {}
        self._word_list: list[str] = []
        self._word_list_dirty = False

        # ── Tag indexes (lowered value → set of track IDs) ──────────────
        self._tag_artist: dict[str, set[str]] = {}
        self._tag_album_artist: dict[str, set[str]] = {}
        self._tag_album: dict[str, set[str]] = {}
        self._tag_genre: dict[str, set[str]] = {}
        self._tag_format: dict[str, set[str]] = {}
        self._tag_dir_hash: dict[str, set[str]] = {}
        self._tag_scan_root_hash: dict[str, set[str]] = {}
        self._tag_dup_group: dict[str, set[str]] = {}

        # ── Sorted indexes: list of (value, track_id) ───────────────────
        self._sorted_year: list[tuple[int, str]] = []
        self._sorted_added_at: list[tuple[int, str]] = []
        self._sorted_duration: list[tuple[float, str]] = []
        self._sorted_bpm: list[tuple[float, str]] = []

        # ── Pre-computed aggregations ────────────────────────────────────
        self._agg_artists: Counter[str] = Counter()
        self._agg_album_artists: Counter[str] = Counter()
        self._agg_albums: Counter[str] = Counter()
        self._agg_genres: Counter[str] = Counter()
        self._agg_years: Counter[int] = Counter()
        self._agg_albums_by_artist: dict[str, Counter[str]] = {}
        self._agg_albums_by_album_artist: dict[str, Counter[str]] = {}

        # ── AOF hook (set by aof module after init) ──────────────────────
        self._aof_append: Callable[..., None] | None = None

        # ── Batch mode: defer O(n) sorted-list rebuilds ─────────────────
        self._batch_mode: bool = False
        self._sorted_dirty: bool = False

        self.history_max = 500

    # ── AOF helper ───────────────────────────────────────────────────────

    def _aof(self, op: str, **kwargs: Any) -> None:
        if self._aof_append:
            self._aof_append(op, **kwargs)

    # ── Index maintenance ────────────────────────────────────────────────

    def _index_track(self, tid: str, t: dict) -> None:
        """Add a track to all indexes."""
        for token in _tokenize_track(t):
            self._word_index.setdefault(token, set()).add(tid)
        self._word_list_dirty = True

        self._tag_set(self._tag_artist, t.get("artist", ""), tid)
        self._tag_set(self._tag_album_artist, t.get("album_artist", ""), tid)
        self._tag_set(self._tag_album, t.get("album", ""), tid)
        self._tag_set(self._tag_format, t.get("format", ""), tid)
        self._tag_set(self._tag_dir_hash, t.get("dir_hash", ""), tid)
        self._tag_set(self._tag_scan_root_hash, t.get("scan_root_hash", ""), tid)
        gid = t.get("duplicate_group_id")
        if gid:
            self._tag_set(self._tag_dup_group, gid, tid)
        for g in t.get("genre", []):
            self._tag_set(self._tag_genre, g, tid)

        # Extract numeric fields used by both sorted indexes and aggregations
        year = t.get("year")
        added = t.get("added_at", 0)
        dur = t.get("duration", 0.0)
        bpm = t.get("bpm")

        if not self._batch_mode:
            if year is not None:
                _sorted_insert(self._sorted_year, (year, tid))
            if added:
                _sorted_insert(self._sorted_added_at, (added, tid))
            if dur:
                _sorted_insert(self._sorted_duration, (dur, tid))
            if bpm is not None:
                _sorted_insert(self._sorted_bpm, (bpm, tid))
        else:
            self._sorted_dirty = True

        # Aggregations
        artist = (t.get("artist") or "").strip()
        album_artist = (t.get("album_artist") or "").strip()
        album = (t.get("album") or "").strip()
        if artist:
            self._agg_artists[artist.lower()] += 1
            if album:
                self._agg_albums_by_artist.setdefault(artist.lower(), Counter())[album.lower()] += 1
        if album_artist:
            self._agg_album_artists[album_artist.lower()] += 1
            if album:
                self._agg_albums_by_album_artist.setdefault(album_artist.lower(), Counter())[album.lower()] += 1
        if album:
            self._agg_albums[album.lower()] += 1
        for g in t.get("genre", []):
            gl = g.strip().lower()
            if gl:
                self._agg_genres[gl] += 1
        if year is not None:
            self._agg_years[year] += 1

    def _unindex_track(self, tid: str, t: dict) -> None:
        """Remove a track from all indexes."""
        for token in _tokenize_track(t):
            s = self._word_index.get(token)
            if s:
                s.discard(tid)
                if not s:
                    del self._word_index[token]
        self._word_list_dirty = True

        self._tag_del(self._tag_artist, t.get("artist", ""), tid)
        self._tag_del(self._tag_album_artist, t.get("album_artist", ""), tid)
        self._tag_del(self._tag_album, t.get("album", ""), tid)
        self._tag_del(self._tag_format, t.get("format", ""), tid)
        self._tag_del(self._tag_dir_hash, t.get("dir_hash", ""), tid)
        self._tag_del(self._tag_scan_root_hash, t.get("scan_root_hash", ""), tid)
        gid = t.get("duplicate_group_id")
        if gid:
            self._tag_del(self._tag_dup_group, gid, tid)
        for g in t.get("genre", []):
            self._tag_del(self._tag_genre, g, tid)

        # Extract numeric fields used by both sorted indexes and aggregations
        year = t.get("year")
        added = t.get("added_at", 0)
        dur = t.get("duration", 0.0)
        bpm = t.get("bpm")

        if not self._batch_mode:
            if year is not None:
                _sorted_remove(self._sorted_year, (year, tid))
            if added:
                _sorted_remove(self._sorted_added_at, (added, tid))
            if dur:
                _sorted_remove(self._sorted_duration, (dur, tid))
            if bpm is not None:
                _sorted_remove(self._sorted_bpm, (bpm, tid))
        else:
            self._sorted_dirty = True

        artist = (t.get("artist") or "").strip()
        album_artist = (t.get("album_artist") or "").strip()
        album = (t.get("album") or "").strip()
        if artist:
            self._agg_artists[artist.lower()] -= 1
            if self._agg_artists[artist.lower()] <= 0:
                del self._agg_artists[artist.lower()]
            by_art = self._agg_albums_by_artist.get(artist.lower())
            if by_art and album:
                by_art[album.lower()] -= 1
                if by_art[album.lower()] <= 0:
                    del by_art[album.lower()]
        if album_artist:
            self._agg_album_artists[album_artist.lower()] -= 1
            if self._agg_album_artists[album_artist.lower()] <= 0:
                del self._agg_album_artists[album_artist.lower()]
            by_aa = self._agg_albums_by_album_artist.get(album_artist.lower())
            if by_aa and album:
                by_aa[album.lower()] -= 1
                if by_aa[album.lower()] <= 0:
                    del by_aa[album.lower()]
        if album:
            self._agg_albums[album.lower()] -= 1
            if self._agg_albums[album.lower()] <= 0:
                del self._agg_albums[album.lower()]
        for g in t.get("genre", []):
            gl = g.strip().lower()
            if gl:
                self._agg_genres[gl] -= 1
                if self._agg_genres[gl] <= 0:
                    del self._agg_genres[gl]
        if year is not None:
            self._agg_years[year] -= 1
            if self._agg_years[year] <= 0:
                del self._agg_years[year]

    @staticmethod
    def _tag_set(idx: dict[str, set[str]], value: str, tid: str) -> None:
        key = value.strip().lower() if isinstance(value, str) else str(value)
        if key:
            idx.setdefault(key, set()).add(tid)

    @staticmethod
    def _tag_del(idx: dict[str, set[str]], value: str, tid: str) -> None:
        key = value.strip().lower() if isinstance(value, str) else str(value)
        if key:
            s = idx.get(key)
            if s:
                s.discard(tid)
                if not s:
                    del idx[key]

    def _rebuild_word_list(self) -> None:
        if self._word_list_dirty:
            self._word_list = sorted(self._word_index.keys())
            self._word_list_dirty = False

    # ── Bulk load (used on startup — indexes rebuilt after all data loaded) ──

    def bulk_load(
        self,
        tracks: dict[str, dict],
        waveforms: dict[str, list[float]],
        ratings: dict[str, int],
        play_stats: dict[str, dict],
        playlists: dict[str, dict],
        history: list[dict],
        scan_dirs: dict[str, dict],
        hash_lookups: dict[str, str],
        config: dict[str, Any],
    ) -> None:
        """Load all data from a snapshot.  Call rebuild_indexes() afterwards."""
        self._tracks = tracks
        self._waveforms = waveforms
        self._ratings = ratings
        self._play_stats = play_stats
        self._playlists = playlists
        self._history = history
        self._scan_dirs = scan_dirs
        self._hash_lookups = hash_lookups
        self._config = config

    def rebuild_indexes(self) -> None:
        """Rebuild all indexes from current data.  ~50-100ms for 7K tracks."""
        self.clear_indexes()
        for tid, t in self._tracks.items():
            self._index_track(tid, t)
        self._rebuild_word_list()
        log.info("Indexes rebuilt for %d tracks", len(self._tracks))

    def clear_indexes(self) -> None:
        """Clear all indexes (first phase of rebuild, can be followed by
        batched ``index_tracks_batch`` calls)."""
        self._word_index.clear()
        self._word_list_dirty = True
        for tag_idx in (
            self._tag_artist, self._tag_album_artist, self._tag_album,
            self._tag_genre, self._tag_format, self._tag_dir_hash,
            self._tag_scan_root_hash, self._tag_dup_group,
        ):
            tag_idx.clear()
        self._sorted_year.clear()
        self._sorted_added_at.clear()
        self._sorted_duration.clear()
        self._sorted_bpm.clear()
        self._agg_artists.clear()
        self._agg_album_artists.clear()
        self._agg_albums.clear()
        self._agg_genres.clear()
        self._agg_years.clear()
        self._agg_albums_by_artist.clear()
        self._agg_albums_by_album_artist.clear()

    def index_tracks_batch(self, items: list[tuple[str, dict]]) -> None:
        """Index a batch of (track_id, track_dict) tuples."""
        for tid, t in items:
            self._index_track(tid, t)

    def finish_rebuild(self) -> None:
        """Finalise an async rebuild — build word list and log."""
        self._rebuild_word_list()
        log.info("Indexes rebuilt for %d tracks", len(self._tracks))

    def track_items_list(self) -> list[tuple[str, dict]]:
        """Return a snapshot of (track_id, track_dict) for chunked iteration."""
        return list(self._tracks.items())

    # ── Track CRUD ───────────────────────────────────────────────────────

    def get_track(self, track_id: str) -> dict | None:
        return self._tracks.get(track_id)

    def get_tracks_batch(self, track_ids: list[str]) -> list[dict | None]:
        return [self._tracks.get(tid) for tid in track_ids]

    def upsert_track(self, track: dict) -> None:
        tid = track["id"]
        old = self._tracks.get(tid)
        if old:
            self._unindex_track(tid, old)
        self._tracks[tid] = track
        self._index_track(tid, track)
        self._aof("upsert_track", id=tid, data=track)

    def upsert_tracks_batch(self, tracks: list[dict]) -> int:
        for t in tracks:
            tid = t["id"]
            old = self._tracks.get(tid)
            if old:
                self._unindex_track(tid, old)
            self._tracks[tid] = t
            self._index_track(tid, t)
        self._aof("batch_upsert_tracks", count=len(tracks), data=tracks)
        return len(tracks)

    # ── Batch mode: defer O(n) sorted-list operations ───────────────

    def enter_batch_mode(self) -> None:
        """Enter batch mode — sorted indexes are deferred until exit."""
        self._batch_mode = True
        self._sorted_dirty = False

    def exit_batch_mode(self) -> None:
        """Exit batch mode and rebuild sorted indexes if dirty."""
        self._batch_mode = False
        if self._sorted_dirty:
            self._rebuild_sorted_indexes()
            self._sorted_dirty = False

    def _rebuild_sorted_indexes(self) -> None:
        """Rebuild all 4 sorted indexes from scratch.  O(n log n) via sort."""
        year, added, dur, bpm = [], [], [], []
        for tid, t in self._tracks.items():
            y = t.get("year")
            if y is not None:
                year.append((y, tid))
            a = t.get("added_at", 0)
            if a:
                added.append((a, tid))
            d = t.get("duration", 0.0)
            if d:
                dur.append((d, tid))
            b = t.get("bpm")
            if b is not None:
                bpm.append((b, tid))
        year.sort(); added.sort(); dur.sort(); bpm.sort()
        self._sorted_year = year
        self._sorted_added_at = added
        self._sorted_duration = dur
        self._sorted_bpm = bpm
        log.info("Sorted indexes rebuilt: %d year, %d added, %d dur, %d bpm",
                 len(year), len(added), len(dur), len(bpm))

    def delete_track(self, track_id: str) -> bool:
        t = self._tracks.pop(track_id, None)
        if t is None:
            return False
        self._unindex_track(track_id, t)
        self._waveforms.pop(track_id, None)
        self._aof("delete_tracks", ids=[track_id])
        return True

    def delete_track_ids(self, track_ids: list[str]) -> int:
        deleted = 0
        ids = []
        for tid in track_ids:
            t = self._tracks.pop(tid, None)
            if t:
                self._unindex_track(tid, t)
                self._waveforms.pop(tid, None)
                deleted += 1
                ids.append(tid)
        if ids:
            self._aof("delete_tracks", ids=ids)
        return deleted

    def track_count(self) -> int:
        return len(self._tracks)

    def all_tracks(self) -> list[dict]:
        return list(self._tracks.values())

    def all_track_metas(self) -> list[dict]:
        """Return all tracks without embedding field."""
        return [
            {k: v for k, v in t.items() if k != "embedding"}
            for t in self._tracks.values()
        ]

    def get_track_ids_for_scan_root(self, root_hash: str) -> set[str]:
        return set(self._tag_scan_root_hash.get(root_hash, set()))

    def update_track_fields(self, track_id: str, updates: dict) -> bool:
        """Update specific fields on an existing track."""
        t = self._tracks.get(track_id)
        if not t:
            return False
        self._unindex_track(track_id, t)
        t.update(updates)
        self._index_track(track_id, t)
        self._aof("update_track_fields", id=track_id, data=updates)
        return True

    # ── Search ───────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 50, offset: int = 0) -> list[dict]:
        """Full-text search across indexed fields.

        Returns track dicts (without embedding) matching all query tokens.
        """
        if not query or query == "*":
            return self._paginate_all(limit, offset)

        result_ids = self._resolve_query(query)
        if result_ids is None:
            return []

        ids = sorted(result_ids, key=lambda tid: self._tracks[tid].get("added_at", 0), reverse=True)
        page = ids[offset : offset + limit]
        return [self._meta_dict(tid) for tid in page if tid in self._tracks]

    def filter_tracks(
        self,
        artist: str | None = None,
        album_artist: str | None = None,
        album: str | None = None,
        genre: str | None = None,
        format_: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        dir_hash: str | None = None,
        scan_root_hash: str | None = None,
        query: str | None = None,
        limit: int = 200,
        offset: int = 0,
        filter_duplicates: bool = False,
    ) -> list[dict]:
        """Filter tracks by tag and/or range criteria, intersecting results."""
        sets: list[set[str]] = []

        if artist:
            sets.append(self._tag_artist.get(artist.lower(), set()))
        if album_artist:
            sets.append(self._tag_album_artist.get(album_artist.lower(), set()))
        if album:
            sets.append(self._tag_album.get(album.lower(), set()))
        if genre:
            sets.append(self._tag_genre.get(genre.lower(), set()))
        if format_:
            sets.append(self._tag_format.get(format_.lower(), set()))
        if dir_hash:
            sets.append(self._tag_dir_hash.get(dir_hash, set()))
        if scan_root_hash:
            sets.append(self._tag_scan_root_hash.get(scan_root_hash, set()))
        if year_min is not None or year_max is not None:
            lo = year_min if year_min is not None else -999999
            hi = year_max if year_max is not None else 999999
            sets.append(set(_sorted_range(self._sorted_year, lo, hi)))
        if query:
            q_ids = self._resolve_query(query)
            if q_ids is not None:
                sets.append(q_ids)

        if not sets:
            return self._paginate_all(limit, offset, filter_duplicates=filter_duplicates)

        result = sets[0]
        for s in sets[1:]:
            result = result & s
            if not result:
                return []

        if filter_duplicates:
            result = {tid for tid in result
                      if self._tracks.get(tid, {}).get("is_duplicate_primary", True)}

        ids = sorted(result, key=lambda tid: self._tracks.get(tid, {}).get("added_at", 0), reverse=True)
        page = ids[offset : offset + limit]
        return [self._meta_dict(tid) for tid in page if tid in self._tracks]

    def _resolve_query(self, query: str) -> set[str] | None:
        """Resolve a text query to a set of matching track IDs."""
        tokens = _tokenize_text(query)
        if not tokens:
            return None

        self._rebuild_word_list()
        sets: list[set[str]] = []
        for token in tokens:
            exact = self._word_index.get(token)
            if exact:
                sets.append(exact)
            else:
                prefix_match = self._prefix_match(token)
                if prefix_match:
                    sets.append(prefix_match)
                else:
                    return set()

        if not sets:
            return set()

        result = sets[0].copy()
        for s in sets[1:]:
            result &= s
        return result

    def _prefix_match(self, prefix: str) -> set[str]:
        """Find all track IDs matching tokens that start with prefix."""
        lo = bisect.bisect_left(self._word_list, prefix)
        hi = bisect.bisect_right(self._word_list, prefix + "\uffff")
        result: set[str] = set()
        for word in self._word_list[lo:hi]:
            result |= self._word_index.get(word, set())
        return result

    def _paginate_all(self, limit: int, offset: int, *, filter_duplicates: bool = False) -> list[dict]:
        """Return a paginated slice of all tracks, newest first."""
        if not filter_duplicates:
            n = len(self._sorted_added_at)
            start = max(0, n - offset - limit)
            end = n - offset
            if end <= 0:
                return []
            page = self._sorted_added_at[max(start, 0) : end]
            return [self._meta_dict(tid) for _, tid in reversed(page) if tid in self._tracks]

        results: list[dict] = []
        skipped = 0
        for _, tid in reversed(self._sorted_added_at):
            t = self._tracks.get(tid)
            if not t:
                continue
            if not t.get("is_duplicate_primary", True):
                continue
            if skipped < offset:
                skipped += 1
                continue
            results.append(self._meta_dict(tid))
            if len(results) >= limit:
                break
        return results

    def _meta_dict(self, tid: str) -> dict:
        t = self._tracks.get(tid)
        if not t:
            return {}
        return {k: v for k, v in t.items() if k != "embedding"}

    # ── Aggregations ─────────────────────────────────────────────────────

    def aggregate_artists(self) -> list[dict]:
        """All artists with track counts, sorted alphabetically."""
        results: list[dict] = []
        for key, count in self._agg_artists.items():
            tids = self._tag_artist.get(key)
            if not tids:
                continue
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = (t.get("artist") or "").strip() if t else key
            results.append({"artist": name or key, "count": count})
        return sorted(results, key=lambda x: x["artist"].lower())

    def aggregate_album_artists(self) -> list[dict]:
        results: list[dict] = []
        for key, count in self._agg_album_artists.items():
            tids = self._tag_album_artist.get(key)
            if not tids:
                continue
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = (t.get("album_artist") or "").strip() if t else key
            results.append({"album_artist": name or key, "count": count})
        return sorted(results, key=lambda x: x["album_artist"].lower())

    def aggregate_albums(
        self, artist: str | None = None, album_artist: str | None = None,
    ) -> list[dict]:
        """Albums, optionally filtered by artist/album_artist."""
        if artist:
            album_counter = self._agg_albums_by_artist.get(
                artist.lower(), Counter(),
            )
        elif album_artist:
            album_counter = self._agg_albums_by_album_artist.get(
                album_artist.lower(), Counter(),
            )
        else:
            album_counter = self._agg_albums

        results: list[dict] = []
        for key, count in album_counter.items():
            tids = self._tag_album.get(key)
            if not tids:
                continue
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = (t.get("album") or "").strip() if t else key
            results.append({"album": name or key, "count": count})
        return sorted(results, key=lambda x: x["album"].lower())

    def aggregate_genres(self) -> list[dict]:
        results: list[dict] = []
        for key, count in self._agg_genres.items():
            tids = self._tag_genre.get(key)
            if not tids:
                continue
            # Resolve proper-cased display name from one track
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = key  # fallback
            if t:
                for g in t.get("genre", []):
                    if g.strip().lower() == key:
                        name = g.strip()
                        break
            results.append({"genre": name, "count": count})
        return sorted(results, key=lambda x: x["genre"].lower())

    def aggregate_years(self) -> list[dict]:
        # Counter stores raw year values; normalize YYYYMMDD → YYYY
        merged: dict[int, int] = {}
        for y, count in self._agg_years.items():
            if isinstance(y, int) and y > 99991231:
                y = y // 10000
            elif isinstance(y, int) and y > 9999:
                y = y // 10000
            merged[y] = merged.get(y, 0) + count
        return sorted(
            [{"year": y, "count": c} for y, c in merged.items()],
            key=lambda x: -x["year"],
        )

    # ── Recently added (sorted index) ────────────────────────────────────

    def recently_added(self, limit: int = 100) -> list[dict]:
        ids = _sorted_tail(self._sorted_added_at, limit)
        return [self._meta_dict(tid) for tid in ids if tid in self._tracks]

    # ── Waveforms ────────────────────────────────────────────────────────

    def get_waveform(self, track_id: str) -> list[float] | None:
        return self._waveforms.get(track_id)

    def store_waveform(self, track_id: str, amplitudes: list[float]) -> None:
        self._waveforms[track_id] = amplitudes

    def store_waveforms_batch(self, mapping: dict[str, list[float]]) -> None:
        self._waveforms.update(mapping)

    def waveform_exists_batch(self, track_ids: list[str]) -> dict[str, bool]:
        return {tid: tid in self._waveforms for tid in track_ids}

    def clear_waveforms(self) -> int:
        n = len(self._waveforms)
        self._waveforms.clear()
        return n

    # ── Ratings ──────────────────────────────────────────────────────────

    def get_rating(self, track_id: str) -> int:
        return self._ratings.get(track_id, 0)

    def set_rating(self, track_id: str, rating: int) -> None:
        if rating <= 0:
            self._ratings.pop(track_id, None)
        else:
            self._ratings[track_id] = rating
        self._aof("set_rating", id=track_id, rating=rating)

    def get_ratings_batch(self, track_ids: list[str]) -> dict[str, int]:
        return {tid: self._ratings[tid] for tid in track_ids if tid in self._ratings}

    def get_all_ratings(self) -> dict[str, int]:
        return dict(self._ratings)

    # ── Play stats ───────────────────────────────────────────────────────

    def record_play(self, track_id: str) -> dict:
        now = int(time.time())
        stats = self._play_stats.get(track_id)
        if stats:
            stats["count"] = stats.get("count", 0) + 1
            stats["last_played"] = now
        else:
            stats = {"count": 1, "last_played": now}
            self._play_stats[track_id] = stats
        self._aof("record_play", id=track_id, ts=now)
        return dict(stats)

    def get_play_stats(self, track_id: str) -> dict:
        return dict(self._play_stats.get(track_id, {"count": 0}))

    def get_play_stats_batch(self, track_ids: list[str]) -> dict[str, dict]:
        return {tid: dict(self._play_stats[tid]) for tid in track_ids if tid in self._play_stats}

    def get_all_play_stats(self) -> dict[str, dict]:
        return {tid: dict(s) for tid, s in self._play_stats.items()}

    # ── Playlists ────────────────────────────────────────────────────────

    def create_playlist(self, playlist_id: str, name: str, track_ids: list[str] | None = None) -> dict:
        now = int(time.time())
        pl = {
            "id": playlist_id,
            "name": name,
            "tracks": track_ids or [],
            "created_at": now,
            "updated_at": now,
        }
        self._playlists[playlist_id] = pl
        self._aof("upsert_playlist", id=playlist_id, data=pl)
        return dict(pl)

    def get_playlist(self, playlist_id: str) -> dict | None:
        pl = self._playlists.get(playlist_id)
        return dict(pl) if pl else None

    def list_playlists(self) -> list[dict]:
        return [dict(pl) for pl in self._playlists.values()]

    def update_playlist(self, playlist_id: str, updates: dict) -> dict | None:
        pl = self._playlists.get(playlist_id)
        if not pl:
            return None
        pl.update(updates)
        pl["updated_at"] = int(time.time())
        self._aof("upsert_playlist", id=playlist_id, data=dict(pl))
        return dict(pl)

    def delete_playlist(self, playlist_id: str) -> bool:
        if self._playlists.pop(playlist_id, None) is not None:
            self._aof("delete_playlist", id=playlist_id)
            return True
        return False

    # ── History ──────────────────────────────────────────────────────────

    def push_history(self, entry: dict) -> None:
        self._history.append(entry)
        if len(self._history) > self.history_max:
            self._history = self._history[-self.history_max :]
        self._aof("push_history", data=entry)

    def get_history(self, limit: int = 50) -> list[dict]:
        return list(reversed(self._history[-limit:]))

    # ── Scan dirs ────────────────────────────────────────────────────────

    def upsert_scan_dir(self, path: str, track_count_val: int | None = None,
                        network_share_id: str | None = None,
                        status: str = "ok") -> dict:
        existing = self._scan_dirs.get(path, {})
        now = int(time.time())
        sd = {
            "path": path,
            # Preserve the existing count when no explicit value is given
            # (e.g. at scan start before the final count is known).
            "track_count": track_count_val if track_count_val is not None
                           else existing.get("track_count", 0),
            "added_at": existing.get("added_at", now),
            "last_scanned": now,
            "network_share_id": network_share_id or existing.get("network_share_id"),
            "status": status,
        }
        self._scan_dirs[path] = sd
        self._aof("upsert_scan_dir", path=path, data=sd)
        return dict(sd)

    def list_scan_dirs(self) -> list[dict]:
        import hashlib
        result = []
        for sd in self._scan_dirs.values():
            d = dict(sd)
            # Always compute the live track count from the tag index
            # instead of relying on the cached field (which can be stale
            # if a scan was interrupted or the old code reset it to 0).
            path = d.get("path", "")
            h = hashlib.sha256(path.encode()).hexdigest()[:16]
            d["track_count"] = len(self._tag_scan_root_hash.get(h, set()))
            result.append(d)
        return result

    def delete_scan_dir(self, path: str) -> bool:
        if self._scan_dirs.pop(path, None) is not None:
            self._aof("delete_scan_dir", path=path)
            return True
        return False

    # ── Hash lookups ─────────────────────────────────────────────────────

    def store_hash_lookup(self, value: str) -> str:
        h = hashlib.sha256(value.encode()).hexdigest()[:16]
        self._hash_lookups[h] = value
        return h

    def store_hash_lookups_batch(self, values: list[str]) -> dict[str, str]:
        result = {}
        for v in values:
            h = hashlib.sha256(v.encode()).hexdigest()[:16]
            self._hash_lookups[h] = v
            result[v] = h
        return result

    def resolve_hash(self, h: str) -> str | None:
        return self._hash_lookups.get(h)

    def list_hash_lookups(self) -> dict[str, str]:
        return dict(self._hash_lookups)

    # ── Config ───────────────────────────────────────────────────────────

    def set_config(self, key: str, value: Any) -> None:
        self._config[key] = value
        self._aof("set_config", key=key, value=value)

    def get_config(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    # ── Art absent tracking ──────────────────────────────────────────────

    def mark_art_absent(self, track_id: str) -> None:
        self._art_absent.add(track_id)

    def is_art_absent(self, track_id: str) -> bool:
        return track_id in self._art_absent

    def clear_art_absent(self) -> None:
        self._art_absent.clear()

    # ── Serialisation (for persistence snapshot) ─────────────────────────

    def to_snapshot(self) -> dict:
        """Serialise entire store state to a dict for JSON persistence."""
        return {
            "tracks": self._tracks,
            "waveforms": self._waveforms,
            "ratings": self._ratings,
            "play_stats": self._play_stats,
            "playlists": self._playlists,
            "history": self._history,
            "scan_dirs": self._scan_dirs,
            "hash_lookups": self._hash_lookups,
            "config": self._config,
        }


# ── Module-level singleton ───────────────────────────────────────────────────

_store: TrackStore | None = None


def get_store() -> TrackStore:
    global _store
    if _store is None:
        _store = TrackStore()
    return _store
