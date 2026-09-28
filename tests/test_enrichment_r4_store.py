# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-4 store fixes:

* ``game:`` folds accents and whitespace runs like plain search does
  (``game:pokemon`` finds "Pokémon", ``game:"real evil 180s"`` finds
  "real evil      180s") through two small sorted side lists that every
  index path (insert, field update, batch exit, full rebuild, the async
  shadow rebuild) keeps byte-identical;
* deep pages of a sorted All Tracks view with duplicates hidden slice a
  memoised primary-only order instead of walking ``offset`` rows;
* ``index_generation`` — the one tuple an index rebuild compares to detect a
  write that landed while it built."""
from __future__ import annotations

import asyncio
import random

import pytest

from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _mk(tid, **kw):
    t = {"id": tid, "path": f"/m/{tid}.mod", "title": "", "artist": "", "album": "",
         "album_artist": "", "format": "ProTracker", "genre": [], "added_at": 1}
    t.update(kw)
    return t


# (Title matches need a SID / Atari tune without a game — ``game:`` reads a
# retro track's title only there; album matches work for any retro format.)
_GAMES = [
    _mk("acc_t", title="Pokémon Title", format="SID"),
    _mk("asc_t", title="Pokemon Title", format="SID"),
    _mk("acc_a", title="Intro", album="Pokémon Red", album_source="tag", format="GBS"),
    _mk("tra", title="Träumerei", format="SID"),
    _mk("spc", title="real evil      180s", format="YM"),
    _mk("tab", title="Tab\tSeparated  Game", format="SNDH"),
    _mk("the", title="Intro", album="The Dämon Hunt", album_source="tag", format="SPC"),
    _mk("jp", title="がんばれ", format="SID"),
    _mk("other", title="Paradroid", format="SID"),
]


@pytest.fixture
def games():
    s = TrackStore()
    s.upsert_tracks_batch([dict(t) for t in _GAMES])
    return s


@pytest.mark.parametrize("q,expect", [
    ("pokemon", {"acc_t", "asc_t", "acc_a"}),
    ("pokémon", {"acc_t", "asc_t", "acc_a"}),
    ("POKÉMON red", {"acc_a"}),
    ("traumerei", {"tra"}),
    ("träumerei", {"tra"}),
    ("real evil 180s", {"spc"}),
    ("real   evil", {"spc"}),
    ("tab separated game", {"tab"}),
    ("damon hunt", {"the"}),                   # article + accent together
    ("the damon", {"the"}),
    ("がんばれ", {"jp"}),                        # non-Latin: unchanged
    ("paradroid", {"other"}),
    ("pokemon x", set()),
])
def test_game_folds_accents_and_whitespace(games, q, expect):
    assert games._candidate_ids(game=q) == expect


def test_side_lists_hold_only_the_differing_values(games):
    assert {tid for _k, tid in games._sorted_title_fold} == {"acc_t", "tra", "spc", "tab"}
    # An album's game is keyed folded in the game index — no album side list.
    assert "pokemon red" in games._tag_game
    assert not hasattr(games, "_sorted_album_fold")


def _assert_parity(s):
    rep = s.verify_indexes()
    assert rep["index_ok"], rep["mismatches"]


def test_side_lists_follow_every_write_path(games):
    s = games
    s.upsert_track(_mk("new", title="Élan Vital", format="SID"))
    assert s._candidate_ids(game="elan") == {"new"}
    s.update_track_fields("new", {"title": "Plain"})           # leaves the side list
    assert s._candidate_ids(game="elan") == set()
    s.update_track_fields("other", {"album": "Café  Racer", "album_source": "folder"})
    assert s._candidate_ids(game="cafe racer") == {"other"}
    s.delete_track("acc_t")
    assert s._candidate_ids(game="pokemon") == {"asc_t", "acc_a"}
    _assert_parity(s)
    # batch-mode field updates (merge of the re-keyed entries on exit)
    s.enter_batch_mode()
    s.update_track_fields_batch([("tra", {"title": "Träumerei II"}),
                                 ("asc_t", {"title": "Pokémon Blue"})])
    s.exit_batch_mode()
    assert s._candidate_ids(game="pokemon blue") == {"asc_t"}
    _assert_parity(s)
    s.rebuild_indexes()
    _assert_parity(s)
    assert s._candidate_ids(game="traumerei ii") == {"tra"}


async def test_scanner_full_async_rebuild_builds_the_side_lists(games):
    from soniqboom.core import scanner
    s = games
    s.enter_batch_mode()
    s.upsert_tracks_batch([_mk("ins", title="Ünder  Pressure", format="SID")])  # insert → full
    assert s._dirty_sorted == set(store_mod.SORTED_LISTS)
    await scanner._async_exit_batch_mode(s)
    assert s._candidate_ids(game="under pressure") == {"ins"}
    _assert_parity(s)


async def test_async_shadow_rebuild_swaps_identical_side_lists(games, monkeypatch):
    from soniqboom.core import data
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: games)
    before = (list(games._sorted_title_fold), dict(games._tag_game))
    rep = await data.rebuild_indexes()
    assert rep["index_ok"], rep["mismatches"]
    assert (games._sorted_title_fold, games._tag_game) == before


def test_whole_titles_do_not_fill_the_token_fold_cache():
    store_mod._fold_nonascii.cache_clear()
    s = TrackStore()
    s.upsert_track(_mk("x", title="Ünique Wörds Here", format="SID"))
    s._candidate_ids(game="unique words")
    cached = store_mod._fold_nonascii.cache_info().currsize
    # Only the per-token folds (ünique, wörds) — never the whole title / query.
    assert cached == 2


def test_random_library_parity():
    r = random.Random(4)
    words = ["pokémon", "  café", "tune", "\tlevel", "ÖÖrni", "the", "zone  2", "a"]
    s = TrackStore()
    for i in range(400):
        s.upsert_track(_mk(f"r{i}", title=" ".join(r.choice(words) for _ in range(3)),
                           album=r.choice(["", "Pokémon Red", "Plain", "Día  de", "x y"]),
                           album_source=r.choice([None, "songdb", "tag"]),
                           format=r.choice(["SID", "YM", "ProTracker", "SPC", "MP3"])))
    for i in range(0, 400, 3):
        s.update_track_fields(f"r{i}", {"title": r.choice(words) + " again"})
    _assert_parity(s)
    for q in ("pokemon", "cafe", "the pokemon", "oorni"):
        prefixes = (q, q[4:]) if q.startswith("the ") else (q, "the " + q)

        def hit(t):
            game = t.get("game") or ""
            if game:
                return store_mod._game_key(game).startswith(prefixes)
            return (t["format"] in ("SID", "YM")
                    and store_mod._game_key(t.get("title")).startswith(prefixes))
        want = {tid for tid, t in s._tracks.items() if hit(t)}
        assert all((t.get("game") or "") == ((t.get("album") or "") if t.get("album_source")
                                              and (t.get("album") or "").strip() else "")
                   for t in s._tracks.values())
        assert want and s._candidate_ids(game=q) == want, q


# ── r4-enr-9: deep sorted pages ──────────────────────────────────────────────

def _old_walk(s, limit, offset, *, filter_duplicates, sort_by, sort_order):
    """The pre-round-4 general path of ``_paginate_all`` (the oracle)."""
    idx = s._pick_sort_index(sort_by, filter_duplicates=filter_duplicates)
    walk = reversed(idx) if s._is_descending(sort_by, sort_order) else iter(idx)
    skipped, out = 0, []
    for _v, tid in walk:
        if filter_duplicates:
            t = s._tracks.get(tid)
            if not t or not t.get("is_duplicate_primary", True):
                continue
        if skipped < offset:
            skipped += 1
            continue
        out.append(tid)
        if len(out) >= limit:
            break
    return [tid for tid in out if tid in s._tracks]


@pytest.fixture
def lib(monkeypatch):
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 50)
    r = random.Random(9)
    s = TrackStore()
    s.upsert_tracks_batch([
        _mk(f"p{i:04d}", title=r.choice(["Alpha", "beta", "Gamma", "", "Δelta"]) + f" {i % 37}",
            artist=r.choice(["A", "b", "C", ""]), album=r.choice(["X", "y", ""]),
            year=r.choice([None, 1987, 1991, 2001]), duration=r.choice([0.0, 60.0, 61.5, 300.0]),
            added_at=r.randint(1, 50), is_duplicate_primary=r.random() > 0.3)
        for i in range(600)])
    return s


@pytest.mark.parametrize("sort_by", ["title", "artist", "album", "year", "duration", "format"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("dedup", [False, True])
def test_deep_pages_match_the_walk(lib, sort_by, order, dedup):
    for offset in (0, 49, 50, 51, 200, 395, 590, 700):
        for limit in (1, 25, 100):
            got = [d["id"] for d in lib._paginate_all(limit, offset, filter_duplicates=dedup,
                                                      sort_by=sort_by, sort_order=order)]
            assert got == _old_walk(lib, limit, offset, filter_duplicates=dedup,
                                    sort_by=sort_by, sort_order=order), (offset, limit)


def test_primary_order_memo_follows_writes(lib):
    page = lambda: [d["id"] for d in lib._paginate_all(  # noqa: E731
        10, 100, filter_duplicates=True, sort_by="duration", sort_order="asc")]
    first = page()
    memo = lib._primary_order_memo["_sorted_duration"]
    assert page() == first and lib._primary_order_memo["_sorted_duration"] is memo   # a hit
    # A duration-only write (render backfill) re-keys _sorted_duration without
    # bumping _mutation_seq — the memo must still be rebuilt.
    seq = lib._mutation_seq
    lib.update_track_fields(first[0], {"duration": 9999.0})
    assert lib._mutation_seq == seq
    assert page() == _old_walk(lib, 10, 100, filter_duplicates=True,
                               sort_by="duration", sort_order="asc")
    assert first[0] not in page()
    # An upsert (new track) and a primary flip are seen too.
    lib.upsert_track(_mk("zz", title="Alpha 0", duration=60.0, added_at=3))
    lib.update_track_fields(first[1], {"is_duplicate_primary": False})
    assert page() == _old_walk(lib, 10, 100, filter_duplicates=True,
                               sort_by="duration", sort_order="asc")


def test_a_list_replaced_at_batch_exit_is_not_served_stale(lib):
    kw = dict(filter_duplicates=True, sort_by="title", sort_order="asc")
    lib.enter_batch_mode()
    lib.upsert_tracks_batch([_mk("new", title="Aaaa first", added_at=5)])
    lib._paginate_all(10, 60, **kw)             # memo built from the pre-exit list
    lib.exit_batch_mode()                       # replaces the list, no new write
    assert [d["id"] for d in lib._paginate_all(10, 60, **kw)] == \
        _old_walk(lib, 10, 60, **kw)
