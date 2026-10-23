# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browse lists, counts and legends as set arithmetic.

The store keeps the ids a list hides as maintained sets — the non-primary
duplicate copies (``_nonprimary_ids``), the "[No Artist]"-style buckets
(``_untagged_*``), year → ids (``_tag_year``) — and leaves them out with set
operations instead of a per-row test; its browse memos (aggregates, deep-page
orders, the duplicate stand-ins, the resumed ``filter_tracks`` walks) key on
``_browse_seq``, which writes no list reads (a health mark, a play-time
duration backfill, cover art) leave alone.

Checked here: the sets follow every write path (incremental, batch, rebuild,
index swap); a seeded randomized run of writes in which every page, count and
legend equals a brute-force evaluation over the tracks, and every memoised
answer equals the same call made cold; the browse counter's exact contract;
the incremental duplicate stand-ins equal a full build; the resumed walks;
reads off the event-loop thread.
"""
from __future__ import annotations

import copy
import json
import random
import threading

import pytest

from soniqboom.api import library
from soniqboom.core import store as store_mod
from soniqboom.core.store import (TrackStore, _BROWSE_EXEMPT_FIELDS, _BROWSE_EXTRA_FIELDS,
                                  _INDEXED_FIELDS, _SEQ_EXEMPT_FIELDS, _SORTED_SPECS,
                                  _SWAP_FIELDS, _agg_year, track_genres)

SORTS = [None, "added", "year", "duration", "bpm", "title", "artist", "album_artist",
         "album", "format"]
ORDERS = [None, "asc", "desc"]


# ── fixtures ───────────────────────────────────────────────────────────────

ARTISTS = ["Rob Hubbard", "rob hubbard", "Jeroen Tel", "A • B", "", "  ", None, "Öörni"]
ALBUMS = ["Commando", "commando", "", None, "Sanxion", "Pokémon"]
GENRES = [[], ["Chiptune"], ["chiptune", "Game"], [""], None, ["Rock"]]
FORMATS = ["SID", "sid", "ProTracker", "MP3", "", "FLAC"]
TITLES = ["Commando", "Uridium 2", "The Last Ninja 3", "Pokémon theme", "", "Mad Max",
          "Madness", "Music Disk", "Theme", "song"]
ROOTS = ("r0", "r1", "r2", "r3")


def _year(r):
    return r.choice([None, 1985, 1991, 19950312, "1999", 1999.5, float("nan"), 0, 2024])


def _mk(r: random.Random, i: int, **kw) -> dict:
    root = r.choice(ROOTS)
    t = {"id": f"t{i:04d}", "path": f"/{root}/d{i % 5}/{i}.x", "scan_root_hash": root,
         "dir_hash": f"{root}d{i % 5}",
         "title": r.choice(TITLES) + (f" {i % 7}" if r.random() < 0.3 else ""),
         "artist": r.choice(ARTISTS), "album_artist": r.choice(ARTISTS),
         "album": r.choice(ALBUMS), "genre": copy.deepcopy(r.choice(GENRES)),
         "format": r.choice(FORMATS), "year": _year(r), "bitrate": r.choice([None, 128, 900]),
         "added_at": r.choice([0, None, r.randint(1, 300)]),
         "duration": r.choice([None, 0.0, float("nan"), 60.0, r.randint(1, 500)])}
    if r.random() < 0.2:
        t["genre_derived"] = r.choice([["Chiptune"], [], None])
    if r.random() < 0.2:
        t["game"] = r.choice(["Uridium", "The Last Ninja", None, ""])
        t["game_source"] = r.choice([None, "folder", "tag"])
    t.update(kw)
    return t


def _library(r: random.Random, n: int) -> list[dict]:
    out, i = [], 0
    while i < n:
        if r.random() < 0.2:
            k = r.randint(2, 4)
            prim = r.randrange(k)
            for j in range(k):
                out.append(_mk(r, i, duplicate_group_id=f"g{i - j}",
                               is_duplicate_primary=(j == prim) if r.random() < 0.9
                               else r.choice([True, False, None])))
                i += 1
        else:
            out.append(_mk(r, i))
            i += 1
    return out


# ── brute-force references ─────────────────────────────────────────────────

def _untagged(t: dict, field: str) -> bool:
    """The "[No Artist]"-style blank-tag test, written out independently of
    the store's (a list is blank when no element has text; genre reads the
    genres the track is listed under)."""
    v = track_genres(t) if field == "genre" else t.get(field)
    if isinstance(v, (list, tuple)):
        return not any(str(x).strip() for x in v)
    return v is None or not str(v).strip()


def _primary(s: TrackStore, tid: str, swap) -> bool:
    t = s._tracks.get(tid, {})
    return swap.is_primary(tid, t) if swap is not None else t.get("is_duplicate_primary", True)


def _expected_page_ids(s: TrackStore, preds: dict, dedup: bool, exclude, swap,
                       sort_by, sort_order) -> list[str]:
    """The whole list ``filter_tracks`` pages through, by brute force over the
    tracks: the predicates' candidates (``_candidate_ids`` without the
    duplicate filter — its tag / text / game / year resolution is not what
    this file changes) or every track, less the excluded ids and the hidden
    copies, ordered by the sort index's (value, id) keys."""
    if not dedup:
        swap = None
    base = s._candidate_ids(**preds) if preds else None
    ids = set(s._tracks) if base is None else set(base)
    ids = {t for t in ids if t in s._tracks and not (exclude and t in exclude)
           and (not dedup or _primary(s, t, swap))}
    attr = s._sort_index_attr(sort_by, filter_duplicates=dedup and swap is None)
    value = _SORTED_SPECS[attr][1]
    rows = sorted((value(s._tracks[t]), t) for t in ids if value(s._tracks[t]) is not None)
    out = [t for _v, t in rows]
    return out[::-1] if s._is_descending(sort_by, sort_order) else out


def _ref_count_visible(s, tids, primary_only, exclude, swap):
    """The per-row count the legends used before the set arithmetic."""
    if not primary_only:
        return len(tids) - len(exclude.intersection(tids))
    shown = tids.difference(exclude) if exclude else tids
    return sum(1 for tid in shown if _primary(s, tid, swap))


def _ref_bucket_count(s, tids, primary_only, exclude, swap):
    if exclude and not exclude.isdisjoint(tids):
        return _ref_count_visible(s, tids, primary_only, exclude, swap)
    if swap is not None and not swap.changed.isdisjoint(tids):
        return _ref_count_visible(s, tids, True, exclude, swap)
    return None


