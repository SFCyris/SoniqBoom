# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shuffle play must cover the WHOLE result set, not the rows a list has loaded.

Regression guard for GitHub issue #11 ("is random playing really random?"): with
~75 000 songs, shuffle only ever picked from the first few hundred, because the
browser shuffled inside its small play queue.  Shuffle is now a server-side
seeded permutation of every track matching the view's filter, paged by the
client.  These tests pin the properties that fix depends on:

  * paging the shuffled order visits EVERY matching track exactly once
    (full coverage + no repeats) — including the deep tail of a big library,
  * the order honours the same predicates + "hide duplicates" setting as the
    list endpoints (one shared resolver — they cannot drift),
  * the same seed reproduces the same order (resume after reload), different
    seeds give unrelated orders, and the order is stable when a scan adds or
    removes tracks mid-session,
  * ``/tracks/count`` reports the deduped ``visible`` total the list really serves.
"""
from __future__ import annotations

import json
from collections import Counter

from soniqboom.api import tracks as tracks_api
from soniqboom.core import shuffle_order
from soniqboom.core.store import TrackStore


def _track(n: int, **kw) -> dict:
    d = {
        "id": f"t{n:06d}",
        "title": f"Track {n}",
        "artist": "",
        "album_artist": "",
        "album": "",
        "genre": [],
        "format": "MP3",
        "year": 2000,
        "added_at": 1_700_000_000 + n,
        "duration": 120.0,
    }
    d.update(kw)
    return d


def _big_store(n: int = 3000) -> TrackStore:
    """A library shaped like the bug report: far more tracks than any queue."""
    store = TrackStore()
    for i in range(n):
        fmt = "ProTracker" if i % 3 == 0 else ("SID" if i % 3 == 1 else "FLAC")
        store.upsert_track(_track(
            i, format=fmt,
            artist="Purple Motion" if i % 10 == 0 else ("" if i % 7 == 0 else f"Artist {i % 50}"),
            genre=["Module"] if fmt != "FLAC" else ["Rock"],
            year=1990 + (i % 30),
        ))
    return store


async def _page(monkeypatch, store, *, seed, offset=0, limit=50, **filters) -> dict:
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    shuffle_order.clear()
    args = dict(q=None, artist=None, album_artist=None, album=None, genre=None,
                scene_group=None, format=None, year_min=None, year_max=None,
                untagged=None)
    args.update(filters)
    resp = await tracks_api.shuffled_tracks(seed=seed, offset=offset, limit=limit, **args)
    return json.loads(resp.body)


async def _all_pages(monkeypatch, store, *, seed, limit=200, **filters) -> list[str]:
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    shuffle_order.clear()
    args = dict(q=None, artist=None, album_artist=None, album=None, genre=None,
                scene_group=None, format=None, year_min=None, year_max=None,
                untagged=None)
    args.update(filters)
    ids: list[str] = []
    offset = 0
    while True:
        resp = await tracks_api.shuffled_tracks(seed=seed, offset=offset, limit=limit, **args)
        body = json.loads(resp.body)
        if not body["tracks"]:
            return ids
        ids.extend(t["id"] for t in body["tracks"])
        offset += limit
        assert offset < 1_000_000, "paging never ended"        # a regression must FAIL, not hang


# ── the issue-#11 property ────────────────────────────────────────────────────

async def test_shuffle_covers_entire_library_exactly_once(monkeypatch):
    store = _big_store(3000)
    ids = await _all_pages(monkeypatch, store, seed=42)
    assert len(ids) == 3000                      # every track reachable…
    assert len(set(ids)) == 3000                 # …and none repeated
    assert set(ids) == set(store._tracks)        # …and nothing foreign


async def test_first_page_reaches_the_deep_tail(monkeypatch):
    """The bug: picks only ever came from the head of the list.  The very first
    shuffled page must already draw from all over the library — including the
    last third — not from a leading window."""
    store = _big_store(3000)
    body = await _page(monkeypatch, store, seed=7, limit=50)
    positions = sorted(int(t["id"][1:]) for t in body["tracks"])
    assert body["total"] == 3000
    assert positions[-1] > 2000, positions       # tail is reachable on page 1
    assert positions[0] < 1000, positions
    # Inversion of the old behaviour: a leading-window pick would put ALL 50
    # inside the first 500 rows.
    assert sum(p < 500 for p in positions) < 25, positions


async def test_every_track_can_be_first(monkeypatch):
    """Across seeds, the first-played track is spread over the whole library."""
    store = _big_store(600)
    firsts = Counter()
    for seed in range(400):
        body = await _page(monkeypatch, store, seed=seed, limit=1)
        firsts[int(body["tracks"][0]["id"][1:]) // 100] += 1
    # 6 buckets of 100 tracks, 400 draws → ~67 each; a head-only pick gives 400/0/0…
    assert len(firsts) == 6, firsts
    assert min(firsts.values()) > 30, firsts


# ── filter fidelity (shared resolver) ────────────────────────────────────────

async def test_filtered_shuffle_matches_the_list_filter(monkeypatch):
    store = _big_store(3000)
    ids = await _all_pages(monkeypatch, store, seed=5, format="ProTracker")
    expected = {d["id"] for d in store.filter_tracks(format_="ProTracker", limit=10_000)}
    assert set(ids) == expected and len(ids) == len(expected) == 1000


async def test_combined_filter_artist_and_format(monkeypatch):
    store = _big_store(3000)
    ids = await _all_pages(monkeypatch, store, seed=9,
                           artist="Purple Motion", format="ProTracker")
    expected = {d["id"] for d in store.filter_tracks(
        artist="Purple Motion", format_="ProTracker", limit=10_000)}
    assert expected and set(ids) == expected


async def test_search_query_uses_the_search_parse_path(monkeypatch):
    store = _big_store(900)
    ids = await _all_pages(monkeypatch, store, seed=3,
                           q='artist:"Purple Motion" format:SID')
    expected = {d["id"] for d in store.filter_tracks(
        artist="Purple Motion", format_="SID", limit=10_000)}
    assert set(ids) == expected


async def test_untagged_predicate(monkeypatch):
    store = _big_store(700)
    ids = await _all_pages(monkeypatch, store, seed=1, untagged="artist")
    expected = {tid for tid, t in store._tracks.items() if not (t.get("artist") or "").strip()}
    assert expected and set(ids) == expected


async def test_shuffle_honours_hide_duplicates(monkeypatch):
    store = TrackStore()
    for i in range(40):
        store.upsert_track(_track(i, is_duplicate_primary=(i % 4 != 0),
                                  duplicate_group_id=f"g{i // 4}"))
    primaries = {tid for tid, t in store._tracks.items()
                 if t.get("is_duplicate_primary", True)}
    assert len(primaries) == 30

    store.set_config("filter_duplicates", False)
    assert len(await _all_pages(monkeypatch, store, seed=2)) == 40
    store.set_config("filter_duplicates", True)
    ids = await _all_pages(monkeypatch, store, seed=2)
    assert set(ids) == primaries and len(ids) == 30
    # …and that is exactly what the deduped LIST serves — with a predicate too.
    assert set(ids) == {d["id"] for d in store.filter_tracks(limit=1000, filter_duplicates=True)}
    listed = {d["id"] for d in store.filter_tracks(format_="MP3", limit=1000, filter_duplicates=True)}
    assert set(await _all_pages(monkeypatch, store, seed=4, format="MP3")) == listed
    assert set(store.filter_track_ids(format_="MP3", filter_duplicates=True)) == listed


def test_filter_track_ids_is_the_same_predicate_as_filter_tracks():
    store = _big_store(1200)
    for preds in ({"genre": "Module"}, {"format_": "SID", "year_min": 1995, "year_max": 2005},
                  {"artist": "Artist 7"}, {"genre": "Rock", "format_": "FLAC"}):
        listed = {d["id"] for d in store.filter_tracks(**preds, limit=10_000)}
        assert set(store.filter_track_ids(**preds)) == listed, preds


# ── order properties ─────────────────────────────────────────────────────────

def test_same_seed_same_order_different_seed_unrelated():
    ids = [f"t{i:06d}" for i in range(2000)]
    a, a2, b = (shuffle_order.seeded_order(ids, s) for s in (11, 11, 12))
    assert a == a2 and sorted(a) == ids
    assert a != b
    # "Unrelated": few ids keep their position between two seeds (expected ≈ 1).
    assert sum(x == y for x, y in zip(a, b)) < 12
    # …and neighbours don't stay neighbours (a constant-XOR reshuffle would).
    pairs_a = set(zip(a, a[1:]))
    assert len(pairs_a & set(zip(b, b[1:]))) < 12


def test_order_is_stable_when_tracks_are_added_or_removed():
    ids = [f"t{i:06d}" for i in range(1000)]
    base = shuffle_order.seeded_order(ids, 99)
    fewer = shuffle_order.seeded_order([i for i in ids if i != base[10]], 99)
    assert fewer == base[:10] + base[11:]              # removal only deletes a slot
    more = shuffle_order.seeded_order(ids + ["zz-new"], 99)
    assert [i for i in more if i != "zz-new"] == base  # addition only inserts one


# ── list sizing ──────────────────────────────────────────────────────────────

async def test_count_reports_visible_total_under_hide_duplicates(monkeypatch):
    store = TrackStore()
    for i in range(20):
        store.upsert_track(_track(i, is_duplicate_primary=(i % 5 != 0),
                                  duplicate_group_id=f"g{i // 5}"))
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)

    store.set_config("filter_duplicates", False)
    assert await tracks_api.count_tracks() == {"count": 20, "visible": 20}
    store.set_config("filter_duplicates", True)
    body = await tracks_api.count_tracks()
    assert body == {"count": 20, "visible": 16}
    # ``visible`` is exactly what the deduped list can serve.
    assert len(store.filter_tracks(limit=1000, filter_duplicates=True)) == body["visible"]


# ── folder view (``/fs/tracks-with-meta?shuffle_seed=``) ─────────────────────

def _folder_rows(n: int, prefix: str = "f") -> list[dict]:
    return [{"id": f"{prefix}{i:05d}", "title": f"Row {i}", "path": f"/music/x/{i}.mod"}
            for i in range(n)]


def test_folder_shuffle_pages_cover_the_whole_listing_once(monkeypatch):
    from soniqboom.api import fstree
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    shuffle_order.clear()
    rows = _folder_rows(1234)

    seen: list[str] = []
    offset = 0
    while True:
        listing = fstree._shuffled_listing(rows, "/music/x", True, False, 11)
        page = fstree._maybe_paginate(listing, offset, 100, True)
        assert page["total"] == 1234
        if not page["tracks"]:
            break
        seen += [t["id"] for t in page["tracks"]]
        offset += 100
    assert Counter(seen).most_common(1)[0][1] == 1     # no repeats
    assert set(seen) == {r["id"] for r in rows}          # full coverage
    assert seen != [r["id"] for r in rows]               # …and actually shuffled
    # The rows themselves are passed through untouched.
    assert all(t["path"].startswith("/music/x/") for t in listing)


def test_folder_shuffle_seed_reproducible_and_reshuffles(monkeypatch):
    from soniqboom.api import fstree
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    shuffle_order.clear()
    rows = _folder_rows(500)
    ids = lambda seed: [t["id"] for t in fstree._shuffled_listing(rows, "/music/x", True, False, seed)]
    assert ids(5) == ids(5)
    assert ids(5) != ids(6)
    assert sorted(ids(6)) == sorted(r["id"] for r in rows)


def test_folder_shuffle_survives_a_listing_that_drifts_under_the_cache_key(monkeypatch):
    """Same folder / seed / size / store version, different ids (files renamed
    on disk before a rescan): every row must still be dealt — none dropped."""
    from soniqboom.api import fstree
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    shuffle_order.clear()
    before = _folder_rows(300, prefix="a")
    after = before[:299] + [{"id": "renamed", "title": "New", "path": "/music/x/new.mod"}]
    fstree._shuffled_listing(before, "/music/x", True, False, 3)     # primes the cache
    out = fstree._shuffled_listing(after, "/music/x", True, False, 3)
    assert sorted(t["id"] for t in out) == sorted(t["id"] for t in after)


async def test_folder_endpoint_shuffle_seed_pages_the_same_listing(monkeypatch, tmp_path):
    """End to end through ``tracks_with_meta``: ``shuffle_seed`` reorders the
    SAME listing (same total, same rows) and pages through all of it."""
    from soniqboom.api import fstree
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    shuffle_order.clear()
    for i in range(37):
        (tmp_path / f"tune{i:02d}.mod").write_bytes(b"\0" * 16)
    (tmp_path / "notes.txt").write_text("not audio")

    async def call(**kw):
        return await fstree.tracks_with_meta(
            path=str(tmp_path), recursive=True, filter_duplicates=False,
            offset=kw.pop("offset", 0), limit=kw.pop("limit", 10),
            shuffle_seed=kw.pop("shuffle_seed", None))

    ordered = []
    for off in range(0, 40, 10):
        ordered += [t["id"] for t in (await call(offset=off))["tracks"]]
    assert len(ordered) == 37

    shuffled = []
    for off in range(0, 40, 10):
        page = await call(offset=off, shuffle_seed=21)
        assert page["total"] == 37
        shuffled += [t["id"] for t in page["tracks"]]
    assert sorted(shuffled) == sorted(ordered)           # same rows, all of them, once
    assert shuffled != ordered
    assert (await call(shuffle_seed=21))["tracks"][0]["id"] == shuffled[0]   # reproducible
    # No seed → the endpoint's own order is untouched.
    assert [t["id"] for t in (await call(limit=37))["tracks"]] == ordered


# ── order cache ──────────────────────────────────────────────────────────────

async def test_cached_order_survives_metadata_writes_and_skips_deleted_ids(monkeypatch):
    """Play-time backfills (duration, cover ref, defect) bump the store's
    mutation counter constantly; they must not throw the session's order away.
    Ids deleted mid-session are skipped, never served."""
    store = _big_store(600)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    shuffle_order.clear()
    args = dict(q=None, artist=None, album_artist=None, album=None, genre=None,
                scene_group=None, format=None, year_min=None, year_max=None, untagged=None)
    first = json.loads((await tracks_api.shuffled_tracks(seed=8, offset=0, limit=50, **args)).body)

    calls = []
    real = shuffle_order.seeded_order
    monkeypatch.setattr(shuffle_order, "seeded_order", lambda ids, seed: calls.append(1) or real(ids, seed))
    store.update_track_fields(first["tracks"][0]["id"], {"duration": 321.0})
    gone = None
    second = json.loads((await tracks_api.shuffled_tracks(seed=8, offset=50, limit=50, **args)).body)
    assert calls == []                                   # no re-sort
    gone = second["tracks"][5]["id"]
    store.delete_track(gone)
    again = json.loads((await tracks_api.shuffled_tracks(seed=8, offset=50, limit=50, **args)).body)
    assert calls == []
    assert gone not in [t["id"] for t in again["tracks"]]
    assert [t["id"] for t in again["tracks"]] == [t["id"] for t in second["tracks"] if t["id"] != gone]


def test_cache_is_bounded_by_entries_and_by_total_ids(monkeypatch):
    shuffle_order.clear()
    monkeypatch.setattr(shuffle_order, "_MAX_CACHED_ORDERS", 4)
    monkeypatch.setattr(shuffle_order, "_MAX_CACHED_IDS", 250)
    for k in range(10):                                  # small orders: entry bound
        shuffle_order.cached_order(("k", k), lambda: [f"a{i}" for i in range(10)], k)
    assert len(shuffle_order._cache) == 4
    assert shuffle_order.peek(("k", 9)) is not None and shuffle_order.peek(("k", 0)) is None
    for k in range(3):                                   # big orders: id budget
        shuffle_order.cached_order(("big", k), lambda: [f"b{i}" for i in range(100)], k)
    assert shuffle_order._cached_ids == sum(len(v) for v in shuffle_order._cache.values()) <= 250
    assert shuffle_order.peek(("big", 2)) is not None
    # One order larger than the whole budget is still kept (it is the live one).
    shuffle_order.cached_order(("huge",), lambda: [f"c{i}" for i in range(999)], 1)
    assert list(shuffle_order._cache) == [("huge",)]
    shuffle_order.clear()
    assert shuffle_order._cached_ids == 0


def test_concurrent_cold_requests_sort_once():
    import threading, time
    shuffle_order.clear()
    calls = []

    def factory():
        calls.append(1)
        time.sleep(0.15)                                 # a slow cold build
        return [f"t{i}" for i in range(500)]

    out: list = []
    threads = [threading.Thread(target=lambda: out.append(
        shuffle_order.cached_order(("sf",), factory, 3))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert len(out) == 4 and all(o == out[0] for o in out)
    assert not shuffle_order._inflight


def test_refresh_replaces_a_cached_order():
    shuffle_order.clear()
    a = shuffle_order.cached_order(("r",), lambda: ["x", "y", "z"], 1)
    b = shuffle_order.cached_order(("r",), lambda: ["p", "q"], 1)             # hit
    c = shuffle_order.cached_order(("r",), lambda: ["p", "q"], 1, refresh=True)
    assert a is b and sorted(c) == ["p", "q"]
    assert shuffle_order.peek(("r",)) is c and shuffle_order._cached_ids == 2


def test_normalize_seed_accepts_anything():
    assert shuffle_order.normalize_seed(5) == 5
    assert shuffle_order.normalize_seed("17") == 17
    assert shuffle_order.normalize_seed(-1) == 0x7FFF_FFFF_FFFF_FFFF
    assert shuffle_order.normalize_seed(2 ** 80 + 3) == 3
    assert shuffle_order.normalize_seed(None) == 0
    assert shuffle_order.normalize_seed("nope") == 0
    ids = [f"t{i}" for i in range(50)]
    assert sorted(shuffle_order.seeded_order(ids, -1)) == sorted(ids)


def test_unknown_untagged_field_is_rejected_not_ignored():
    import pytest
    store = _big_store(50)
    with pytest.raises(ValueError):
        store.filter_track_ids(untagged="scene_group")
    assert 0 < len(store.filter_track_ids(untagged="artist")) < 50


async def test_folder_endpoint_shuffles_the_indexed_store_listing(monkeypatch, tmp_path):
    """The ``recursive=true`` fast path (rows straight from the store index, the
    one a real library folder takes) with ``dedup_folders`` semantics applied:
    the shuffle is a permutation of exactly the rows the ordered call serves."""
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    monkeypatch.setattr(fstree, "_STORE_RECURSIVE_CACHE", {}, raising=False)
    shuffle_order.clear()
    root = str(tmp_path.resolve())
    (tmp_path / "sub").mkdir()
    store.upsert_scan_dir(root)
    for i in range(64):
        store.upsert_track(_track(
            i, path=f"{root}/sub/tune{i:02d}.mod", scan_root_hash=path_hash(root),
            duplicate_group_id=f"g{i // 2}", is_duplicate_primary=(i % 2 == 0)))

    async def ids(**kw):
        res = await fstree.tracks_with_meta(
            path=root, recursive=True, offset=0, limit=500,
            filter_duplicates=kw.get("dedup", False), shuffle_seed=kw.get("seed"))
        return res["total"], [t["id"] for t in res["tracks"]]

    total, ordered = await ids()
    assert total == 64 and len(ordered) == 64            # came from the store, not the (empty) FS walk
    t2, shuffled = await ids(seed=9)
    assert t2 == 64 and sorted(shuffled) == sorted(ordered) and shuffled != ordered
    t3, deduped = await ids(dedup=True)
    t4, deduped_shuffled = await ids(dedup=True, seed=9)
    assert t3 == t4 == 32 and sorted(deduped_shuffled) == sorted(deduped)


# ── cache-key correctness (warm cache — no clear() between the calls) ─────────

async def _warm_ids(store, *, seed, **filters) -> list[str]:
    args = dict(q=None, artist=None, album_artist=None, album=None, genre=None,
                scene_group=None, format=None, year_min=None, year_max=None, untagged=None)
    args.update(filters)
    out, offset = [], 0
    while True:
        body = json.loads((await tracks_api.shuffled_tracks(
            seed=seed, offset=offset, limit=500, **args)).body)
        if not body["tracks"]:
            return out
        out += [t["id"] for t in body["tracks"]]
        offset += 500


async def test_same_seed_different_filters_never_share_a_cached_order(monkeypatch):
    store = _big_store(900)
    for i in range(0, 900, 9):                       # some non-primary duplicates
        store.update_track_fields(f"t{i:06d}", {"is_duplicate_primary": False,
                                                "duplicate_group_id": "g"})
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    shuffle_order.clear()
    fmt = lambda f: {d["id"] for d in store.filter_tracks(format_=f, limit=10_000)}

    assert set(await _warm_ids(store, seed=77, format="ProTracker")) == fmt("ProTracker")
    assert set(await _warm_ids(store, seed=77, format="SID")) == fmt("SID")
    assert set(await _warm_ids(store, seed=77, genre="Rock")) == \
        {d["id"] for d in store.filter_tracks(genre="Rock", limit=10_000)}
    assert set(await _warm_ids(store, seed=77, q="Purple Motion")) != set(await _warm_ids(store, seed=77))
    everything = await _warm_ids(store, seed=77)
    assert len(everything) == 900
    store.set_config("filter_duplicates", True)       # same seed, same (no) filter
    assert len(await _warm_ids(store, seed=77)) == 800


def test_folder_cache_key_separates_dedup_mode_recursion_and_path(monkeypatch):
    """Every key component must separate entries.  The drift heal would hide a
    collision from the OUTPUT (it re-deals), so count the sorts instead: distinct
    listings sort once each, and going back to an earlier one sorts nothing."""
    from soniqboom.api import fstree
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: TrackStore())
    shuffle_order.clear()
    sorts = []
    real = shuffle_order.seeded_order
    monkeypatch.setattr(shuffle_order, "seeded_order",
                        lambda ids, seed: sorts.append(1) or real(ids, seed))
    a, b = _folder_rows(60, prefix="a"), _folder_rows(60, prefix="b")
    variants = [
        (a, "/m/x", True, False, 5),
        (b, "/m/y", True, False, 5),      # other path, same size
        (b, "/m/x", False, False, 5),     # other recursion mode
        (b, "/m/x", True, True, 5),       # other dedup mode
        (_folder_rows(61, prefix="c"), "/m/x", True, False, 5),   # other size (and other files)
        (a, "/m/x", True, False, 6),      # other seed
    ]
    for rows, *key in variants:
        out = fstree._shuffled_listing(rows, *key)
        assert sorted(t["id"] for t in out) == sorted(t["id"] for t in rows)
    assert len(sorts) == len(variants)
    for rows, *key in variants:           # all six are still cached, none evicted another
        fstree._shuffled_listing(rows, *key)
    assert len(sorts) == len(variants)


def test_a_waiter_that_never_gets_a_turn_still_caches_its_result(monkeypatch):
    import threading
    shuffle_order.clear()
    monkeypatch.setattr(shuffle_order, "_INFLIGHT_WAIT_S", 0.01)
    monkeypatch.setattr(shuffle_order, "_INFLIGHT_ATTEMPTS", 2)
    hold = threading.Event()
    owner = threading.Thread(target=lambda: shuffle_order.cached_order(
        ("slow",), lambda: (hold.wait(5), ["a", "b"])[1], 1))
    owner.start()
    import time as _t
    deadline = _t.monotonic() + 5
    while ("slow",) not in shuffle_order._inflight:
        assert _t.monotonic() < deadline
        _t.sleep(0.001)
    got = shuffle_order.cached_order(("slow",), lambda: ["a", "b"], 1)   # times out twice
    assert sorted(got) == ["a", "b"] and shuffle_order.peek(("slow",)) is not None
    hold.set(); owner.join(5)
    assert not shuffle_order._inflight

async def test_folder_endpoint_treats_seed_zero_as_a_seed(monkeypatch, tmp_path):
    from soniqboom.api import fstree
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    shuffle_order.clear()
    for i in range(25):
        (tmp_path / f"s{i:02d}.mod").write_bytes(b"\0" * 8)
    call = lambda seed: fstree.tracks_with_meta(path=str(tmp_path), recursive=True, offset=0,
                                                limit=25, filter_duplicates=False, shuffle_seed=seed)
    plain = [t["id"] for t in (await call(None))["tracks"]]
    zero = [t["id"] for t in (await call(0))["tracks"]]
    assert sorted(zero) == sorted(plain) and zero != plain


def test_refresh_recomputes_even_while_another_thread_is_sorting_the_key():
    import threading
    shuffle_order.clear()
    started, release = threading.Event(), threading.Event()

    def slow_old():
        started.set()
        release.wait(5)
        return ["OLD-a", "OLD-b", "OLD-c"]

    out = {}
    t1 = threading.Thread(target=lambda: out.update(
        normal=shuffle_order.cached_order(("rk",), slow_old, 1)))
    t1.start()
    assert started.wait(5)
    t2 = threading.Thread(target=lambda: out.update(
        refresh=shuffle_order.cached_order(("rk",), lambda: ["NEW-x", "NEW-y"], 1, refresh=True)))
    t2.start()
    release.set()
    t1.join(5); t2.join(5)
    assert sorted(out["normal"]) == ["OLD-a", "OLD-b", "OLD-c"]
    assert sorted(out["refresh"]) == ["NEW-x", "NEW-y"]
    assert sorted(shuffle_order.peek(("rk",))) == ["NEW-x", "NEW-y"]
    assert not shuffle_order._inflight


def test_a_failed_computation_does_not_fan_out_into_one_sort_per_waiter():
    import threading, time
    shuffle_order.clear()
    calls = []

    def factory():
        calls.append(1)
        time.sleep(0.05)
        if len(calls) == 1:
            raise RuntimeError("first owner dies")
        return [f"t{i}" for i in range(100)]

    results, errors = [], []

    def run():
        try:
            results.append(shuffle_order.cached_order(("boom",), factory, 2))
        except RuntimeError as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(errors) == 1 and len(results) == 4
    assert len(calls) == 2                               # the failure + ONE recompute
    assert shuffle_order.peek(("boom",)) is not None and not shuffle_order._inflight


async def test_hide_duplicates_id_set_does_not_depend_on_added_at(monkeypatch):
    """The deduped id set is a SET question; tracks the ``added`` sort index
    cannot hold (no ``added_at``) must still be shuffled and counted alike."""
    store = TrackStore()
    for i in range(10):
        store.upsert_track(_track(i))
    for i in range(100, 103):
        store.upsert_track(_track(i, added_at=0))
    store.upsert_track(_track(200, is_duplicate_primary=False, duplicate_group_id="g"))
    store.set_config("filter_duplicates", True)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    shuffle_order.clear()
    assert len(store.filter_track_ids(filter_duplicates=True)) == 13
    assert len(await _warm_ids(store, seed=6)) == 13
    assert (await tracks_api.count_tracks())["visible"] == 13


# ── Settings → "Hide duplicates when browsing folders" ───────────────────────

async def test_folder_dedup_defaults_on_and_only_a_value_saved_by_a_working_ui_counts(monkeypatch, tmp_path):
    from soniqboom.api import fstree
    from soniqboom.core import data
    from soniqboom.core.data import path_hash
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    monkeypatch.setattr(fstree, "_STORE_RECURSIVE_CACHE", {}, raising=False)
    shuffle_order.clear()
    root = str(tmp_path.resolve())
    (tmp_path / "sub").mkdir()
    store.upsert_scan_dir(root)
    for i in range(20):
        store.upsert_track(_track(
            i, path=f"{root}/sub/tune{i:02d}.mod", scan_root_hash=path_hash(root),
            duplicate_group_id=f"g{i // 2}", is_duplicate_primary=(i % 2 == 0)))

    async def total():            # what the web UI sends: NO filter_duplicates param
        res = await fstree.tracks_with_meta(path=root, recursive=True, offset=0, limit=100,
                                            filter_duplicates=None, shuffle_seed=None)
        return res["total"]

    assert await data.folder_dedup_enabled() is True and await total() == 10     # fresh install
    store.set_config("dedup_folders", False)             # left behind by the old, inert checkbox
    assert await data.folder_dedup_enabled() is True and await total() == 10
    store.set_config("dedup_folders_set", True)          # saved by a version where it works
    assert await data.folder_dedup_enabled() is False and await total() == 20
    store.set_config("dedup_folders", True)
    assert await total() == 10


def test_folder_shuffle_page_equals_a_slice_of_the_full_shuffle_and_is_cheap_when_warm(monkeypatch):
    import time
    from soniqboom.api import fstree
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: TrackStore())
    shuffle_order.clear()
    fstree._BYID_MEMO.clear()
    rows = _folder_rows(60_000)
    full = [t["id"] for t in fstree._shuffled_listing(rows, "/big", True, False, 4)]
    total, page = fstree._shuffled_page(rows, "/big", True, False, 4, 1000, 50)
    assert total == 60_000 and [t["id"] for t in page] == full[1000:1050]
    assert fstree._shuffled_page(rows, "/big", True, False, 4, 59_990, 50)[1][-1]["id"] == full[-1]
    assert fstree._shuffled_page(rows, "/big", True, False, 4, 60_000, 50) == (60_000, [])
    t0 = time.perf_counter()
    for off in range(0, 5000, 50):
        fstree._shuffled_page(rows, "/big", True, False, 4, off, 50)
    per_page_ms = (time.perf_counter() - t0) * 1000 / 100
    assert per_page_ms < 1.0, f"{per_page_ms:.2f} ms per warm page — the id map is being rebuilt"
    # …and a listing that drifted under the key is re-dealt, not served short.
    drifted = rows[:-1] + [{"id": "renamed", "title": "x", "path": "/big/x.mod"}]
    total, page = fstree._shuffled_page(drifted, "/big", True, False, 4, 0, 60_000)
    assert total == 60_000 and sorted(t["id"] for t in page) == sorted(t["id"] for t in drifted)


# ── ground truth (NOT computed through the code under test) ──────────────────

def _truth(store, pred) -> set[str]:
    return {tid for tid, t in store._tracks.items() if pred(t)}


async def test_combined_predicates_intersect_against_ground_truth(monkeypatch):
    """`filter_tracks`, `filter_track_ids` and the shuffle all share one resolver,
    so comparing them with each other proves nothing about the predicate itself."""
    store = _big_store(1500)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    shuffle_order.clear()
    pm_pt = _truth(store, lambda t: t["artist"] == "Purple Motion" and t["format"] == "ProTracker")
    assert 0 < len(pm_pt) < len(_truth(store, lambda t: t["artist"] == "Purple Motion"))
    assert set(await _warm_ids(store, seed=1, artist="Purple Motion", format="ProTracker")) == pm_pt
    assert set(store.filter_track_ids(artist="Purple Motion", format_="ProTracker")) == pm_pt
    assert {d["id"] for d in store.filter_tracks(artist="Purple Motion", format_="ProTracker", limit=10_000)} == pm_pt
    sid_90s = _truth(store, lambda t: t["format"] == "SID" and 1995 <= t["year"] <= 1999)
    assert 0 < len(sid_90s)
    assert set(await _warm_ids(store, seed=1, format="SID", year_min=1995, year_max=1999)) == sid_90s
    rock = _truth(store, lambda t: "Rock" in t["genre"])
    assert set(await _warm_ids(store, seed=1, genre="Rock")) == rock


def test_untagged_treats_an_empty_list_as_blank():
    store = TrackStore()
    store.upsert_track(_track(1, genre=[]))
    store.upsert_track(_track(2, genre=["  "]))
    store.upsert_track(_track(3, genre=["Rock"]))
    assert set(store.filter_track_ids(untagged="genre")) == {"t000001", "t000002"}


async def test_endpoint_orders_differ_by_seed_and_later_pages_do_not_resolve_the_filter_again(monkeypatch):
    store = _big_store(900)
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    shuffle_order.clear()
    a = await _warm_ids(store, seed=1)
    b = await _warm_ids(store, seed=257)                 # same low byte as seed 1
    c = await _warm_ids(store, seed=2)
    assert sorted(a) == sorted(b) == sorted(c) and a != b and a != c and b != c
    calls = []
    real = store.filter_track_ids
    monkeypatch.setattr(store, "filter_track_ids", lambda **kw: calls.append(1) or real(**kw))
    await _warm_ids(store, seed=1)                       # every page of a cached order
    assert calls == [], "a warm page re-resolved the whole filter on the event loop"


async def test_settings_endpoints_make_the_folder_dedup_checkbox_real(monkeypatch):
    """GET must show the EFFECTIVE value (on, for an install that only has the old
    inert ``False``), and a save must mark the stored value as the user's own."""
    from soniqboom.api import admin
    from soniqboom.core import data
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    store.set_config("dedup_folders", False)             # left behind by the old checkbox
    got = await admin.get_settings()
    assert got["dedup_folders"] is True
    await admin.update_settings({"dedup_folders": False})
    assert store.get_config("dedup_folders_set") is True
    assert await data.folder_dedup_enabled() is False
    assert (await admin.get_settings())["dedup_folders"] is False


