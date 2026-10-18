# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The library as Subsonic and DLNA clients see it while catalogue tracks are
hidden from them.

A catalogue (``core/catalogue.py`` — the Modland catalogue) can list hundreds
of thousands of tunes that are fetched from a volunteer archive when played.
Subsonic apps with offline sync / "cache everything" features (and DLNA
control points that index a whole server) would walk and download all of
them, so by default (the catalogue's "Show to Subsonic apps" switch off) those
clients see a library WITHOUT the catalogue: no songs, artists, albums,
genres, folders, search hits, random picks, stars or playlist entries of it,
and a catalogue id answers "not found" — also for stream / download.

``subsonic_store()`` returns the real store when nothing is hidden, else a
``SubsonicStoreView``: a thin proxy over the store that filters the track
mapping, the per-name / per-folder id indexes, the id lookups and the
filtered queries, and forwards everything else (writes included) unchanged.
``api/subsonic.py`` and ``api/dlna_upnp.py`` bind their ``get_store`` to it.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class _VisibleTracks(Mapping):
    """``store._tracks`` without the hidden ids."""

    __slots__ = ("_t", "_h")

    def __init__(self, tracks: dict, hidden) -> None:
        self._t = tracks
        self._h = hidden

    def __getitem__(self, tid):
        if tid in self._h:
            raise KeyError(tid)
        return self._t[tid]

    def get(self, tid, default=None):
        if tid in self._h:
            return default
        return self._t.get(tid, default)

    def __contains__(self, tid) -> bool:
        return tid in self._t and tid not in self._h

    def __iter__(self):
        h = self._h
        return (tid for tid in list(self._t) if tid not in h)

    def __len__(self) -> int:
        return max(0, len(self._t) - len(self._h))

    def keys(self):
        h = self._h
        return [tid for tid in self._t if tid not in h]

    def values(self):
        h = self._h
        return [t for tid, t in self._t.items() if tid not in h]

    def items(self):
        h = self._h
        return [(tid, t) for tid, t in self._t.items() if tid not in h]


class _VisibleIndex(Mapping):
    """A ``key → set of ids`` store index without the hidden ids."""

    __slots__ = ("_i", "_h")

    def __init__(self, idx: dict, hidden) -> None:
        self._i = idx
        self._h = hidden

    def _vis(self, s):
        if not s:
            return s
        h = self._h
        if len(s) <= 64 or len(s) * 4 <= len(h):
            return {t for t in s if t not in h}
        return s - h

    def __getitem__(self, key):
        return self._vis(self._i[key])

    def get(self, key, default=None):
        s = self._i.get(key)
        if s is None:
            return default
        return self._vis(s)

    def __contains__(self, key) -> bool:
        return bool(self.get(key))

    def __iter__(self):
        return iter(list(self._i))

    def __len__(self) -> int:
        return len(self._i)


class SubsonicStoreView:
    """The store without the hidden catalogue tracks (see the module doc).

    One view object per (store, hidden roots) — ``subsonic_store`` hands out
    the same one until that changes, because the Subsonic layer's caches are
    keyed on the store object's identity.  The hidden ids are read live from
    the store's per-root index on every use."""

    def __init__(self, store, roots) -> None:
        import hashlib
        object.__setattr__(self, "_s", store)
        object.__setattr__(self, "_roots", frozenset(roots))
        object.__setattr__(self, "_root_hashes",
                           tuple(hashlib.sha256(r.encode()).hexdigest()[:16] for r in roots))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._s, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._s, name, value)

    @property
    def _h(self):
        idx = self._s._tag_scan_root_hash
        sets = [s for h in self._root_hashes if (s := idx.get(h))]
        if not sets:
            return frozenset()
        if len(sets) == 1:
            return sets[0]
        # Several hidden roots (every online collection starts hidden): their
        # union, built again only after an insert / delete in the store
        # (``_catalog_seq`` bumps on both) or when a root's set was replaced.
        key = (getattr(self._s, "_catalog_seq", None), tuple((id(x), len(x)) for x in sets))
        cached = self.__dict__.get("_union")
        if cached is not None and cached[0] == key:
            return cached[1]
        out: set = set()
        for x in sets:
            out |= x
        object.__setattr__(self, "_union", (key, frozenset(out)))
        return self.__dict__["_union"][1]

    # ── the hidden-aware surface ──────────────────────────────────────────
    @property
    def _tracks(self):
        return _VisibleTracks(self._s._tracks, self._h)

    @property
    def _tag_artist(self):
        return _VisibleIndex(self._s._tag_artist, self._h)

    @property
    def _tag_album_artist(self):
        return _VisibleIndex(self._s._tag_album_artist, self._h)

    @property
    def _tag_dir_hash(self):
        return _VisibleIndex(self._s._tag_dir_hash, self._h)

    @property
    def _tag_genre(self):
        return _VisibleIndex(self._s._tag_genre, self._h)

    @property
    def _scan_dirs(self):
        return {p: sd for p, sd in self._s._scan_dirs.items()
                if p.rstrip("/") not in self._roots}

    def list_scan_dirs(self) -> list[dict]:
        return [d for d in self._s.list_scan_dirs()
                if (d.get("path") or "").rstrip("/") not in self._roots]

    def get_track(self, tid):
        if tid in self._h:
            return None
        return self._s.get_track(tid)

    def get_tracks_batch(self, ids):
        h = self._h
        return [None if tid in h else t for tid, t in zip(ids, self._s.get_tracks_batch(ids))]

    def track_count(self) -> int:
        return max(0, self._s.track_count() - len(self._h))

    def filter_tracks(self, **kw):
        kw["exclude"] = self._h
        return self._s.filter_tracks(**kw)

    def filter_track_ids(self, **kw):
        h = self._h
        return [t for t in self._s.filter_track_ids(**kw) if t not in h]

    def _candidate_ids(self, **kw):
        res = self._s._candidate_ids(**kw)
        if res is None:
            h = self._h
            return {t for t in self._s._tracks if t not in h}
        h = self._h
        return {t for t in res if t not in h}

    def similar_candidates(self, seed, **kw):
        h = self._h
        return [t for t in self._s.similar_candidates(seed, **kw)
                if (t.get("id") if isinstance(t, dict) else t) not in h]

    def aggregate_genres(self, primary_only: bool = False) -> list[dict]:
        """``store.aggregate_genres`` counting only the visible tracks (set
        arithmetic on the genre index; memoised on the store's browse
        sequence, ``_browse_seq``)."""
        s = self._s
        key = f"subsonic_visible_genres:{primary_only}:{len(self._h)}"
        cached = s._agg_cache_get(key)
        if cached is not None:
            return cached
        h = self._h
        out = []
        for row in s.aggregate_genres(primary_only):
            tids = s._tag_genre.get((row.get("genre") or "").strip().lower()) or set()
            if primary_only:
                n = s._count_primaries({t for t in tids if t not in h})
            else:
                n = len(tids) - len(tids & h if len(tids) <= len(h) else h & tids)
            if n > 0:
                out.append({**row, "count": n})
        return s._agg_cache_set(key, out)


    def aggregate_album_artists(self, primary_only: bool = False) -> list[dict]:
        """``store.aggregate_album_artists`` over the visible tracks."""
        s, h = self._s, self._h
        key = f"subsonic_visible_album_artists:{primary_only}:{len(h)}"
        cached = s._agg_cache_get(key)
        if cached is not None:
            return cached
        from soniqboom.core.store import name_display
        out = []
        for k in list(s._agg_album_artists):
            tids = s._tag_album_artist.get(k)
            if not tids:
                continue
            vis = [t for t in tids if t not in h] if h else list(tids)
            if primary_only:
                vis = [t for t in vis if s._tracks.get(t, {}).get("is_duplicate_primary", True)]
            if not vis:
                continue
            t = s._tracks.get(min(vis))
            name = name_display(t.get("album_artist"), k) if t else k
            out.append({"album_artist": name or k, "count": len(vis)})
        out.sort(key=lambda x: x["album_artist"].lower())
        return s._agg_cache_set(key, out)

    def aggregate_albums(self, artist: str | None = None, album_artist: str | None = None,
                         primary_only: bool = False) -> list[dict]:
        """``store.aggregate_albums`` over the visible tracks."""
        s, h = self._s, self._h
        key = (f"subsonic_visible_albums:{(artist or '').lower()}:"
               f"{(album_artist or '').lower()}:{primary_only}:{len(h)}")
        cached = s._agg_cache_get(key)
        if cached is not None:
            return cached
        if artist:
            keys = list(s._agg_albums_by_artist.get(artist.lower(), {}))
            scope = s._tag_artist.get(artist.lower()) or set()
        elif album_artist:
            keys = list(s._agg_albums_by_album_artist.get(album_artist.lower(), {}))
            scope = s._tag_album_artist.get(album_artist.lower()) or set()
        else:
            keys = list(s._agg_albums)
            scope = None
        out = []
        for k in keys:
            tids = s._tag_album.get(k)
            if not tids:
                continue
            members = tids if scope is None else (tids & scope)
            vis = [t for t in members if t not in h]
            if primary_only:
                vis = [t for t in vis if s._tracks.get(t, {}).get("is_duplicate_primary", True)]
            if not vis:
                continue
            rep = vis[0]
            t = s._tracks.get(rep)
            name = (t.get("album") or "").strip() if t else k
            out.append({"album": name or k, "count": len(vis), "track_id": rep})
        out.sort(key=lambda x: x["album"].lower())
        return s._agg_cache_set(key, out)


_VIEWS: dict = {}


def hidden_ids(store):
    """The track ids ``store`` (a ``subsonic_store`` result) hides — empty
    for the real store."""
    return store._h if isinstance(store, SubsonicStoreView) else frozenset()


def subsonic_store(store):
    """``store`` as Subsonic / DLNA clients may see it: the store itself when
    no catalogue is hidden from them, else the (cached, stable)
    ``SubsonicStoreView`` of it."""
    from soniqboom.core import catalogue
    roots = catalogue.hidden_roots()
    if not roots:
        return store
    key = (id(store), tuple(sorted(roots)))
    v = _VIEWS.get(key)
    if v is None or v._s is not store:
        _VIEWS.clear()
        v = _VIEWS[key] = SubsonicStoreView(store, roots)
    return v