def _ref_albums(s, artist=None, album_artist=None, primary_only=False, exclude=None, swap=None):
    """``aggregate_albums`` per row — the cover representative included (the
    first primary in the members' order / the smallest shown id)."""
    if not primary_only:
        swap = None
    if artist:
        counter = s._agg_albums_by_artist.get(artist.lower(), {})
        scope = s._tag_artist.get(artist.lower()) or set()
    elif album_artist:
        counter = s._agg_albums_by_album_artist.get(album_artist.lower(), {})
        scope = s._tag_album_artist.get(album_artist.lower()) or set()
    else:
        counter, scope = s._agg_albums, None
    out = []
    for key, count in counter.items():
        tids = s._tag_album.get(key)
        if not tids:
            continue
        if ((exclude and not exclude.isdisjoint(tids))
                or (swap is not None and not swap.changed.isdisjoint(tids))):
            members = tids if scope is None else (tids & scope)
            ex = exclude or ()
            shown = [x for x in members if x not in ex and (
                _primary(s, x, swap) if (swap is not None or primary_only) else True)]
            count = len(shown)
            if not count:
                continue
            rep_ = min(shown)
        elif primary_only:
            members = tids if scope is None else (tids & scope)
            prims = [x for x in members if _primary(s, x, None)]
            count = len(prims)
            if not count:
                continue
            rep_ = prims[0]
        else:
            rep_ = next(iter(tids))
        t = s._tracks.get(rep_)
        name = (t.get("album") or "").strip() if t else key
        out.append({"album": name or key, "count": count, "track_id": rep_})
    out.sort(key=lambda x: x["album"].lower())
    return out


def _ref_formats(s, primary_only=False, exclude=None, swap=None):
    """``aggregate_formats`` per row — the display spelling included (the last
    member's format when duplicates are hidden)."""
    from soniqboom.core.catalogue import is_package_format
    from soniqboom.core.retro import coarse_family
    if not primary_only:
        swap = None
    out = []
    for key, tids in s._tag_format.items():
        if not key or not tids or is_package_format(key):
            continue
        touched = _ref_bucket_count(s, tids, primary_only, exclude, swap)
        if touched is not None:
            count = touched
            if not count:
                continue
            shown = tids.difference(exclude) if exclude else tids
            t = s._tracks.get(next(iter(shown))) if shown else None
            name = (t.get("format") if t else None) or key
        elif primary_only:
            count, name = 0, key
            for tid in tids:
                t = s._tracks.get(tid)
                if t is None:
                    continue
                name = t.get("format") or name
                if t.get("is_duplicate_primary", True):
                    count += 1
            if not count:
                continue
        else:
            t = s._tracks.get(next(iter(tids)))
            name = (t.get("format") if t else None) or key
            count = len(tids)
        out.append({"format": name, "count": count, "family": coarse_family(name)})
    out.sort(key=lambda x: (-x["count"], x["format"].lower()))
    return out


def _expected_sets(s: TrackStore) -> dict:
    tr = s._tracks
    years: dict = {}
    for tid, t in tr.items():
        y = _agg_year(t)
        if y is not None:
            years.setdefault(y, set()).add(tid)
    return {
        "_nonprimary_ids": {tid for tid, t in tr.items()
                            if not t.get("is_duplicate_primary", True)},
        "_untagged_artist": {tid for tid, t in tr.items() if _untagged(t, "artist")},
        "_untagged_album_artist": {tid for tid, t in tr.items() if _untagged(t, "album_artist")},
        "_untagged_album": {tid for tid, t in tr.items() if _untagged(t, "album")},
        "_untagged_genre": {tid for tid, t in tr.items() if _untagged(t, "genre")},
        "_tag_year": years,
    }


def _assert_sets(s: TrackStore) -> None:
    for attr, want in _expected_sets(s).items():
        assert getattr(s, attr) == want, attr


def _cold(s: TrackStore) -> None:
    """Drop every browse memo — the next call computes from scratch."""
    s.browse_changed()
    s._agg_cache.clear()
    s._agg_x_cache.clear()
    s._primary_swap_memo = None
    s._dup_inv_memo = None
    s._inv_memo = None


def J(x) -> str:
    return json.dumps(x, default=str)


# ── 1. the field lists ─────────────────────────────────────────────────────

def test_browse_exempt_fields_feed_no_browse_surface():
    assert _BROWSE_EXEMPT_FIELDS.isdisjoint(_INDEXED_FIELDS | _BROWSE_EXTRA_FIELDS)
    assert _SEQ_EXEMPT_FIELDS <= _BROWSE_EXEMPT_FIELDS
    assert _SWAP_FIELDS.isdisjoint(_BROWSE_EXEMPT_FIELDS)
    assert "duration" not in _BROWSE_EXEMPT_FIELDS        # (duration-only: ``_duration_seq``)


# ── 2. the browse counter ──────────────────────────────────────────────────

def test_browse_seq_moves_exactly_for_what_a_list_can_show():
    s = TrackStore()
    s.upsert_tracks_batch([_mk(random.Random(1), i) for i in range(6)])
    b, m, d = s._browse_seq, s._mutation_seq, s._duration_seq

    def moved():
        nonlocal b, m, d
        out = (s._browse_seq != b, s._mutation_seq != m, s._duration_seq != d)
        b, m, d = s._browse_seq, s._mutation_seq, s._duration_seq
        return out

    s.update_track_fields("t0000", {"health": "broken", "health_note": "x"})
    assert moved() == (False, True, False)      # skip rules still see it (_mutation_seq)
    s.update_track_fields("t0000", {"tune_lengths": [10, 20], "defect": "corrupt"})
    assert moved() == (False, True, False)
    s.update_track_fields("t0000", {"cover_art": "/api/art/x"})
    assert moved() == (False, False, False)
    s.update_track_fields("t0000", {"duration": 4321.0})
    assert moved() == (False, False, True)      # duration orders key on _duration_seq
    s.update_track_fields("t0000", {"duration": 1234.0, "health": None})
    assert moved() == (False, True, True)       # duration + a field no list reads
    s.update_track_fields("t0000", {"duration": 1235.0, "tune_lengths": [30, 40]})
    assert moved() == (False, True, True)       # (the tune-length backfill)
    s.update_track_fields("t0000", {"start_subsong": 1, "duration": 1236.0})
    assert moved() == (False, True, True)       # (a default-tune probe)
    s.update_track_fields("t0000", {"duration": 1237.0, "title": "renamed"})
    assert moved() == (True, True, False)       # with a field a list reads: a browse write
    for f, v in (("title", "zzz"), ("is_duplicate_primary", False), ("bitrate", 1),
                 ("game_source", "folder"), ("user_edited", ["game"]), ("year", 1977),
                 ("path", "/elsewhere/x")):
        s.update_track_fields("t0001", {f: v})
        assert moved()[0], f
    s.update_track_fields("t0001", {"title": "zzz"})              # no change: no write
    assert moved() == (False, False, False)
    s.update_track_fields_batch([("t0002", {"health": "glitch"}), ("t0003", {"stil": "s"})])
    assert moved() == (False, True, False)
    s.update_track_fields_batch([("t0002", {"health": None}), ("t0003", {"album": "Q"})])
    assert moved()[0]
    s.upsert_track(_mk(random.Random(2), 99))
    assert moved()[0]
    s.delete_track_ids(["t0099"])
    assert moved()[0]
    s.enter_batch_mode()
    s.upsert_tracks_batch([_mk(random.Random(3), 98)])
    moved()
    s.exit_batch_mode()                          # the sorted lists are replaced
    assert moved()[0]


