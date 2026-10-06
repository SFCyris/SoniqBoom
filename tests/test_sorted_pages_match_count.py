# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sorted track pages reach every track the list's count includes.

``GET /api/tracks?sort=<key>`` pages through the store's sorted indexes, and the
desktop All Tracks list sizes itself from ``GET /api/tracks/count``
(``visible_track_count``).  A track with no year / duration / bpm / added_at
used to be left out of that key's index: the count included it, no page
reached it, and the list ended in skeleton rows that never filled.

Now every track is in every sort index: no year / duration / bpm sorts after
every value (last ascending, first descending — as a blank title or artist
does), no added_at as the oldest.  Checked for every allowed sort key and
order, with and without hidden duplicates and the "Tracks on unavailable
drives: Hide" exclude set, over every page path (index slice, shallow walk,
memoised deep-page orders, candidate sort / walk), every index build path
(incremental, full rebuild, the scanner's yielding batch exit, field updates)
and through the HTTP API.
"""
from __future__ import annotations

import bisect
import copy
import hashlib
import math

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import tracks as tracks_api
from soniqboom.core import availability, remote_cache, scanner
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore, normalise_year
from soniqboom.models.track import Track

SORT_KEYS = sorted(tracks_api._ALLOWED_SORT_KEYS) + [None]
ORDERS = [None, "asc", "desc"]
NUMERIC = ("_sorted_year", "_sorted_added_at", "_sorted_added_at_primary",
           "_sorted_duration", "_sorted_bpm")


def _rows(n: int = 120) -> list[dict]:
    """Tracks with every kind of missing sort value: no / None year, a
    YYYYMMDD year, no / zero / NaN duration, bpm on a third only, no / zero /
    None added_at, blank title / artist / album / format, hidden duplicates."""
    rows = []
    for i in range(n):
        r = {"id": f"t{i:03d}", "path": f"/m/d{i % 4}/{i}.mod",
             "title": "" if i % 13 == 0 else f"Song {(i * 37) % n:03d}",
             "artist": "" if i % 9 == 0 else f"Artist {i % 6}",
             "album_artist": f"AA {i % 3}" if i % 2 else "",
             "album": "" if i % 8 == 0 else f"Album {i % 5}",
             "format": "" if i % 17 == 0 else ["SID", "ProTracker", "MP3"][i % 3],
             "added_at": 10_000 + (i * 7) % n,
             "duration": 30.0 + (i * 11) % n}
        if i % 7 == 0:
            r.pop("year", None)
        elif i % 7 == 3:
            r["year"] = None
        else:
            r["year"] = 1985 + i % 9
        if i == 1:
            r["year"] = 19950312                        # YYYYMMDD → 1995
        if i % 10 == 0:
            r.pop("duration")
        elif i % 10 == 5:
            r["duration"] = 0.0
        if i == 13:
            r["duration"] = float("nan")
        if i % 3 == 0:
            r["bpm"] = 90.0 + i % 40
        if i % 11 == 0:
            r["added_at"] = 0
        if i == 33:
            r.pop("added_at")
        if i == 44:
            r["added_at"] = None
        if i % 4 == 1:
            r["is_duplicate_primary"] = False
        rows.append(r)
    return rows


def _store(rows=None) -> TrackStore:
    s = TrackStore()
    s.upsert_tracks_batch(copy.deepcopy(rows or _rows()))
    return s


def _exclude(s: TrackStore) -> frozenset:
    # Every fifth track hidden, plus an id the store no longer has.
    return frozenset([tid for i, tid in enumerate(sorted(s._tracks)) if i % 5 == 2]
                     + ["gone"])


def _expected(s: TrackStore, dups: bool, ex) -> set[str]:
    return {tid for tid, t in s._tracks.items()
            if (not ex or tid not in ex) and (not dups or t.get("is_duplicate_primary", True))}


def _page_all(s: TrackStore, step: int = 7, **kw) -> list[str]:
    got: list[str] = []
    off = 0
    while True:
        page = s.filter_tracks(limit=step, offset=off, **kw)
        if not page:
            return got
        assert len(page) <= step
        got += [t["id"] for t in page]
        off += step
        assert off < 10 * len(s._tracks)


def _no_value(t: dict, key: str) -> bool:
    if key == "year":
        return normalise_year(t.get("year")) is None
    if key == "duration":
        d = t.get("duration")
        return not d or not math.isfinite(d)
    if key == "bpm":
        return t.get("bpm") is None
    if key == "added":
        return not t.get("added_at")
    return not (t.get(key) or "").strip()


def test_the_fixture_really_has_missing_values():
    s = _store()
    for key in ("year", "duration", "bpm", "added", "title", "artist", "album", "format"):
        n = sum(_no_value(t, key) for t in s._tracks.values())
        assert 0 < n < len(s._tracks), key


@pytest.mark.parametrize("sort_by", SORT_KEYS)
@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("dups", [False, True])
@pytest.mark.parametrize("hide", [False, True])
def test_every_sort_key_pages_exactly_the_counted_tracks(sort_by, order, dups, hide):
    s = _store()
    ex = _exclude(s) if hide else None
    got = _page_all(s, sort_by=sort_by, sort_order=order, filter_duplicates=dups, exclude=ex)
    count = s.visible_track_count(primary_only=dups, exclude=ex)
    assert len(got) == len(set(got)) == count, (sort_by, order, dups, hide, len(got), count)
    assert set(got) == _expected(s, dups, ex)
    # One big page is the same list as the small pages.
    assert got == [t["id"] for t in s.filter_tracks(
        limit=1000, sort_by=sort_by, sort_order=order, filter_duplicates=dups, exclude=ex)]


@pytest.mark.parametrize("sort_by", SORT_KEYS)
@pytest.mark.parametrize("order", ORDERS)
def test_deep_pages_slice_the_same_order(monkeypatch, sort_by, order):
    # Pages at offset >= _PRIMARY_ORDER_MIN_OFFSET slice the memoised
    # primary-only / exclude orders instead of walking: same rows, same order.
    s = _store()
    ex = _exclude(s)
    for dups, excl in ((False, None), (True, None), (False, ex), (True, ex)):
        kw = dict(sort_by=sort_by, sort_order=order, filter_duplicates=dups, exclude=excl)
        walked = _page_all(s, step=9, **kw)
        monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 9)
        sliced = _page_all(s, step=9, **kw)
        monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 5000)
        assert sliced == walked, (dups, excl is not None)
        assert len(sliced) == s.visible_track_count(primary_only=dups, exclude=excl)


@pytest.mark.parametrize("sort_by", SORT_KEYS)
@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("dups", [False, True])
def test_a_filtered_page_reaches_every_candidate(monkeypatch, sort_by, order, dups):
    # A predicate (the Galaxy per-format browse, a search) sorts its candidate
    # set directly when it is small and walks the index when it is not: both
    # reach every candidate, in the same order.
    s = _store()
    ex = _exclude(s)
    for q in (dict(format_="SID"), dict(query="song"), dict(artist="Artist 1")):
        cands = s._candidate_ids(**q, filter_duplicates=dups) - ex
        kw = dict(**q, sort_by=sort_by, sort_order=order, filter_duplicates=dups, exclude=ex)
        direct = _page_all(s, step=5, **kw)
        monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
        walked = _page_all(s, step=5, **kw)
        monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 2000)
        assert direct == walked, q
        assert len(direct) == len(cands) and set(direct) == cands, q


@pytest.mark.parametrize("key", ["year", "duration", "bpm", "title", "artist", "album", "format"])
def test_tracks_without_a_value_sort_last_ascending_and_first_descending(key):
    # The numeric keys now place a missing value where the lexical keys
    # always placed a blank one.
    s = _store()
    missing = sorted(tid for tid, t in s._tracks.items() if _no_value(t, key))
    asc = [t["id"] for t in s.filter_tracks(limit=1000, sort_by=key, sort_order="asc")]
    desc = [t["id"] for t in s.filter_tracks(limit=1000, sort_by=key, sort_order="desc")]
    assert asc[len(asc) - len(missing):] == missing
    assert desc[:len(missing)] == missing[::-1]
    assert desc == asc[::-1]
    if key in ("year", "duration", "bpm"):
        vals = [s._tracks[tid].get(key) for tid in asc[:len(asc) - len(missing)]]
        vals = [normalise_year(v) for v in vals] if key == "year" else vals
        assert vals == sorted(vals)
    if key == "year":
        assert 19950312 in [s._tracks[tid].get("year") for tid in asc[:len(asc) - len(missing)]]


def test_tracks_without_added_at_sort_as_the_oldest():
    s = _store()
    dateless = sorted(tid for tid, t in s._tracks.items() if not t.get("added_at"))
    assert {"t033", "t044", "t000"} <= set(dateless)
    newest_first = [t["id"] for t in s.filter_tracks(limit=1000)]          # the default
    assert newest_first[len(newest_first) - len(dateless):] == dateless[::-1]
    oldest_first = [t["id"] for t in s.filter_tracks(limit=1000, sort_by="added",
                                                     sort_order="asc")]
    assert oldest_first[:len(dateless)] == dateless
    # Recently Added (the tail of the same index) still starts at the newest.
    assert [t["id"] for t in s.recently_added(3)] == newest_first[:3]
    assert not {t["id"] for t in s.recently_added(10)} & set(dateless)


@pytest.mark.parametrize("dups", [False, True])
def test_year_ranges_never_match_a_track_without_a_year(dups):
    s = _store()
    with_year = {tid for tid, t in s._tracks.items() if normalise_year(t.get("year")) is not None}
    for lo, hi in ((None, None), (1990, None), (None, 1990), (1988, 1991), (1995, 1995),
                   (-10**9, 10**9)):
        if lo is None and hi is None:
            ids = set(store_mod._sorted_range(s._sorted_year, -999999, 999999))
        else:
            ids = {t["id"] for t in s.filter_tracks(year_min=lo, year_max=hi, limit=1000,
                                                    filter_duplicates=dups)}
        assert ids <= with_year, (lo, hi)
        want = {tid for tid in with_year
                if (lo is None or normalise_year(s._tracks[tid]["year"]) >= lo)
                and (hi is None or normalise_year(s._tracks[tid]["year"]) <= hi)
                and (not dups or s._tracks[tid].get("is_duplicate_primary", True))}
        if lo is not None or hi is not None:
            assert ids == want, (lo, hi)
            assert set(s.filter_track_ids(year_min=lo, year_max=hi,
                                          filter_duplicates=dups)) == want
    # The duplicate grouping bisects 5-second duration buckets: no bucket
    # reaches a track without a duration.
    finite = sum(1 for t in s._tracks.values() if not _no_value(t, "duration"))
    assert bisect.bisect_left(s._sorted_duration, (1e15,)) == finite


async def test_every_build_path_produces_the_same_full_lists(tmp_data_dir):
    rows = _rows()
    one_by_one = TrackStore()
    for r in copy.deepcopy(rows):
        one_by_one.upsert_track(r)                    # bisect inserts, no batch mode
    full = _store(rows)
    full.rebuild_indexes()                            # _rebuild_sorted_indexes
    yielding = TrackStore()
    yielding.enter_batch_mode()
    yielding.upsert_tracks_batch(copy.deepcopy(rows))
    await scanner._async_exit_batch_mode(yielding)    # the scan-commit rebuild
    assert not yielding._batch_mode and not yielding._dirty_sorted
    primaries = sum(1 for r in rows if r.get("is_duplicate_primary", True))
    for name in store_mod.SORTED_LISTS:
        ref = getattr(full, name)
        assert getattr(one_by_one, name) == ref, name
        assert getattr(yielding, name) == ref, name
        assert sorted(full.build_sorted_list(name)) == ref, name
        if name in NUMERIC:
            assert len(ref) == (primaries if name.endswith("_primary") else len(rows)), name
            assert all(v == v for v, _tid in ref), name            # no NaN key
    for s in (one_by_one, full, yielding):
        assert s.verify_indexes()["index_ok"]


_TRANSITIONS = [
    ("t001", {"year": None}), ("t007", {"year": 1999}), ("t014", {"year": 19870101}),
    ("t010", {"duration": 123.0}), ("t002", {"duration": 0.0}),
    ("t004", {"duration": float("nan")}), ("t013", {"duration": 61.0}),
    ("t003", {"bpm": None}), ("t004", {"bpm": 128.0}),
    ("t022", {"added_at": 20_000}), ("t005", {"added_at": 0}),
    ("t006", {"is_duplicate_primary": False}), ("t009", {"is_duplicate_primary": True}),
]


async def test_a_track_moves_between_no_value_and_a_value(tmp_data_dir):
    # Field updates re-key a track in place (incremental), through a batch
    # section's re-key plan (sync exit), and through the scanner's yielding
    # exit — in every case the lists equal a fresh rebuild and every key
    # still pages the counted tracks.
    for mode in ("incremental", "batch", "yielding"):
        s = _store()
        if mode != "incremental":
            s.enter_batch_mode()
        for tid, upd in _TRANSITIONS:
            assert s.update_track_fields(tid, upd)
        if mode != "incremental":
            plan = s.sorted_rebuild_plan()
            # A re-key plan (not a full rebuild) for each numeric list.
            assert set(NUMERIC) <= set(plan) and all(plan[n] is not None for n in NUMERIC)
        if mode == "batch":
            s.exit_batch_mode()
        elif mode == "yielding":
            await scanner._async_exit_batch_mode(s)
        assert s.verify_indexes()["index_ok"], mode
        for sort_by in SORT_KEYS:
            for dups in (False, True):
                got = _page_all(s, step=11, sort_by=sort_by, filter_duplicates=dups)
                assert len(got) == len(set(got)) == s.visible_track_count(primary_only=dups), (
                    mode, sort_by, dups)
        # ... and back: each track re-keyed out of the no-value slot again.
        s.update_track_fields("t001", {"year": 2001})
        s.update_track_fields("t004", {"duration": 99.0, "bpm": None})
        s.update_track_fields("t005", {"added_at": 12_345})
        assert s.verify_indexes()["index_ok"], mode


async def test_raw_string_values_are_keyed_as_numbers_or_no_value(tmp_data_dir):
    # A stored row may hold a raw string (a legacy "1999" year — the API model
    # turns it into 1999).  A numeric one is keyed as its number, any other as
    # no value, on every build path: an insert, a rebuild, a sort or a year
    # range never meets keys that don't compare (it used to raise TypeError as
    # soon as the list also held an int year — or, keyed math.inf, a track
    # without one).
    nv = store_mod._NO_VALUE_KEY
    rows = [{"id": "s", "path": "/m/s.mod", "title": "S", "year": "19990101",
             "duration": "95.5", "bpm": "fast", "added_at": "7"},
            {"id": "y", "path": "/m/y.mod", "title": "Y", "year": 1985, "duration": 30.0,
             "bpm": 120.0, "added_at": 5}]
    rows += [{"id": f"n{i}", "path": f"/m/n{i}.mod", "title": f"N{i}", "added_at": 6 + i}
             for i in range(3)]
    one_by_one = TrackStore()
    for r in copy.deepcopy(rows):
        one_by_one.upsert_track(r)
    full = _store(rows)
    full.rebuild_indexes()
    yielding = TrackStore()
    yielding.enter_batch_mode()
    yielding.upsert_tracks_batch(copy.deepcopy(rows))
    await scanner._async_exit_batch_mode(yielding)
    for s in (one_by_one, full, yielding):
        assert s._sorted_year == [(1985, "y"), (1999, "s"), (nv, "n0"), (nv, "n1"), (nv, "n2")]
        assert s._sorted_duration == [(30.0, "y"), (95.5, "s"),
                                      (nv, "n0"), (nv, "n1"), (nv, "n2")]
        assert s._sorted_bpm == [(120.0, "y"), (nv, "n0"), (nv, "n1"), (nv, "n2"), (nv, "s")]
        assert s._sorted_added_at == [(5, "y"), (6, "n0"), (7, "n1"), (7, "s"), (8, "n2")]
        assert {t["id"] for t in s.filter_tracks(year_min=1990, limit=50)} == {"s"}
        for key in ("year", "duration", "bpm", "added"):
            for order in ("asc", "desc"):
                assert len(_page_all(s, step=2, sort_by=key, sort_order=order)) == 5
        assert s.verify_indexes()["index_ok"]


def test_numeric_list_tuples_are_not_gc_tracked():
    # The lists hold ~1M tuples at 263K tracks: a key the cyclic GC tracks
    # (an instance of a Python class) would keep every one of them tracked and
    # slow every full collection.  Numbers and strings let the GC drop them.
    import gc
    s = _store()
    s.rebuild_indexes()
    gc.collect()
    for name in NUMERIC:
        assert not any(gc.is_tracked(e) for e in getattr(s, name)), name


def test_deleting_tracks_without_values_empties_every_list():
    s = _store()
    s.delete_track_ids(list(s._tracks))
    for name in store_mod.SORTED_LISTS:
        assert getattr(s, name) == [], name


# ── through the HTTP API (the QA measurement) ──────────────────────────────


def _ph(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()[:16]


@pytest.fixture
def api_lib(tmp_path, monkeypatch):
    """Two local roots (A: 60 tracks, B: 40), a quarter of the tracks without a
    year and a fifth without a duration, installed as THE store."""
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(remote_cache, "_cache", remote_cache.RemoteCache(tmp_path / "rc", 512))
    availability.reset_for_tests()
    ra, rb = str(a), str(b)
    s.upsert_scan_dir(ra)
    s.upsert_scan_dir(rb)
    ids = {ra: [], rb: []}
    for i in range(100):
        root = ra if i < 60 else rb
        path = f"{root}/t{i}.mod"
        row = Track(id=f"id{i:03d}", path=path, title=f"Song {i}", artist=f"Artist {i % 7}",
                    year=None if i % 4 == 0 else 1990 + i % 5,
                    duration=0.0 if i % 5 == 0 else 100.0 + i,
                    bpm=120.0 if i % 9 == 0 else None,
                    format="ProTracker", added_at=1000 + i, mtime=1.7e9, file_size=1000,
                    dir_hash=_ph(root), scan_root_hash=_ph(root)).model_dump()
        row.pop("embedding")
        s.upsert_track(row)
        ids[root].append(row["id"])

    class Lib:
        store = s
        root_b = rb
        ids_a, ids_b = ids[ra], ids[rb]
    yield Lib()
    availability.reset_for_tests()


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(tracks_api.router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _get(c: httpx.AsyncClient, url: str):
    r = await c.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:300])
    return r.json()


@pytest.mark.parametrize("mode", ["show", "hide"])
@pytest.mark.parametrize("dedup", [False, True])
async def test_api_sorted_pages_match_the_visible_count(api_lib, mode, dedup):
    api_lib.store.set_scan_dir_status(api_lib.root_b, "unavailable")
    api_lib.store.set_config(availability.MODE_KEY, mode)
    api_lib.store.set_config("filter_duplicates", dedup)
    # Two copies of one track: the hidden copy is in neither the count nor a page.
    api_lib.store.update_track_fields(api_lib.ids_a[1], {"is_duplicate_primary": False})
    want = set(api_lib.ids_a) | (set(api_lib.ids_b) if mode == "show" else set())
    if dedup:
        want.discard(api_lib.ids_a[1])
    async with _client() as c:
        cnt = await _get(c, "/api/tracks/count")
        assert cnt == {"count": 100, "visible": len(want)}
        for sort in SORT_KEYS:
            for order in ORDERS:
                q = (f"&sort={sort}" if sort else "") + (f"&order={order}" if order else "")
                got: list[str] = []
                for off in range(0, cnt["visible"] + 20, 13):
                    got += [r["id"] for r in
                            await _get(c, f"/api/tracks?limit=13&offset={off}{q}")]
                assert len(got) == len(set(got)) == cnt["visible"], (sort, order, len(got))
                assert set(got) == want, (sort, order)


@pytest.mark.parametrize("mode", ["show", "mark", "hide"])
async def test_the_play_queue_pages_every_playable_track_in_any_sort(api_lib, mode):
    # ``playable=1`` (the play queue paging a sorted list): it follows
    # ``next_offset`` until ``ended`` — a page cut short by a missing value
    # would end the queue early.
    api_lib.store.set_scan_dir_status(api_lib.root_b, "unavailable")
    api_lib.store.set_config(availability.MODE_KEY, mode)
    want = set(api_lib.ids_a) | (set(api_lib.ids_b) if mode == "show" else set())
    async with _client() as c:
        for sort in ("year", "duration", "bpm", "added"):
            for order in ("asc", "desc"):
                got: list[str] = []
                off = 0
                for _ in range(50):
                    page = await _get(c, f"/api/tracks?playable=1&limit=11&offset={off}"
                                         f"&sort={sort}&order={order}")
                    got += [r["id"] for r in page["tracks"]]
                    off = page["next_offset"]
                    if page["ended"]:
                        break
                assert len(got) == len(set(got)) and set(got) == want, (mode, sort, order)
