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
import heapq
import logging
import re
import time
from collections import Counter
from typing import Any, Callable


def normalise_year(y):
    """Collapse YYYYMMDD-form year ints to YYYY so the sorted index and the
    aggregation Counter agree.  Single source of truth — both
    ``TrackStore._index_track`` and ``scanner._async_exit_batch_mode``
    call this so neither path can drift from the other again.
    """
    if isinstance(y, int) and y > 9999:
        return y // 10000
    return y

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
        # Mutation sequence number — bumps on every track upsert / field
        # update / delete so caches keyed on store state can detect
        # changes even when the *count* doesn't move (rescan, retag,
        # set-primary).
        self._mutation_seq: int = 0

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
        # Parallel index tracking only primary (non-secondary-dup) tracks so
        # ``filter_tracks(filter_duplicates=True)`` and ``_paginate_all`` can
        # walk a shorter pre-filtered list rather than testing every entry's
        # ``is_duplicate_primary`` flag per request.
        self._sorted_added_at_primary: list[tuple[int, str]] = []
        self._sorted_duration: list[tuple[float, str]] = []
        self._sorted_bpm: list[tuple[float, str]] = []
        # Lexical sort indexes for column-header sort in the All Tracks
        # windowed view.  Keys are lower-cased so the order is locale-
        # agnostic case-insensitive — matches the user's expectation that
        # "ABBA" and "abba" sort adjacent.  Cost: ~2 MB each at 267K
        # tracks (interned string pointers + tuple overhead), ~10 MB
        # total for the four new indexes vs the 280 MB snapshot already
        # in RAM.  Insert/remove cost: bisect O(log N) per index, same
        # shape as the numeric sort indexes above.
        self._sorted_title: list[tuple[str, str]] = []
        self._sorted_artist: list[tuple[str, str]] = []
        self._sorted_album_artist: list[tuple[str, str]] = []
        self._sorted_album: list[tuple[str, str]] = []
        self._sorted_format: list[tuple[str, str]] = []

        # ── Pre-computed aggregations ────────────────────────────────────
        self._agg_artists: Counter[str] = Counter()
        self._agg_album_artists: Counter[str] = Counter()
        self._agg_albums: Counter[str] = Counter()
        self._agg_genres: Counter[str] = Counter()
        self._agg_years: Counter[int] = Counter()
        self._agg_albums_by_artist: dict[str, Counter[str]] = {}
        self._agg_albums_by_album_artist: dict[str, Counter[str]] = {}

        # ── Memoised aggregations keyed on _mutation_seq ─────────────────
        # Each entry is ``(seq, key, value)`` so a single ``_mutation_seq``
        # bump invalidates every cached aggregation in lock-step.  Mirrors
        # the (already-existing) duplicate-snapshot cache in api/smart.py.
        self._agg_cache: dict[str, tuple[int, Any]] = {}

        # ── Track IDs that have never been played ────────────────────────
        # Mirrors `play_stats` keys; updated on every track upsert / delete
        # and on `record_play`.  Lets api/smart.py serve the "unplayed"
        # view by walking ``_sorted_added_at`` filtered through this set
        # instead of scanning every track in the library.
        self._unplayed_ids: set[str] = set()

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

        # Extract numeric fields used by both sorted indexes and aggregations.
        # ``normalise_year`` collapses YYYYMMDD-form ints down to YYYY so the
        # sorted index agrees with the aggregation Counter — the same rule
        # is reused by scanner._async_exit_batch_mode via the module helper.
        year = normalise_year(t.get("year"))
        added = t.get("added_at", 0)
        dur = t.get("duration", 0.0)
        bpm = t.get("bpm")

        # Lexical sort keys for the column-header sort (windowed view).
        # Lower-cased so ABBA and abba sort adjacent; empty values fall
        # to the end of asc / start of desc — we use the ``￿``
        # sentinel for empties to keep that behaviour without a separate
        # filter pass at query time.  Using a high-BMP unicode codepoint
        # not present in real metadata.
        EMPTY_SORT_KEY = "￿"
        title_key        = (t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY
        artist_key       = (t.get("artist")       or "").strip().lower() or EMPTY_SORT_KEY
        album_artist_key = (t.get("album_artist") or "").strip().lower() or EMPTY_SORT_KEY
        album_key        = (t.get("album")        or "").strip().lower() or EMPTY_SORT_KEY
        fmt_key          = (t.get("format")       or "").strip().lower() or EMPTY_SORT_KEY

        if not self._batch_mode:
            if year is not None:
                _sorted_insert(self._sorted_year, (year, tid))
            if added:
                _sorted_insert(self._sorted_added_at, (added, tid))
                if t.get("is_duplicate_primary", True):
                    _sorted_insert(self._sorted_added_at_primary, (added, tid))
            if dur:
                _sorted_insert(self._sorted_duration, (dur, tid))
            if bpm is not None:
                _sorted_insert(self._sorted_bpm, (bpm, tid))
            _sorted_insert(self._sorted_title,        (title_key,        tid))
            _sorted_insert(self._sorted_artist,       (artist_key,       tid))
            _sorted_insert(self._sorted_album_artist, (album_artist_key, tid))
            _sorted_insert(self._sorted_album,        (album_key,        tid))
            _sorted_insert(self._sorted_format,       (fmt_key,          tid))
        else:
            self._sorted_dirty = True

        # Unplayed bookkeeping — a freshly-indexed track has no play stats yet
        # so it counts as unplayed unless ``record_play`` has already fired
        # (e.g. AOF replay applied "record_play" before the track upsert in
        # rare reorder cases — we still respect the existing entry).
        if tid not in self._play_stats:
            self._unplayed_ids.add(tid)

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

        # Extract numeric fields used by both sorted indexes and aggregations.
        # ``normalise_year`` keeps insert/remove in agreement.
        year = normalise_year(t.get("year"))
        added = t.get("added_at", 0)
        dur = t.get("duration", 0.0)
        bpm = t.get("bpm")

        # Same lexical key derivation as ``_index_track`` — must match
        # exactly or the remove turns into a no-op and the index drifts.
        EMPTY_SORT_KEY = "￿"
        title_key        = (t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY
        artist_key       = (t.get("artist")       or "").strip().lower() or EMPTY_SORT_KEY
        album_artist_key = (t.get("album_artist") or "").strip().lower() or EMPTY_SORT_KEY
        album_key        = (t.get("album")        or "").strip().lower() or EMPTY_SORT_KEY
        fmt_key          = (t.get("format")       or "").strip().lower() or EMPTY_SORT_KEY

        if not self._batch_mode:
            if year is not None:
                _sorted_remove(self._sorted_year, (year, tid))
            if added:
                _sorted_remove(self._sorted_added_at, (added, tid))
                if t.get("is_duplicate_primary", True):
                    _sorted_remove(self._sorted_added_at_primary, (added, tid))
            if dur:
                _sorted_remove(self._sorted_duration, (dur, tid))
            if bpm is not None:
                _sorted_remove(self._sorted_bpm, (bpm, tid))
            _sorted_remove(self._sorted_title,        (title_key,        tid))
            _sorted_remove(self._sorted_artist,       (artist_key,       tid))
            _sorted_remove(self._sorted_album_artist, (album_artist_key, tid))
            _sorted_remove(self._sorted_album,        (album_key,        tid))
            _sorted_remove(self._sorted_format,       (fmt_key,          tid))
        else:
            self._sorted_dirty = True

        # Unplayed bookkeeping — when a track is removed it can't be unplayed
        # anymore; this stops the set leaking entries for deleted tracks.
        self._unplayed_ids.discard(tid)

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
        """Rebuild all indexes from current data.

        Runs in batch mode so the 9 sorted indexes are built by one O(N log N)
        ``sort()`` at the end rather than N × ``bisect.insort`` (which is
        O(N) per call thanks to the memmove, so the per-track loop ends up
        O(N²) wall-clock and goes from ~17 s baseline to >70 s once we
        started maintaining the 5 lexical indexes too).
        """
        self.clear_indexes()
        self.enter_batch_mode()
        try:
            for tid, t in self._tracks.items():
                self._index_track(tid, t)
        finally:
            self.exit_batch_mode()
        self._rebuild_word_list()
        # ``_index_track`` adds every track to ``_unplayed_ids`` whose key
        # isn't already in ``_play_stats`` — the order above guarantees that
        # snapshot-loaded play stats correctly suppress those tids.
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
        self._sorted_added_at_primary.clear()
        self._sorted_duration.clear()
        self._sorted_bpm.clear()
        # Lexical sort indexes — must be cleared in lock-step with the
        # numeric ones, otherwise a ``rebuild_indexes()`` after a snapshot
        # load would leave stale ``(key, tid)`` entries from a previous
        # snapshot alongside the fresh inserts, and the windowed sort
        # would point at deleted track ids.
        self._sorted_title.clear()
        self._sorted_artist.clear()
        self._sorted_album_artist.clear()
        self._sorted_album.clear()
        self._sorted_format.clear()
        self._agg_artists.clear()
        self._agg_album_artists.clear()
        self._agg_albums.clear()
        self._agg_genres.clear()
        self._agg_years.clear()
        self._agg_albums_by_artist.clear()
        self._agg_albums_by_album_artist.clear()
        self._agg_cache.clear()
        self._unplayed_ids.clear()

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
        self._mutation_seq += 1
        self._aof("upsert_track", id=tid, data=track)

    def upsert_tracks_batch(self, tracks: list[dict]) -> int:
        for t in tracks:
            tid = t["id"]
            old = self._tracks.get(tid)
            if old:
                self._unindex_track(tid, old)
            self._tracks[tid] = t
            self._index_track(tid, t)
        self._mutation_seq += 1
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
        """Rebuild all sorted indexes from scratch.  O(n log n) via sort.

        Also rebuilds ``_sorted_added_at_primary`` (subset of
        ``_sorted_added_at`` for tracks that are the primary copy of their
        duplicate group, or aren't duplicates at all).
        """
        EMPTY_SORT_KEY = "￿"
        year, added, added_primary, dur, bpm = [], [], [], [], []
        title, artist_s, album_artist_s, album_s, fmt = [], [], [], [], []
        for tid, t in self._tracks.items():
            y = normalise_year(t.get("year"))
            if y is not None:
                year.append((y, tid))
            a = t.get("added_at", 0)
            if a:
                added.append((a, tid))
                if t.get("is_duplicate_primary", True):
                    added_primary.append((a, tid))
            d = t.get("duration", 0.0)
            if d:
                dur.append((d, tid))
            b = t.get("bpm")
            if b is not None:
                bpm.append((b, tid))
            # Lexical keys mirror ``_index_track`` exactly — same
            # ``.strip().lower()`` + EMPTY sentinel — so per-track
            # incremental insert/remove and the full rebuild produce
            # byte-identical index contents.
            title.append(         ((t.get("title")        or "").strip().lower() or EMPTY_SORT_KEY, tid))
            artist_s.append(      ((t.get("artist")       or "").strip().lower() or EMPTY_SORT_KEY, tid))
            album_artist_s.append(((t.get("album_artist") or "").strip().lower() or EMPTY_SORT_KEY, tid))
            album_s.append(       ((t.get("album")        or "").strip().lower() or EMPTY_SORT_KEY, tid))
            fmt.append(           ((t.get("format")       or "").strip().lower() or EMPTY_SORT_KEY, tid))
        year.sort(); added.sort(); added_primary.sort(); dur.sort(); bpm.sort()
        title.sort(); artist_s.sort(); album_artist_s.sort(); album_s.sort(); fmt.sort()
        self._sorted_year = year
        self._sorted_added_at = added
        self._sorted_added_at_primary = added_primary
        self._sorted_duration = dur
        self._sorted_bpm = bpm
        self._sorted_title = title
        self._sorted_artist = artist_s
        self._sorted_album_artist = album_artist_s
        self._sorted_album = album_s
        self._sorted_format = fmt
        log.info(
            "Sorted indexes rebuilt: %d year, %d added (%d primary), %d dur, %d bpm, "
            "%d title, %d artist, %d album_artist, %d album, %d fmt",
            len(year), len(added), len(added_primary), len(dur), len(bpm),
            len(title), len(artist_s), len(album_artist_s), len(album_s), len(fmt),
        )

    def delete_track(self, track_id: str) -> bool:
        t = self._tracks.pop(track_id, None)
        if t is None:
            return False
        self._unindex_track(track_id, t)
        self._waveforms.pop(track_id, None)
        self._mutation_seq += 1
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
            self._mutation_seq += 1
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
        self._mutation_seq += 1
        self._aof("update_track_fields", id=track_id, data=updates)
        return True

    def update_track_fields_batch(self, items: list[tuple[str, dict]]) -> int:
        """Apply many field-updates in one batch AOF record.

        Recompute-duplicates and bulk-tag-edit paths used to call
        ``update_track_fields`` once per track — for a 170K-track library
        that's 170K AOF records and hundreds of MB of journal data, which
        starved every other write during the recompute.  One batched record
        plus a single ``_mutation_seq`` bump is dramatically cheaper.
        """
        applied = 0
        records: list[dict] = []
        for tid, updates in items:
            t = self._tracks.get(tid)
            if not t:
                continue
            self._unindex_track(tid, t)
            t.update(updates)
            self._index_track(tid, t)
            applied += 1
            records.append({"id": tid, "data": updates})
        if applied:
            self._mutation_seq += 1
            self._aof("update_track_fields_batch", data=records)
        return applied

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

        # ``heapq.nlargest`` is O(N log K) vs. the previous full
        # ``sorted(...)`` which was O(N log N).  For a query that matches
        # tens of thousands of tracks but only needs the first page back,
        # this collapses the work to the top ``offset+limit`` entries.
        top = heapq.nlargest(
            offset + limit,
            result_ids,
            key=lambda tid: self._tracks[tid].get("added_at", 0),
        )
        page = top[offset : offset + limit]
        return [self._meta_dict(tid) for tid in page if tid in self._tracks]

    # Map a public ``sort_by`` value (the one the API exposes and the
    # column-header click sends) to the in-memory sorted index that drives
    # the paginated walk.  Single source of truth so the All Tracks /tracks
    # endpoint, ``filter_tracks``, and ``_paginate_all`` can't drift.
    #
    # ``added`` defaults to descending (newest first) because that's the
    # historical contract — every other key defaults to ascending and the
    # column header click flips with each press (handled at the API layer).
    _SORT_INDEX_MAP: dict[str, str] = {
        "added":        "_sorted_added_at",
        "year":         "_sorted_year",
        "duration":     "_sorted_duration",
        "bpm":          "_sorted_bpm",
        "title":        "_sorted_title",
        "artist":       "_sorted_artist",
        "album_artist": "_sorted_album_artist",
        "album":        "_sorted_album",
        "format":       "_sorted_format",
    }

    def _pick_sort_index(self, sort_by: str | None, *, filter_duplicates: bool) -> list[tuple]:
        """Return the sorted index list matching ``sort_by``.

        Falls back to ``_sorted_added_at`` (or its ``_primary`` variant when
        ``filter_duplicates`` is set) so callers that don't pass an explicit
        sort key still get the historical newest-first behaviour.
        """
        if not sort_by or sort_by == "added":
            return self._sorted_added_at_primary if filter_duplicates else self._sorted_added_at
        attr = self._SORT_INDEX_MAP.get(sort_by)
        if not attr:
            return self._sorted_added_at_primary if filter_duplicates else self._sorted_added_at
        return getattr(self, attr)

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
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> list[dict]:
        """Filter tracks by tag and/or range criteria, intersecting results.

        ``sort_by`` selects which pre-computed sorted index drives the page
        walk: one of ``title``, ``artist``, ``album``, ``format``, ``year``,
        ``duration``, ``bpm``, or ``added`` (default).  ``sort_order`` is
        ``"asc"`` or ``"desc"`` — falsy / unknown values default to ``desc``
        for ``added`` (newest first, historical) and ``asc`` for everything
        else (the natural reading order for a column header click).
        """
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
            return self._paginate_all(
                limit, offset,
                filter_duplicates=filter_duplicates,
                sort_by=sort_by, sort_order=sort_order,
            )

        result = sets[0]
        for s in sets[1:]:
            result = result & s
            if not result:
                return []

        if filter_duplicates:
            result = {tid for tid in result
                      if self._tracks.get(tid, {}).get("is_duplicate_primary", True)}

        # Pick the right sorted index for the requested sort, then walk it
        # in the requested direction keeping only tids that are in the
        # candidate set.  ``descending`` walk = reversed iterator over the
        # ascending index — Python's reversed() over a list is O(1) setup +
        # O(k) for k items consumed, same shape as the original code.
        idx = self._pick_sort_index(sort_by, filter_duplicates=filter_duplicates)
        descending = self._is_descending(sort_by, sort_order)
        walk_iter = reversed(idx) if descending else iter(idx)

        need = offset + limit
        collected: list[str] = []
        for _, tid in walk_iter:
            if tid in result:
                collected.append(tid)
                if len(collected) >= need:
                    break
        page = collected[offset : offset + limit]
        return [self._meta_dict(tid) for tid in page if tid in self._tracks]

    @staticmethod
    def _is_descending(sort_by: str | None, sort_order: str | None) -> bool:
        """Resolve the effective sort direction.

        Explicit ``sort_order`` wins; otherwise ``added`` defaults to desc
        (newest first, historical behaviour) and everything else defaults
        to asc (natural reading order for a fresh column click).
        """
        if sort_order:
            o = sort_order.strip().lower()
            if o in ("desc", "descending", "down", "d"):
                return True
            if o in ("asc", "ascending", "up", "a"):
                return False
        # No explicit order: only ``added`` (and its default-empty alias)
        # defaults to descending.
        return not sort_by or sort_by == "added"

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

    def _paginate_all(
        self,
        limit: int,
        offset: int,
        *,
        filter_duplicates: bool = False,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> list[dict]:
        """Return a paginated slice of all tracks.

        Default sort = ``added`` desc (newest first, historical contract).
        With ``filter_duplicates=True`` the pre-built
        ``_sorted_added_at_primary`` index drives a pure slice when the
        default sort is in use; for other sort keys ``filter_duplicates`` is
        applied per-row at walk time (no primary-variant index exists for
        every sortable column — the cost would be 8 extra indexes × 4–8 MB
        each, not worth it for the duplicates-hidden case which is the
        minority view).
        """
        descending = self._is_descending(sort_by, sort_order)

        # Fast path: default sort + no filter_duplicates ⇒ pure slice
        # off the already-sorted ``_sorted_added_at`` index.  This is the
        # path the windowed All Tracks view hits on the very first page
        # so it stays O(limit) regardless of library size.
        if (not sort_by or sort_by == "added") and not filter_duplicates:
            idx = self._sorted_added_at
            n = len(idx)
            if descending:
                # newest first → walk from the tail
                start = max(0, n - offset - limit)
                end = n - offset
                if end <= 0:
                    return []
                page = idx[max(start, 0) : end]
                return [self._meta_dict(tid) for _, tid in reversed(page) if tid in self._tracks]
            else:
                # oldest first → slice from the head
                page = idx[offset : offset + limit]
                return [self._meta_dict(tid) for _, tid in page if tid in self._tracks]

        # filter_duplicates + default sort retains its primary-variant fast
        # path so the duplicates-hidden default page is still O(limit).
        if (not sort_by or sort_by == "added") and filter_duplicates:
            idx = self._sorted_added_at_primary
            n = len(idx)
            if descending:
                start = max(0, n - offset - limit)
                end = n - offset
                if end <= 0:
                    return []
                page = idx[max(start, 0) : end]
                return [self._meta_dict(tid) for _, tid in reversed(page) if tid in self._tracks]
            else:
                page = idx[offset : offset + limit]
                return [self._meta_dict(tid) for _, tid in page if tid in self._tracks]

        # General path for non-default sort keys: walk the chosen sorted
        # index in the requested direction, optionally filtering duplicates
        # per row.  For 267K tracks the inner ``in self._tracks`` membership
        # test is a dict lookup (~70 ns), so a full page draw costs
        # ~limit×100 ns + the sort-time work we did once at index build.
        idx = self._pick_sort_index(sort_by, filter_duplicates=filter_duplicates)
        walk_iter = reversed(idx) if descending else iter(idx)

        # Consume offset items first, then collect limit items.
        skipped = 0
        collected: list[str] = []
        for _, tid in walk_iter:
            if filter_duplicates:
                t = self._tracks.get(tid)
                if not t or not t.get("is_duplicate_primary", True):
                    continue
            if skipped < offset:
                skipped += 1
                continue
            collected.append(tid)
            if len(collected) >= limit:
                break
        return [self._meta_dict(tid) for tid in collected if tid in self._tracks]

    def _meta_dict(self, tid: str) -> dict:
        t = self._tracks.get(tid)
        if not t:
            return {}
        return {k: v for k, v in t.items() if k != "embedding"}

    # ── Aggregations ─────────────────────────────────────────────────────

    def _agg_cache_get(self, key: str) -> Any | None:
        """Return the cached aggregation result for ``key`` if still valid.

        The cache is keyed on ``_mutation_seq`` so any track upsert / update /
        delete since the previous call automatically invalidates every entry
        without an explicit ``clear()`` — mirrors the duplicate-snapshot
        cache pattern in api/smart.py.
        """
        entry = self._agg_cache.get(key)
        if entry is not None and entry[0] == self._mutation_seq:
            return entry[1]
        return None

    def _agg_cache_set(self, key: str, value: Any) -> Any:
        self._agg_cache[key] = (self._mutation_seq, value)
        return value

    def aggregate_artists(self) -> list[dict]:
        """All artists with track counts, sorted alphabetically."""
        cached = self._agg_cache_get("artists")
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, count in self._agg_artists.items():
            tids = self._tag_artist.get(key)
            if not tids:
                continue
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = (t.get("artist") or "").strip() if t else key
            results.append({"artist": name or key, "count": count})
        results.sort(key=lambda x: x["artist"].lower())
        return self._agg_cache_set("artists", results)

    def aggregate_album_artists(self) -> list[dict]:
        cached = self._agg_cache_get("album_artists")
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, count in self._agg_album_artists.items():
            tids = self._tag_album_artist.get(key)
            if not tids:
                continue
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = (t.get("album_artist") or "").strip() if t else key
            results.append({"album_artist": name or key, "count": count})
        results.sort(key=lambda x: x["album_artist"].lower())
        return self._agg_cache_set("album_artists", results)

    def aggregate_albums(
        self, artist: str | None = None, album_artist: str | None = None,
    ) -> list[dict]:
        """Albums, optionally filtered by artist/album_artist."""
        # Cache key embeds the filter args so different (artist, album_artist)
        # combinations cache independently.
        cache_key = f"albums::{(artist or '').lower()}::{(album_artist or '').lower()}"
        cached = self._agg_cache_get(cache_key)
        if cached is not None:
            return cached

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
            # ``track_id`` is a representative track for the album so the
            # frontend can build its cover-art URL directly instead of
            # round-tripping a /search/filter lookup per grid card.
            results.append({"album": name or key, "count": count, "track_id": tid})
        results.sort(key=lambda x: x["album"].lower())
        return self._agg_cache_set(cache_key, results)

    def aggregate_genres(self) -> list[dict]:
        cached = self._agg_cache_get("genres")
        if cached is not None:
            return cached
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
        results.sort(key=lambda x: x["genre"].lower())
        return self._agg_cache_set("genres", results)

    def aggregate_formats(self) -> list[dict]:
        """Return ``[{format, count}]`` from the format tag index.

        Drives the library "Galaxy" visualization (per-format star
        clusters).  Counts come straight from ``_tag_format`` bucket
        sizes — O(number of distinct formats), no track scan.
        """
        cached = self._agg_cache_get("formats")
        if cached is not None:
            return cached
        results: list[dict] = []
        for key, tids in self._tag_format.items():
            if not key or not tids:
                continue
            # Resolve a display-cased name from one member track.
            tid = next(iter(tids))
            t = self._tracks.get(tid)
            name = (t.get("format") if t else None) or key
            results.append({"format": name, "count": len(tids)})
        results.sort(key=lambda x: -x["count"])
        return self._agg_cache_set("formats", results)

    def aggregate_years(self) -> list[dict]:
        cached = self._agg_cache_get("years")
        if cached is not None:
            return cached
        # Counter stores raw year values; normalize YYYYMMDD → YYYY.  The
        # previous code had two branches that performed the same division —
        # the first was unreachable because the second matched first for any
        # 5-or-more-digit value.
        merged: dict[int, int] = {}
        for y, count in self._agg_years.items():
            if isinstance(y, int) and y > 9999:
                y = y // 10000
            merged[y] = merged.get(y, 0) + count
        results = sorted(
            [{"year": y, "count": c} for y, c in merged.items()],
            key=lambda x: -x["year"],
        )
        return self._agg_cache_set("years", results)

    # ── Recently added (sorted index) ────────────────────────────────────

    def recently_added(self, limit: int = 100) -> list[dict]:
        ids = _sorted_tail(self._sorted_added_at, limit)
        return [self._meta_dict(tid) for tid in ids if tid in self._tracks]

    def list_unplayed(self, limit: int = 100) -> list[dict]:
        """Return tracks never played, newest-first.

        Backed by the incrementally-maintained ``_unplayed_ids`` set — walks
        ``_sorted_added_at`` in reverse, filtering membership.  This is
        O(limit + misses) instead of the previous O(N) scan over every
        track + ``get_all_play_stats`` snapshot in api/smart.py.
        """
        results: list[dict] = []
        unplayed = self._unplayed_ids
        for _, tid in reversed(self._sorted_added_at):
            if tid in unplayed and tid in self._tracks:
                results.append(self._meta_dict(tid))
                if len(results) >= limit:
                    break
        return results

    # ── Waveforms ────────────────────────────────────────────────────────

    def get_waveform(self, track_id: str) -> list[float] | None:
        return self._waveforms.get(track_id)

    def waveforms_view(self) -> dict[str, list[float]]:
        """Snapshot of all stored waveforms (shallow copy — safe to read
        off-thread while a scan stores new waveforms concurrently)."""
        return dict(self._waveforms)

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
            # First play — track is no longer unplayed.  Discard rather than
            # remove() so the set stays consistent if a play event arrives
            # before the corresponding track upsert (rare AOF replay reorder).
            self._unplayed_ids.discard(track_id)
        self._aof("record_play", id=track_id, ts=now)
        return dict(stats)

    def get_play_stats(self, track_id: str) -> dict:
        return dict(self._play_stats.get(track_id, {"count": 0}))

    def get_play_stats_batch(self, track_ids: list[str]) -> dict[str, dict]:
        return {tid: dict(self._play_stats[tid]) for tid in track_ids if tid in self._play_stats}

    def get_all_play_stats(self) -> dict[str, dict]:
        return {tid: dict(s) for tid, s in self._play_stats.items()}

    # ── Playlists ────────────────────────────────────────────────────────

    def create_playlist(
        self,
        playlist_id: str,
        name: str,
        track_ids: list[str] | None = None,
        owner_user_id: str | None = None,
        query: str | None = None,
    ) -> dict:
        now = int(time.time())
        # The API layer reads/writes ``track_ids`` consistently — historically
        # this code wrote ``tracks`` instead, so freshly-created playlists
        # appeared empty to every reader.  Use the same key everywhere.
        pl = {
            "id": playlist_id,
            "name": name,
            "track_ids": list(track_ids or []),
            "owner_user_id": owner_user_id,  # None ⇒ legacy/shared
            "query": query,                  # non-None ⇒ smart (auto-updating) playlist
            "created_at": now,
            "updated_at": now,
        }
        self._playlists[playlist_id] = pl
        self._aof("upsert_playlist", id=playlist_id, data=pl)
        return dict(pl)

    def list_playlists_for_user(self, user_id: str | None) -> list[dict]:
        """Return playlists visible to ``user_id``.  Visibility rule:
          * owner_user_id == user_id  → always visible (their own)
          * owner_user_id is None     → legacy/shared, visible to all
          * otherwise                 → hidden
        Pass ``user_id=None`` to get every playlist (admin view).
        """
        out = []
        for pl in self._playlists.values():
            pl = self._migrate_playlist(pl)
            owner = pl.get("owner_user_id")
            if user_id is None or owner is None or owner == user_id:
                out.append(dict(pl))
        return out

    def _migrate_playlist(self, pl: dict) -> dict:
        # Legacy playlists persisted with ``tracks`` instead of ``track_ids``.
        # Normalise on read so the API sees the canonical key without forcing
        # a full snapshot rewrite.
        if "track_ids" not in pl and "tracks" in pl:
            pl["track_ids"] = pl.pop("tracks")
        return pl

    def get_playlist(self, playlist_id: str) -> dict | None:
        pl = self._playlists.get(playlist_id)
        if not pl:
            return None
        return dict(self._migrate_playlist(pl))

    def list_playlists(self) -> list[dict]:
        return [dict(self._migrate_playlist(pl)) for pl in self._playlists.values()]

    def update_playlist(
        self, playlist_id: str, updates: dict,
    ) -> dict | None:
        """Apply ``updates`` to a playlist.

        When ``updates`` contains ``track_ids`` (or the legacy ``tracks``
        alias), any ids that don't exist in the track store are silently
        dropped and recorded under ``dropped_ids`` on the returned dict —
        a stale client clinging to deleted tracks no longer leaves orphan
        ids inside the playlist after a save.  Callers that want strict
        validation can inspect ``dropped_ids`` and 400 the response.
        """
        pl = self._playlists.get(playlist_id)
        if not pl:
            return None
        # Normalise legacy "tracks" → "track_ids" *before* applying the
        # update; otherwise PUT /playlists/{id} would leave both keys behind
        # and ``_migrate_playlist`` (guarded on "track_ids" not in pl) would
        # never run again on this playlist.
        self._migrate_playlist(pl)
        # Also strip a legacy "tracks" key from inbound updates: callers
        # should only send "track_ids", but accepting "tracks" here would
        # reintroduce the duplicate key that the migration just cleared.
        cleaned = {k: v for k, v in updates.items() if k != "tracks"}
        if "tracks" in updates and "track_ids" not in cleaned:
            cleaned["track_ids"] = updates["tracks"]
        # Prune unknown track ids from the inbound list so a playlist
        # update can't silently pin references to deleted tracks.
        dropped: list[str] = []
        if "track_ids" in cleaned and isinstance(cleaned["track_ids"], list):
            kept: list[str] = []
            for tid in cleaned["track_ids"]:
                if tid in self._tracks:
                    kept.append(tid)
                else:
                    dropped.append(tid)
            cleaned["track_ids"] = kept
        pl.update(cleaned)
        pl["updated_at"] = int(time.time())
        self._aof("upsert_playlist", id=playlist_id, data=dict(pl))
        out = dict(pl)
        if dropped:
            out["dropped_ids"] = dropped
        return out

    def delete_playlist(self, playlist_id: str) -> bool:
        if self._playlists.pop(playlist_id, None) is not None:
            self._aof("delete_playlist", id=playlist_id)
            return True
        return False

    # ── History ──────────────────────────────────────────────────────────

    def push_history(self, entry: dict) -> None:
        self._history.append(entry)
        if len(self._history) > self.history_max:
            # In-place slice delete — avoids the previous full-list
            # reallocation that fired on every recorded play.
            del self._history[: -self.history_max]
        self._aof("push_history", data=entry)

    def get_history(self, limit: int = 50) -> list[dict]:
        return list(reversed(self._history[-limit:]))

    # ── Scan dirs ────────────────────────────────────────────────────────

    def upsert_scan_dir(self, path: str, track_count_val: int | None = None,
                        network_share_id: str | None = None,
                        status: str = "ok") -> dict:
        existing = self._scan_dirs.get(path, {})
        now = int(time.time())
        # Cache the path_hash once at upsert time so ``list_scan_dirs`` no
        # longer recomputes ``hashlib.sha256(path).hexdigest()[:16]`` for
        # every dir on every request (admin UI polls this endpoint).
        ph = existing.get("path_hash") or hashlib.sha256(path.encode()).hexdigest()[:16]
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
            "path_hash": ph,
        }
        self._scan_dirs[path] = sd
        self._aof("upsert_scan_dir", path=path, data=sd)
        return dict(sd)

    def list_scan_dirs(self) -> list[dict]:
        result = []
        for sd in self._scan_dirs.values():
            d = dict(sd)
            # Always compute the live track count from the tag index
            # instead of relying on the cached field (which can be stale
            # if a scan was interrupted or the old code reset it to 0).
            path = d.get("path", "")
            # ``upsert_scan_dir`` writes ``path_hash`` at insert time; for
            # legacy entries persisted before this cache existed, fall back
            # to a one-shot recompute and stash it for next call.
            h = d.get("path_hash")
            if not h:
                h = hashlib.sha256(path.encode()).hexdigest()[:16]
                sd["path_hash"] = h
                d["path_hash"] = h
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

    # Soft cap on the "art known to be absent" set so it can't grow
    # unboundedly across a long-running session (Perf #1 flagged a
    # 170K-entry set on a fully-browsed library ≈ 9 MB).
    _ART_ABSENT_CAP = 20_000

    def mark_art_absent(self, track_id: str) -> None:
        if len(self._art_absent) >= self._ART_ABSENT_CAP:
            # Drop ~5% — pop_random would be O(1) but unstable; pop a few
            # arbitrary elements via ``pop()`` until under cap.
            drop_n = max(1, self._ART_ABSENT_CAP // 20)
            for _ in range(drop_n):
                try:
                    self._art_absent.pop()
                except KeyError:
                    break
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