def test_legends_stay_warm_across_writes_no_legend_reads(monkeypatch):
    s = TrackStore()
    s.upsert_tracks_batch(_library(random.Random(5), 80))
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    genres = s.aggregate_genres(True)
    library._cache_set("genres", genres)
    s.update_track_fields("t0001", {"health": "broken"})
    s.update_track_fields("t0002", {"duration": 999.0})
    assert s.aggregate_genres(True) is genres                    # store memo kept
    assert library._cache_get("genres") is genres                # HTTP legend cache kept
    s.update_track_fields("t0003", {"genre": ["Brand New"]})
    assert library._cache_get("genres") is None
    assert "Brand New" in {g["genre"] for g in s.aggregate_genres(True)}


# ── 3. the maintained sets follow every write path ─────────────────────────

def test_browse_sets_follow_every_write_path():
    r = random.Random(7)
    s = TrackStore()
    s.upsert_tracks_batch(_library(r, 150))
    _assert_sets(s)
    for step in range(60):
        ids = sorted(s._tracks)
        op = r.choice(["upd", "batch", "add", "del", "batch_mode", "rebuild"])
        if op == "upd":
            tid = r.choice(ids)
            f = r.choice(["artist", "album_artist", "album", "genre", "genre_derived", "year",
                          "is_duplicate_primary", "health"])
            v = {"artist": r.choice(ARTISTS), "album_artist": r.choice(ARTISTS),
                 "album": r.choice(ALBUMS), "genre": copy.deepcopy(r.choice(GENRES)),
                 "genre_derived": r.choice([["Game"], None]), "year": _year(r),
                 "is_duplicate_primary": r.choice([True, False, None, 0]),
                 "health": "x"}[f]
            s.update_track_fields(tid, {f: v})
        elif op == "batch":
            s.update_track_fields_batch([(t, {"year": _year(r), "artist": r.choice(ARTISTS)})
                                         for t in r.sample(ids, 5)])
        elif op == "add":
            s.upsert_tracks_batch([_mk(r, 1000 + step * 10 + j) for j in range(3)])
        elif op == "del":
            s.delete_track_ids(r.sample(ids, 3))
        elif op == "batch_mode":
            s.enter_batch_mode()
            s.upsert_tracks_batch([_mk(r, int(t[1:])) for t in r.sample(ids, 4)])
            _assert_sets(s)                                     # eager inside a batch too
            s.exit_batch_mode()
        else:
            s.rebuild_indexes()
        _assert_sets(s)
    assert s.verify_indexes()["index_ok"]


# ── 4. seeded randomized: pages / counts / legends vs brute force ──────────