# ── memo / cache behaviour the reviews found correct but unpinned ────────────

def test_primary_id_memo_is_invalidated_by_library_changes_and_never_handed_out():
    store = TrackStore()
    for i in range(30):
        store.upsert_track(_track(i, is_duplicate_primary=(i % 3 != 0), duplicate_group_id=f"g{i // 3}"))
    a = store.filter_track_ids(filter_duplicates=True)
    b = store.filter_track_ids(filter_duplicates=True)
    assert a == b and a is not b, "callers must get their own list (Subsonic shuffles it in place)"
    a.clear()
    assert len(store.filter_track_ids(filter_duplicates=True)) == 20
    store.upsert_track(_track(99))                                   # a scan adds a track
    assert "t000099" in store.filter_track_ids(filter_duplicates=True)
    store.update_track_fields("t000099", {"is_duplicate_primary": False, "duplicate_group_id": "g"})
    assert "t000099" not in store.filter_track_ids(filter_duplicates=True)
    store.delete_track("t000001")
    assert "t000001" not in store.filter_track_ids(filter_duplicates=True)


def test_a_file_added_to_a_folder_mid_shuffle_is_dealt_and_counted(monkeypatch):
    from soniqboom.api import fstree
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: TrackStore())
    shuffle_order.clear()
    before = _folder_rows(100)
    total, _ = fstree._shuffled_page(before, "/m/grow", True, False, 12, 0, 50)
    assert total == 100
    after = before + [{"id": "brand-new", "title": "New", "path": "/m/grow/new.mod"}]
    seen, off = [], 0
    while True:
        total, page = fstree._shuffled_page(after, "/m/grow", True, False, 12, off, 50)
        assert total == 101
        if not page:
            break
        seen += [t["id"] for t in page]
        off += 50
        assert off < 1000
    assert "brand-new" in seen and len(seen) == len(set(seen)) == 101
    # The same folder spelled with a trailing slash shares the order (one sort, one entry).
    assert fstree._shuffled_page(after, "/m/grow/", True, False, 12, 0, 50)[1][0]["id"] == seen[0]