def _check_everything(s: TrackStore, r: random.Random, un, tag: str, n_pages: int) -> None:
    preds_pool = [{}, {"genre": "chiptune"}, {"artist": "rob hubbard"}, {"format_": "SID"},
                  {"year_min": 1990}, {"untagged": "artist"}, {"untagged": "genre"},
                  {"query": "ma"}, {"query": "the"}, {"game": "uridium"}, {"album": "commando"},
                  {"untagged": "album", "format_": "sid"}]
    swap = s.primary_swap(un)
    for dedup in (False, True):
        for mode, ex in (("mark", None), ("hide", un)):
            sw = swap if dedup else None
            # counts against brute force
            vis = {t for t in s._tracks
                   if not (ex and t in ex) and (not dedup or _primary(s, t, sw))}
            assert s.visible_track_count(dedup, ex, sw) == len(vis), (tag, dedup, mode)
            for f in ("artist", "album_artist", "album", "genre"):
                want = sum(1 for t in vis if _untagged(s._tracks[t], f))
                assert s.untagged_count(f, dedup, ex, sw) == want, (tag, f, dedup, mode)
            years: dict = {}
            for t in vis:
                y = _agg_year(s._tracks[t])
                if y is not None and (dedup or ex):
                    years[y] = years.get(y, 0) + 1
            if dedup or ex:
                assert {d["year"]: d["count"] for d in s.aggregate_years(dedup, ex, sw)} == years
            genres: dict = {}
            for t in vis:
                for g in track_genres(s._tracks[t]):
                    k = g.strip().lower()
                    if k:
                        genres[k] = genres.get(k, 0) + 1
            assert {d["genre"].lower(): d["count"]
                    for d in s.aggregate_genres(dedup, ex, sw)} == genres, (tag, dedup, mode)
            # albums (cover representative) and formats (display spelling)
            # byte for byte against the per-row reference
            assert J(s.aggregate_formats(dedup, ex, sw)) == J(_ref_formats(s, dedup, ex, sw)), \
                (tag, dedup, mode)
            for scope in ({}, {"artist": "rob hubbard"}, {"album_artist": "a"}):
                assert J(s.aggregate_albums(primary_only=dedup, exclude=ex, swap=sw, **scope)) == \
                    J(_ref_albums(s, primary_only=dedup, exclude=ex, swap=sw, **scope)), \
                    (tag, scope, dedup, mode)
            # every legend: warm (memoised, maybe from before a write no list
            # reads) == cold
            for agg in ("aggregate_artists", "aggregate_album_artists", "aggregate_genres",
                        "aggregate_years", "aggregate_formats", "aggregate_scene_group"):
                warm = J(getattr(s, agg)(dedup, ex, sw))
                _cold(s)
                sw = s.primary_swap(un) if dedup else None
                assert J(getattr(s, agg)(dedup, ex, sw)) == warm, (tag, agg, dedup, mode)
            for scope in ({}, {"artist": "rob hubbard"}, {"album_artist": "a"}):
                warm = J(s.aggregate_albums(primary_only=dedup, exclude=ex, swap=sw, **scope))
                _cold(s)
                sw = s.primary_swap(un) if dedup else None
                assert J(s.aggregate_albums(primary_only=dedup, exclude=ex, swap=sw,
                                            **scope)) == warm, (tag, scope, dedup, mode)
    swap = s.primary_swap(un)
    # pages: a sweep, every page equal to the brute-force list's slice
    for _ in range(n_pages):
        dedup = r.random() < 0.6
        ex = un if r.random() < 0.5 else None
        sw = swap if dedup else None
        preds = r.choice(preds_pool)
        sort_by, sort_order = r.choice(SORTS), r.choice(ORDERS)
        full = _expected_page_ids(s, preds, dedup, ex, sw, sort_by, sort_order)
        step = r.choice([1, 7, 40])
        offs = ([r.randint(0, len(full) + 3) for _ in range(3)]
                + list(range(0, len(full) + step, step)))
        for off in offs:
            got = [t["id"] for t in s.filter_tracks(limit=step, offset=off, filter_duplicates=dedup,
                                                    exclude=ex, swap=sw, sort_by=sort_by,
                                                    sort_order=sort_order, **preds)]
            assert got == full[off:off + step], (tag, preds, dedup, ex is not None,
                                                 sort_by, sort_order, off, step)


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_seeded_random_writes_pages_counts_and_legends_match_brute_force(monkeypatch, seed):
    r = random.Random(seed)
    # Small thresholds so the deep-page memos and both candidate paths run.
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", r.choice([5, 20]))
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", r.choice([0, 15, 60]))
    monkeypatch.setattr(store_mod, "_FILTER_WALK_STEP", r.choice([1, 3, 16]))
    s = TrackStore()
    s.upsert_tracks_batch(_library(r, 220))
    dead = {"r1"}

    def unplayable():
        return frozenset().union(*(s._tag_scan_root_hash.get(h, set()) for h in dead))
    un = unplayable()
    _check_everything(s, r, un, "init", 25)
    nxt = 5000
    for step in range(25):
        ids = sorted(s._tracks)
        op = r.choice(["add", "del", "upd", "batch", "dup", "root", "reupsert", "health", "dur"])
        if op == "add":
            s.upsert_tracks_batch([_mk(r, nxt + j) for j in range(r.randint(1, 6))])
            nxt += 10
        elif op == "del":
            s.delete_track_ids(r.sample(ids, 4))
        elif op == "upd":
            for tid in r.sample(ids, 4):
                f = r.choice(["title", "artist", "album", "genre", "year", "format", "added_at",
                              "bitrate", "is_duplicate_primary", "duplicate_group_id"])
                v = {"title": r.choice(TITLES), "artist": r.choice(ARTISTS),
                     "album": r.choice(ALBUMS), "genre": copy.deepcopy(r.choice(GENRES)),
                     "year": _year(r), "format": r.choice(FORMATS),
                     "added_at": r.randint(0, 400), "bitrate": r.choice([1, 2000]),
                     "is_duplicate_primary": r.choice([True, False]),
                     "duplicate_group_id": r.choice([None, "g0", "g7"])}[f]
                s.update_track_fields(tid, {f: v})
        elif op == "batch":
            s.update_track_fields_batch([(t, {"album": r.choice(ALBUMS), "duration": 3.0})
                                         for t in r.sample(ids, 6)])
        elif op == "dup":
            groups = [g for g, m in s._tag_dup_group.items() if len(m) > 1]
            if groups:
                members = sorted(s._tag_dup_group[r.choice(groups)])
                win = r.choice(members)
                s.update_track_fields_batch([(m, {"is_duplicate_primary": m == win})
                                             for m in members])
        elif op == "root":
            dead ^= {r.choice(ROOTS)}
        elif op == "reupsert":
            s.upsert_tracks_batch([_mk(r, int(t[1:])) for t in r.sample(ids, 3)])
        elif op == "health":
            s.update_track_fields(r.choice(ids), {"health": r.choice(["broken", None])})
        else:
            s.update_track_fields(r.choice(ids), {"duration": float(r.randint(1, 900))})
        new_un = unplayable()
        if new_un != un:
            un = new_un
        _assert_sets(s)
        _check_everything(s, r, un, f"step{step}:{op}", 8)


# ── 5. memoised == cold, also inside a batch section ───────────────────────

def test_memoised_pages_equal_cold_pages_during_a_batch(monkeypatch):
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 10)
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(store_mod, "_FILTER_WALK_STEP", 4)
    r = random.Random(3)
    s = TrackStore()
    s.upsert_tracks_batch(_library(r, 200))
    un = frozenset(s._tag_scan_root_hash.get("r2", set()))
    calls = [dict(filter_duplicates=d, sort_by=sb, sort_order=so, exclude=ex, **p)
             for d in (False, True) for sb in ("title", None, "year")
             for so in ("asc", "desc") for ex in (None, un)
             for p in ({}, {"genre": "chiptune"}, {"query": "ma"}, {"year_min": 1990})]

    def pages():
        out = []
        sw = s.primary_swap(un)
        for kw in calls:
            for off in (0, 13, 40, 90):
                out.append([t["id"] for t in s.filter_tracks(
                    limit=17, offset=off, swap=sw if kw["filter_duplicates"] else None, **kw)])
        return out
    pages()                                      # warm every memo
    s.enter_batch_mode()                         # deferred lists: stale until the exit
    s.upsert_tracks_batch([_mk(r, 900 + j) for j in range(5)])
    s.delete_track_ids(sorted(s._tracks)[:5])
    s.update_track_fields_batch([(t, {"title": "Aaa", "is_duplicate_primary": False})
                                 for t in sorted(s._tracks)[10:14]])
    warm = pages()
    _cold(s)
    assert pages() == warm
    s.exit_batch_mode()
    warm = pages()
    _cold(s)
    assert pages() == warm


def test_two_exclude_sets_in_turn_never_share_a_memo(monkeypatch):
    # (The web's Hide set and the Subsonic catalogue exclusion are two callers
    # with their own sets, used in turn, with no write in between.)
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 6)
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(store_mod, "_FILTER_WALK_STEP", 3)
    r = random.Random(14)
    s = TrackStore()
    s.upsert_tracks_batch(_library(r, 250))
    ex1 = frozenset(s._tag_scan_root_hash.get("r0", set()))
    ex2 = frozenset(s._tag_scan_root_hash.get("r3", set()) | {"gone"})
    sw = s.primary_swap(frozenset(s._tag_scan_root_hash.get("r1", set())))
    for _ in range(3):
        for ex in (ex1, ex2, None):
            for dedup in (True, False):
                w = sw if dedup else None
                vis = {t for t in s._tracks
                       if not (ex and t in ex) and (not dedup or _primary(s, t, w))}
                assert s.visible_track_count(dedup, ex, w) == len(vis)
                assert s.untagged_count("artist", dedup, ex, w) == sum(
                    1 for t in vis if _untagged(s._tracks[t], "artist"))
                for preds in ({}, {"genre": "chiptune"}):
                    want = _expected_page_ids(s, preds, dedup, ex, w, "title", None)
                    for off in (0, 8, 30):
                        got = [t["id"] for t in s.filter_tracks(
                            limit=9, offset=off, filter_duplicates=dedup, exclude=ex, swap=w,
                            sort_by="title", **preds)]
                        assert got == want[off:off + 9], (ex is ex1, dedup, preds, off)


# ── 6. the duplicate stand-ins, incrementally ──────────────────────────────

def test_incremental_primary_swap_equals_a_full_build():
    r = random.Random(9)
    s = TrackStore()
    s.upsert_tracks_batch(_library(r, 300))
    un = frozenset(s._tag_scan_root_hash.get("r0", set()) | s._tag_scan_root_hash.get("r3", set()))
    assert s.primary_swap(un) is not None
    for step in range(40):
        ids = sorted(s._tracks)
        groups = sorted(g for g, m in s._tag_dup_group.items() if len(m) > 1)
        op = r.choice(["flip", "regroup", "fields", "add", "del", "other"])
        if op == "flip" and groups:
            members = sorted(s._tag_dup_group[r.choice(groups)])
            win = r.choice(members)
            s.update_track_fields_batch([(m, {"is_duplicate_primary": m == win}) for m in members])
        elif op == "regroup":
            s.update_track_fields(r.choice(ids), {"duplicate_group_id": r.choice(groups or ["g1"])})
        elif op == "fields":
            f = r.choice(["bitrate", "format", "added_at"])
            s.update_track_fields(r.choice(ids), {f: {"bitrate": r.choice([1, 999, None]),
                                                      "format": r.choice(FORMATS),
                                                      "added_at": r.randint(0, 999)}[f]})
        elif op == "add":
            g = r.choice(groups or ["gx"])
            s.upsert_track(_mk(r, 7000 + step, duplicate_group_id=g,
                               is_duplicate_primary=r.random() < 0.3))
        elif op == "del":
            s.delete_track_ids(r.sample(ids, 2))
        else:
            s.update_track_fields(r.choice(ids), {"health": "x", "cover_art": str(step)})
        inc = s.primary_swap(un)
        full = TrackStore()
        full._tracks = s._tracks
        full._tag_dup_group = s._tag_dup_group
        ref = full.primary_swap(un)
        assert (inc.stand_in if inc else None) == (ref.stand_in if ref else None), (step, op)
    # unchanged → the very same object (memos keyed on it stay valid)
    a = s.primary_swap(un)
    s.update_track_fields(sorted(s._tracks)[0], {"health": "y"})
    assert s.primary_swap(un) is a


# ── 7. resumed walks ───────────────────────────────────────────────────────

@pytest.mark.parametrize("step", [1, 5, 2048])
def test_a_walk_resumes_and_is_dropped_by_what_changes_its_order(monkeypatch, step):
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(store_mod, "_FILTER_WALK_STEP", step)
    r = random.Random(4)
    s = TrackStore()
    s.upsert_tracks_batch(_library(r, 400))
    kw = dict(genre="chiptune", filter_duplicates=True, sort_by="title")
    full = _expected_page_ids(s, {"genre": "chiptune"}, True, None, None, "title", None)
    assert len(full) > 60
    got = []
    for off in range(0, len(full), 9):
        got += [t["id"] for t in s.filter_tracks(limit=9, offset=off, **kw)]
    assert got == full
    down = [t["id"] for off in range(0, len(full), 4)
            for t in s.filter_tracks(limit=4, offset=off, sort_order="desc", **kw)]
    assert down == full[::-1]
    s._filter_walk_memo.pop(next(k for k in s._filter_walk_memo if k[-1]))
    assert len(s._filter_walk_memo) == 1
    walk = next(iter(s._filter_walk_memo.values()))
    assert walk.out == full and walk.done
    # a deep page first, then a shallow one: same walk, same rows
    s.browse_changed()
    assert [t["id"] for t in s.filter_tracks(limit=5, offset=50, **kw)] == full[50:55]
    assert [t["id"] for t in s.filter_tracks(limit=5, offset=3, **kw)] == full[3:8]
    walk = next(iter(s._filter_walk_memo.values()))
    # a health mark keeps it; a title change drops it
    s.update_track_fields(full[0], {"health": "broken"})
    s.filter_tracks(limit=5, offset=0, **kw)
    assert next(iter(s._filter_walk_memo.values())) is walk
    s.update_track_fields(full[0], {"title": "Zzzz last"})
    page = [t["id"] for t in s.filter_tracks(limit=5, offset=0, **kw)]
    assert next(iter(s._filter_walk_memo.values())) is not walk
    assert page == _expected_page_ids(s, {"genre": "chiptune"}, True, None, None, "title", None)[:5]
    # another exclude set (by identity) is another order
    ex = frozenset(full[:3])
    assert [t["id"] for t in s.filter_tracks(limit=5, offset=0, exclude=ex, **kw)] == \
        [t for t in _expected_page_ids(s, {"genre": "chiptune"}, True, None, None, "title",
                                       None) if t not in ex][:5]