def test_folder_page_key_separates_dedup_modes(monkeypatch):
    from soniqboom.api import fstree
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: TrackStore())
    shuffle_order.clear()
    sorts = []
    real = shuffle_order.seeded_order
    monkeypatch.setattr(shuffle_order, "seeded_order", lambda ids, seed: sorts.append(1) or real(ids, seed))
    full, half = _folder_rows(60, prefix="a"), _folder_rows(60, prefix="b")
    for _ in range(2):
        assert fstree._shuffled_page(full, "/m/k", True, False, 5, 0, 60)[0] == 60
        assert {t["id"] for t in fstree._shuffled_page(half, "/m/k", True, True, 5, 0, 60)[1]} == {t["id"] for t in half}
    assert len(sorts) == 2, "the two dedup modes evicted / re-dealt each other"


def test_small_sorts_do_not_queue_behind_a_big_one(monkeypatch):
    import threading, time
    monkeypatch.setattr(shuffle_order, "_SORT_LOCK_MIN_IDS", 100)
    done = {}
    with shuffle_order._sort_lock:                       # a "big sort" is in progress
        t = threading.Thread(target=lambda: done.setdefault("small", shuffle_order.seeded_order([f"t{i}" for i in range(50)], 1)))
        t.start(); t.join(2)
        assert "small" in done, "a 50-id sort waited for the big-sort lock"
        big = threading.Thread(target=lambda: done.setdefault("big", shuffle_order.seeded_order([f"t{i}" for i in range(500)], 1)))
        big.start(); time.sleep(0.05)
        assert "big" not in done                          # …while big ones do take turns
    big.join(2)
    assert len(done["big"]) == 500