def test_a_duration_backfill_drops_only_the_duration_orders(monkeypatch):
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    r = random.Random(8)
    s = TrackStore()
    s.upsert_tracks_batch(_library(r, 300))
    s.filter_tracks(genre="chiptune", sort_by="title", limit=5)
    asc = [t["id"] for t in s.filter_tracks(genre="chiptune", sort_by="duration", limit=400)]
    assert asc == _expected_page_ids(s, {"genre": "chiptune"}, False, None, None,
                                     "duration", None)[:400]
    moved = asc[0]
    s.update_track_fields(moved, {"duration": 54321.0})       # (duration-only: no browse bump)
    asc = [t["id"] for t in s.filter_tracks(genre="chiptune", sort_by="duration", limit=400)]
    assert asc == _expected_page_ids(s, {"genre": "chiptune"}, False, None, None,
                                     "duration", None)[:400]
    assert asc[0] != moved
    for deep in (False, True):                 # the deduped All-Tracks deep orders as well
        monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 3 if deep else 5000)
        want = _expected_page_ids(s, {}, True, None, None, "duration", None)
        assert [t["id"] for t in s.filter_tracks(limit=20, offset=3, sort_by="duration",
                                                 filter_duplicates=True)] == want[3:23]
        tid = want[4]
        s.update_track_fields(tid, {"duration": 0.5})
        want = _expected_page_ids(s, {}, True, None, None, "duration", None)
        assert [t["id"] for t in s.filter_tracks(limit=20, offset=3, sort_by="duration",
                                                 filter_duplicates=True)] == want[3:23]
    s.filter_tracks(genre="chiptune", sort_by="duration", limit=5)
    by_attr = {k[-2]: w for k, w in s._filter_walk_memo.items()}
    tid = next(iter(s._tag_genre["chiptune"]))
    s.update_track_fields(tid, {"duration": 12345.0})
    s.filter_tracks(genre="chiptune", sort_by="title", limit=5)
    got = [t["id"] for t in s.filter_tracks(genre="chiptune", sort_by="duration",
                                            sort_order="desc", limit=400)]
    assert got == _expected_page_ids(s, {"genre": "chiptune"}, False, None, None,
                                     "duration", "desc")[:400]
    assert tid in got
    now = {k[-2]: w for k, w in s._filter_walk_memo.items() if not k[-1]}
    assert now["_sorted_title"] is by_attr["_sorted_title"]


def test_the_walk_memo_is_bounded(monkeypatch):
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(store_mod, "_FILTER_WALK_MAX", 5)
    s = TrackStore()
    s.upsert_tracks_batch(_library(random.Random(2), 200))
    for i in range(12):
        s.filter_tracks(query=["ma", "the", "so", "co", "mu", "po"][i % 6],
                        sort_by=SORTS[i % len(SORTS)], limit=3)
    assert len(s._filter_walk_memo) <= 5
    monkeypatch.setattr(store_mod, "_FILTER_WALK_MAX_IDS", 10)
    s.filter_tracks(genre="chiptune", limit=3)
    assert len(s._filter_walk_memo) == 1        # (one oversized order is still kept)


# ── 8. reads off the event-loop thread ─────────────────────────────────────

def test_reads_from_another_thread_keep_no_shared_state(monkeypatch):
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 5)
    s = TrackStore()
    s.upsert_tracks_batch(_library(random.Random(6), 300))
    un = frozenset(s._tag_scan_root_hash.get("r1", set()))
    calls = [dict(genre="chiptune", filter_duplicates=True), dict(filter_duplicates=True),
             dict(filter_duplicates=False, exclude=un), dict(query="ma", sort_by="year")]
    out: dict = {}

    s.update_track_fields(sorted(s._tracks)[0], {"title": "Zyzzyva newword"})
    assert s._word_list_dirty
    words_before = s._word_list

    def worker():
        sw = s.primary_swap(un)
        out["rows"] = [[t["id"] for t in s.filter_tracks(limit=10, offset=20,
                        swap=sw if kw.get("filter_duplicates") else None, **kw)] for kw in calls]
        out["agg"] = [s.aggregate_genres(True, un, sw), s.untagged_count("artist", True, un, sw),
                      s.visible_track_count(True, un, sw)]
        out["memos"] = (dict(s._filter_walk_memo), dict(s._primary_order_memo),
                        dict(s._excluded_order_memo), s._primary_swap_memo, s._dup_inv_memo,
                        s._inv_memo, dict(s._agg_cache), dict(s._agg_x_cache))
        out["words"] = (s._word_list is words_before, s._word_list_dirty)
    th = threading.Thread(target=worker)
    th.start()
    th.join()
    # nothing kept off the owner thread, the shared word list untouched
    assert out["memos"] == ({}, {}, {}, None, None, None, {}, {})
    assert out["words"] == (True, True)
    sw = s.primary_swap(un)
    assert out["rows"] == [[t["id"] for t in s.filter_tracks(
        limit=10, offset=20, swap=sw if kw.get("filter_duplicates") else None, **kw)]
        for kw in calls]
    assert J(out["agg"]) == J([s.aggregate_genres(True, un, sw),
                               s.untagged_count("artist", True, un, sw),
                               s.visible_track_count(True, un, sw)])
    assert s._filter_walk_memo and s._agg_x_cache   # the owner keeps its memos


# ── 9. the word list is re-sorted only when the vocabulary changes ─────────

def test_reupserting_the_same_words_keeps_the_word_list_sorted():
    s = TrackStore()
    rows = _library(random.Random(1), 50)
    s.upsert_tracks_batch(copy.deepcopy(rows))
    s._rebuild_word_list()
    s.upsert_tracks_batch(copy.deepcopy(rows[:10]))          # same words: no change
    assert s._word_list_dirty is False
    assert s._word_list == sorted(s._word_index)
    s.upsert_track(dict(rows[0], title="Xylophonic"))
    assert s._word_list_dirty is True
    s._rebuild_word_list()
    assert s._word_list == sorted(s._word_index)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_merged_word_list_is_always_the_sorted_vocabulary(seed):
    r = random.Random(seed)
    s = TrackStore()
    s.upsert_tracks_batch(_library(r, 120))
    words = ["alpha", "beta", "gamma", "Ωmega", "zeta", "aa", "zz", "mid"]
    for step in range(200):
        ids = sorted(s._tracks)
        op = r.choice(["title", "del", "add", "rebuild", "clear", "noop"])
        if op == "title":
            s.update_track_fields(r.choice(ids), {"title": " ".join(r.sample(words, 2))
                                                  + f" w{r.randint(0, 30)}"})
        elif op == "del":
            s.delete_track_ids(r.sample(ids, 2))
        elif op == "add":
            s.upsert_tracks_batch([_mk(r, 3000 + step * 3 + j) for j in range(2)])
        elif op == "rebuild":
            s.rebuild_indexes()
        elif op == "clear":
            s.browse_changed()
        if r.random() < 0.5:
            s._rebuild_word_list()
            assert s._word_list == sorted(s._word_index), (step, op)
        if not s._word_list_dirty:
            assert s._word_list == sorted(s._word_index), (step, op)


# ── 10. a library loaded over another one (Backup → Import, Stop / Start) ──

def _warm_everything(s: TrackStore, un) -> None:
    sw = s.primary_swap(un)
    for dedup in (False, True):
        w = sw if dedup else None
        for ex in (None, un):
            s.visible_track_count(dedup, ex, w)
            for f in ("artist", "album_artist", "album", "genre"):
                s.untagged_count(f, dedup, ex, w)
            for agg in ("aggregate_artists", "aggregate_album_artists", "aggregate_genres",
                        "aggregate_years", "aggregate_formats", "aggregate_scene_group"):
                getattr(s, agg)(dedup, ex, w)
            s.aggregate_albums(primary_only=dedup, exclude=ex, swap=w)
            for preds in ({}, {"genre": "chiptune"}, {"query": "ma"}, {"untagged": "artist"}):
                s.filter_track_ids(filter_duplicates=dedup, swap=w, **preds)
                for sort_by in ("title", None, "year"):
                    for off in (0, 12, 40):
                        s.filter_tracks(limit=10, offset=off, filter_duplicates=dedup, exclude=ex,
                                        swap=w, sort_by=sort_by, **preds)
    s.primary_track_count()
    for sort_by in ("title", "duration"):            # (the no-swap deduped deep orders)
        s.filter_tracks(limit=10, offset=40, filter_duplicates=True, sort_by=sort_by)


@pytest.mark.parametrize("via", ["populate_store", "bulk_load+rebuild_indexes"])
def test_importing_an_a_only_backup_over_a_and_b_shows_a_only(monkeypatch, via):
    from soniqboom.core import persistence, startup_status
    monkeypatch.setattr(startup_status, "set_phase", lambda *a, **k: None)
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 5)
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(store_mod, "_FILTER_WALK_STEP", 4)
    r = random.Random(21)
    a_rows = _library(r, 160)
    b_rows = []
    i = 5000
    while len(b_rows) < 140:        # B: other ids, the same genres / artists / groups
        b_rows.append(_mk(r, i, duplicate_group_id=f"g{r.randint(0, 40)}",
                          is_duplicate_primary=r.random() < 0.4)
                      if r.random() < 0.3 else _mk(r, i))
        i += 1
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    s.upsert_tracks_batch(copy.deepcopy(a_rows + b_rows))
    for t in b_rows[:20]:
        s.record_play(t["id"], uid="")
        s.set_rating(t["id"], 5)
    un = frozenset(s._tag_scan_root_hash.get("r1", set()))          # (kept: the same object)
    _warm_everything(s, un)
    seqs = (s._mutation_seq, s._catalog_seq, s._duration_seq, s._play_seq, s._rating_seq,
            s._browse_seq, s.index_generation())
    state = {"tracks": {t["id"]: copy.deepcopy(t) for t in a_rows}, "waveforms": {},
             "ratings": {}, "play_stats": {}, "playlists": {}, "history": [],
             "scan_dirs": {}, "hash_lookups": {}, "config": {}}
    if via == "populate_store":
        persistence.populate_store(state)
    else:
        s.bulk_load(**state)
        s.rebuild_indexes()
    a_ids = {t["id"] for t in a_rows}
    b_ids = {t["id"] for t in b_rows}
    assert set(s._tracks) == a_ids
    now = (s._mutation_seq, s._catalog_seq, s._duration_seq, s._play_seq, s._rating_seq,
           s._browse_seq, s.index_generation())
    assert all(n != o for n, o in zip(now, seqs)), (seqs, now)   # every consumer sees it
    assert s._agg_x_cache == {} or all(e[0] == s._browse_seq for e in s._agg_x_cache.values())
    # Every answer from the warm memos' keys is A's: against brute force
    # (pages, counts, legends — warm == cold too), and no B id anywhere.
    _check_everything(s, r, un, "after import", 30)
    sw = s.primary_swap(un)
    for dedup in (False, True):
        w = sw if dedup else None
        for ex in (None, un):
            for agg in ("aggregate_artists", "aggregate_genres", "aggregate_years",
                        "aggregate_formats"):
                for row in getattr(s, agg)(dedup, ex, w):
                    assert row["count"] > 0
            for row in s.aggregate_albums(primary_only=dedup, exclude=ex, swap=w):
                assert row["track_id"] in a_ids
            for preds in ({}, {"genre": "chiptune"}, {"query": "ma"}, {"untagged": "artist"}):
                assert not set(s.filter_track_ids(filter_duplicates=dedup, swap=w, **preds)) & b_ids
                for sort_by in ("title", None, "year"):
                    for off in (0, 12, 40):
                        page = s.filter_tracks(limit=10, offset=off, filter_duplicates=dedup,
                                               exclude=ex, swap=w, sort_by=sort_by, **preds)
                        assert not {t["id"] for t in page} & b_ids
    assert s.visible_track_count() == len(a_ids)
    assert s.primary_track_count() == sum(1 for t in s._tracks.values()
                                          if t.get("is_duplicate_primary", True))
    assert s.verify_indexes()["index_ok"]


def test_a_full_rebuild_alone_invalidates_every_memo(monkeypatch):
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 5)
    s = TrackStore()
    s.upsert_tracks_batch(_library(random.Random(22), 120))
    un = frozenset(s._tag_scan_root_hash.get("r2", set()))
    _warm_everything(s, un)
    assert s._agg_x_cache and s._agg_cache and s._filter_walk_memo
    assert s._primary_order_memo and s._excluded_order_memo and s._primary_swap_memo
    m, b = s._mutation_seq, s._browse_seq
    s.rebuild_indexes()
    assert s._mutation_seq != m and s._browse_seq != b
    assert s._agg_x_cache == {} and s._agg_cache == {} and s._filter_walk_memo == {}
    assert s._primary_order_memo == {} and s._excluded_order_memo == {}
    assert s._primary_swap_memo is None