def test_normalize_seed_survives_infinity():
    assert shuffle_order.normalize_seed(float("inf")) == 0


def test_trailing_slash_shares_one_folder_order_and_unstable_listings_stay_out_of_the_memos(monkeypatch):
    from soniqboom.api import fstree
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: TrackStore())
    shuffle_order.clear()
    fstree._BYID_MEMO.clear()
    sorts = []
    real = shuffle_order.seeded_order
    monkeypatch.setattr(shuffle_order, "seeded_order", lambda ids, seed: sorts.append(1) or real(ids, seed))
    rows = _folder_rows(6000)
    fstree._shuffled_page(rows, "smb://nas/music", True, False, 3, 0, 50)
    fstree._shuffled_page(rows, "smb://nas/music/", True, False, 3, 50, 50)
    assert len(sorts) == 1, "`/music` and `/music/` were dealt as two different orders"
    fstree._BYID_MEMO.clear()
    for _ in range(3):                                   # a remote listing: a NEW list object every request
        fresh = list(rows)
        fstree._shuffled_page(fresh, "smb://nas/music", True, False, 3, 0, 50, False)
        fstree._shuffled_listing(fresh, "smb://nas/music", True, False, 3, False)
    assert fstree._BYID_MEMO == {}, "per-request listings were pinned in the identity memo"
    fstree._shuffled_page(rows, "smb://nas/music", True, False, 3, 0, 50)          # a stable one is memoised
    assert len(fstree._BYID_MEMO) == 1