# ── 11. what changes an order with no write of its own ─────────────────────

def _rock(n=300):
    return [{"id": f"t{i:04d}", "title": f"Title {i:04d}", "artist": "A", "genre": ["Rock"],
             "year": 1990 + (i % 3), "added_at": i, "scan_root_hash": "r0" if i % 2 else "r1",
             **({"duplicate_group_id": f"g{i // 2}", "is_duplicate_primary": i % 2 == 0}
                if i < 40 else {})} for i in range(n)]


def test_another_swap_with_no_write_is_another_order(monkeypatch):
    # A drive coming back (another unplayable set → another swap object) with
    # no track write: the walk of the old stand-ins must not be served.
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    s = TrackStore()
    s.upsert_tracks_batch(_rock())
    sw1 = s.primary_swap(frozenset(s._tag_scan_root_hash["r1"]))    # the primaries offline
    assert sw1 is not None
    first = [t["id"] for t in s.filter_tracks(genre="rock", filter_duplicates=True, swap=sw1,
                                              sort_by="title", limit=10)]
    assert first == _expected_page_ids(s, {"genre": "rock"}, True, None, sw1, "title", None)[:10]
    for un in (frozenset({"zz"}), frozenset(s._tag_scan_root_hash["r0"])):
        sw = s.primary_swap(un)
        got = [t["id"] for t in s.filter_tracks(genre="rock", filter_duplicates=True, swap=sw,
                                                sort_by="title", limit=10)]
        assert got == _expected_page_ids(s, {"genre": "rock"}, True, None, sw, "title", None)[:10]
        assert got != first


def test_a_year_walk_made_inside_a_batch_is_not_served_after_its_exit(monkeypatch):
    # The year predicate reads the deferred ``_sorted_year``; the exit replaces
    # only that list (a field-update batch), not the title order the walk uses.
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    s = TrackStore()
    s.upsert_tracks_batch(_rock())
    s.enter_batch_mode()
    s.update_track_fields_batch([(f"t{i:04d}", {"year": 2005}) for i in range(0, 300, 5)])
    s.filter_tracks(year_min=1990, year_max=1990, sort_by="title", limit=10)   # stale year list
    title_list = s._sorted_title
    s.exit_batch_mode()
    assert s._sorted_title is title_list             # (only the year list was replaced)
    got = [t["id"] for t in s.filter_tracks(year_min=1990, year_max=1990, sort_by="title",
                                            limit=10)]
    want = [tid for _v, tid in s._sorted_title if s._tracks[tid].get("year") == 1990][:10]
    assert got == want


def test_a_derived_genre_alone_moves_a_track_out_of_no_genre():
    s = TrackStore()
    rows = _rock(20)
    rows[3]["genre"] = []
    s.upsert_tracks_batch(rows)
    assert s.untagged_count("genre") == 1
    assert [t["id"] for t in s.filter_tracks(untagged="genre")] == ["t0003"]
    s.update_track_fields("t0003", {"genre_derived": ["Chiptune"]})
    assert s.untagged_count("genre") == 0
    assert s.filter_tracks(untagged="genre") == []
    s.update_track_fields("t0003", {"genre_derived": None})
    assert s.untagged_count("genre") == 1


def test_display_spellings_and_cover_picks_follow_the_members_order():
    # (Deterministic whatever the hash seed: a two-spelling format bucket has a
    # first member different from its last; 40 two-track albums can't all have
    # their first primary equal to their smallest id but by chance 2**-40.)
    s = TrackStore()
    rows = [{"id": "fa", "title": "a", "format": "SID", "added_at": 1},
            {"id": "fb", "title": "b", "format": "sid", "added_at": 2}]
    for k in range(40):
        rows += [{"id": f"x{k}a", "title": "t", "album": f"Album {k}", "format": "MP3",
                  "added_at": 3},
                 {"id": f"x{k}b", "title": "t", "album": f"Album {k}", "format": "MP3",
                  "added_at": 4}]
    rows += [{"id": "dup1", "title": "d", "format": "MP3", "duplicate_group_id": "gd",
              "is_duplicate_primary": False, "added_at": 5}]
    s.upsert_tracks_batch(rows)
    for dedup in (False, True):
        assert J(s.aggregate_formats(dedup)) == J(_ref_formats(s, dedup)), dedup
        assert J(s.aggregate_albums(primary_only=dedup)) == J(_ref_albums(s, primary_only=dedup))


def test_a_duration_write_with_fields_no_list_reads_keeps_the_browse_memos(monkeypatch):
    """``{duration, tune_lengths}`` (repair.fill_tune_lengths) and
    ``{start_subsong, duration}`` (the default-tune probe) move
    ``_duration_seq``, not ``_browse_seq``: the legends and the non-duration
    orders stay memoised, the duration orders follow the new lengths."""
    monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    r = random.Random(11)
    s = TrackStore()
    s.upsert_tracks_batch(_library(r, 300))
    artists = s.aggregate_artists()
    s.filter_tracks(genre="chiptune", sort_by="title", limit=5)
    s.filter_tracks(genre="chiptune", sort_by="duration", limit=5)
    title_walk = {k[-2]: w for k, w in s._filter_walk_memo.items()}["_sorted_title"]
    b, m, c, d = s._browse_seq, s._mutation_seq, s._catalog_seq, s._duration_seq
    ids = sorted(s._tag_genre["chiptune"])[:6]
    s.update_track_fields_batch([(tid, {"duration": 9000.0 + i, "tune_lengths": [1, 2]})
                                 for i, tid in enumerate(ids[:3])])
    s.update_track_fields(ids[3], {"start_subsong": 2, "duration": 0.25})
    assert s._browse_seq == b and s._duration_seq == d + 2
    assert s._mutation_seq == m + 2 and s._catalog_seq == c + 2   # (skip rules, Subsonic)
    assert s.aggregate_artists() is artists                        # no recompute
    s.filter_tracks(genre="chiptune", sort_by="title", limit=5)
    assert {k[-2]: w for k, w in s._filter_walk_memo.items()}["_sorted_title"] is title_walk
    for order in (None, "desc"):
        got = [t["id"] for t in s.filter_tracks(genre="chiptune", sort_by="duration",
                                                sort_order=order, limit=400)]
        assert got == _expected_page_ids(s, {"genre": "chiptune"}, False, None, None,
                                         "duration", order)[:400]
    assert s.verify_indexes()["index_ok"]