def test_the_big_sort_path_is_the_same_permutation_as_the_small_one(monkeypatch):
    # A real library (≥ _SORT_LOCK_MIN_IDS ids) takes the locked branch; it must
    # deal exactly the order the unlocked branch would.
    ids = [f"t{i:05d}" for i in range(12_000)]
    assert len(ids) >= shuffle_order._SORT_LOCK_MIN_IDS
    big = shuffle_order.seeded_order(ids, 77)
    monkeypatch.setattr(shuffle_order, "_SORT_LOCK_MIN_IDS", 10**9)
    small = shuffle_order.seeded_order(ids, 77)
    assert big == small
    assert sorted(big) == ids and big != ids
    assert shuffle_order.seeded_order(ids, 78) != big


def test_folder_pages_of_two_seeds_never_share_a_cached_order(monkeypatch):
    from soniqboom.api import fstree
    shuffle_order.clear()
    rows = [{"id": f"f{i:04d}", "path": f"/m/two/{i}.mod"} for i in range(400)]
    a = [t["id"] for t in fstree._shuffled_page(rows, "/m/two", True, False, 1, 0, 400)[1]]
    b = [t["id"] for t in fstree._shuffled_page(rows, "/m/two", True, False, 2, 0, 400)[1]]
    assert sorted(a) == sorted(b) == [t["id"] for t in rows]
    assert a != b, "a second shuffle of the same folder replayed the first one's order"
    assert [t["id"] for t in fstree._shuffled_page(rows, "/m/two", True, False, 1, 0, 400)[1]] == a


def test_bucketed_big_sort_is_exactly_the_digest_order():
    import hashlib
    ids = [f"t{i:06d}" for i in range(12_000)] + ["\udcff-lone-surrogate", "Öörni ♫ ünïcode"]
    for seed in (1, 77, 2**40 + 3):
        prefix = shuffle_order.normalize_seed(seed).to_bytes(8, "big")
        ref = sorted(ids, key=lambda t: hashlib.sha1(
            prefix + t.encode("utf-8", "surrogatepass")).digest())
        assert shuffle_order._bucketed_order(ids, prefix) == ref
        assert shuffle_order.seeded_order(ids, seed) == ref


def test_bucketed_order_references_the_callers_strings():
    ids = [f"t{i:06d}" for i in range(12_000)]
    out = shuffle_order.seeded_order(ids, 3)
    pool = {id(t) for t in ids}
    assert all(id(t) in pool for t in out), "the cached order holds copies, not the store's ids"
