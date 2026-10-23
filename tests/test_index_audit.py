# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The index-integrity sweep's check (``core/index_health._Audit``): it finds
and heals drift in every kind of derived index, moves nothing when there is
none, yields to the loop in short slices, and checks — instead of discarding
— a library being written while it runs (a shadow rebuild that replays the
writes, sorted lists checked in place outside batch mode)."""
from __future__ import annotations

import asyncio
import copy
import random

import pytest

from soniqboom.core import index_health as ih
from soniqboom.core import scanner
from soniqboom.core.store import SORTED_LISTS, TrackStore

_TITLES = ["Commando", "Uridium 2", "Pokémon theme", "real evil   180s", "", "Song", "Théme"]
_ARTISTS = ["Rob Hubbard", "A • B", "Öörni", "", None, "X"]
_ALBUMS = ["Commando", "", None, "Sanxion", "Pokémon"]
_GENRES = [[], ["Chiptune"], ["Chiptune", "Game"], ["Rock"], None]


def _mk(r: random.Random, i: int, pre: str = "t") -> dict:
    t = {"id": f"{pre}{i:05d}", "path": f"/r{i % 3}/{pre}{i}", "title": r.choice(_TITLES),
         "artist": r.choice(_ARTISTS), "album_artist": r.choice(_ARTISTS),
         "album": r.choice(_ALBUMS), "genre": copy.deepcopy(r.choice(_GENRES)),
         "format": r.choice(["SID", "MP3", "XM"]), "dir_hash": f"d{i % 9}",
         "scan_root_hash": f"r{i % 3}", "added_at": r.randint(0, 900),
         "duration": r.choice([None, 61.5, 180.0, float("nan")]),
         "year": r.choice([None, 1987, "1999", 19950312]), "bpm": r.choice([None, 120.0])}
    if r.random() < 0.2:
        t["duplicate_group_id"] = f"g{i % 37}"
        t["is_duplicate_primary"] = r.random() < 0.5
    if r.random() < 0.2:
        t["game"] = r.choice(["Uridium", "Pokémon"])
    if r.random() < 0.1:
        t["scene_group"] = "Fairlight • Triad"
    return t


def _store(n: int = 1500, seed: int = 1) -> TrackStore:
    r = random.Random(seed)
    s = TrackStore()
    s.upsert_tracks_batch([_mk(r, i) for i in range(n)] + [_mk(r, i, "w") for i in range(n // 3)])
    for i in range(0, n, 5):
        s.record_play(f"t{i:05d}", uid="")
    return s


def _seqs(s) -> tuple:
    return (s._mutation_seq, s._browse_seq, s._duration_seq, s._catalog_seq, s._play_seq)


async def _writer(s, stop, r, counter):
    """Random writes on the ``w*`` tracks (and new ones) until ``stop``."""
    nxt = 90_000
    while not stop.is_set():
        await asyncio.sleep(r.choice([0, 0, 0.0005, 0.002]))
        ws = [tid for tid in s._tracks if tid.startswith("w")]
        op = r.choice(["new", "changed", "delete", "field", "duration", "play", "batch"])
        if op == "new":
            nxt += 1
            s.upsert_tracks_batch([_mk(r, nxt, "w")])
        elif op == "changed" and ws:
            s.upsert_tracks_batch([_mk(r, int(r.choice(ws)[1:]), "w")])
        elif op == "delete" and ws:
            s.delete_track_ids([r.choice(ws)])
        elif op == "field" and ws:
            f = r.choice(["title", "artist", "album", "genre", "year", "is_duplicate_primary"])
            v = {"title": r.choice(_TITLES), "artist": r.choice(_ARTISTS),
                 "album": r.choice(_ALBUMS), "genre": copy.deepcopy(r.choice(_GENRES)),
                 "year": r.choice([None, 2001]), "is_duplicate_primary": r.random() < 0.5}[f]
            s.update_track_fields(r.choice(ws), {f: v})
        elif op == "duration" and ws:
            s.update_track_fields(r.choice(ws), {"duration": r.choice([12.0, 333.0])})
        elif op == "play" and ws:
            s.record_play(r.choice(ws), uid="")
        elif op == "batch" and ws:
            s.enter_batch_mode()
            s.upsert_tracks_batch([_mk(r, int(r.choice(ws)[1:]), "w")])
            nxt += 1
            s.upsert_tracks_batch([_mk(r, nxt, "w")])
            await asyncio.sleep(r.choice([0, 0.003]))
            await scanner._async_exit_batch_mode(s)
        counter[0] += 1


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: False)


async def test_a_clean_check_moves_nothing():
    s = _store()
    seqs = _seqs(s)
    agg = s.aggregate_artists()
    rep = await ih.audit(s)
    assert rep is not None and rep["index_ok"] and rep["mismatches"] == []
    assert set(rep["indexes"]) >= set(SORTED_LISTS) | {"_word_index"}
    assert _seqs(s) == seqs and s.aggregate_artists() is agg      # no memo dropped
    assert s._write_watch is None


def _plant(s, kind: str) -> str:
    tid = "t00042"
    if kind == "tag":
        s._tag_format.setdefault("ghost-format", set()).add(tid)
        return "_tag_format"
    if kind == "word":
        w = next(w for w, ids in s._word_index.items() if tid in ids and len(ids) > 1)
        s._word_index[w].discard(tid)
        return "_word_index"
    if kind == "agg":
        g = sorted(s._agg_genres)[0]
        s._agg_genres[g] += 1
        return "_agg_genres"
    if kind == "by_artist":
        a = sorted(s._agg_albums_by_artist)[0]
        s._agg_albums_by_artist[a]["ghost album"] += 1
        return "_agg_albums_by_artist"
    if kind == "set":
        s._untagged_album ^= {tid}
        return "_untagged_album"
    if kind == "nonprimary":
        s._nonprimary_ids ^= {tid}
        return "_nonprimary_ids"
    if kind == "sorted_missing":
        lst = s._sorted_artist
        del lst[len(lst) // 2]
        return "_sorted_artist"
    if kind == "sorted_stale":
        s._sorted_year.append((10 ** 9, "zzzz-gone"))
        return "_sorted_year"
    if kind == "sorted_disorder":
        lst = s._sorted_added_at
        i = next(i for i in range(len(lst) - 1) if lst[i][0] != lst[-1][0])
        lst[i], lst[-1] = lst[-1], lst[i]
        return "_sorted_added_at"
    if kind == "fold_missing":
        # (the ``game:`` side list holds only accented / oddly spaced titles:
        # no count to compare — each such track is looked up in it)
        lst = s._sorted_title_fold
        assert lst
        del lst[len(lst) // 2]
        return "_sorted_title_fold"
    if kind == "sorted_rekey":
        lst = s._sorted_title
        i = next(i for i, e in enumerate(lst) if e[1] == tid)
        lst[i] = (lst[i][0] + "~", tid)
        lst.sort()
        return "_sorted_title"
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["tag", "word", "agg", "by_artist", "set", "nonprimary",
                                  "sorted_missing", "sorted_stale",
                                  "fold_missing",
                                  "sorted_disorder", "sorted_rekey"])
async def test_planted_drift_is_found_and_healed_under_concurrent_writes(kind):
    s = _store(seed=3)
    where = _plant(s, kind)
    stop, count = asyncio.Event(), [0]
    w = asyncio.create_task(_writer(s, stop, random.Random(kind), count))
    rep = await ih.audit(s)
    stop.set()
    await w
    assert count[0] > 0                                  # (written during the check)
    assert rep is not None and [m["index"] for m in rep["mismatches"]] == [where], rep["mismatches"]
    sample = rep["mismatches"][0]["sample"]
    if kind == "tag":
        assert sample == ["ghost-format"]               # (what drifted, not only that it did)
    assert rep["healed"] and s.verify_indexes()["index_ok"]
    rep = await ih.audit(s)
    assert rep["index_ok"]


@pytest.mark.parametrize("seed", range(4))
async def test_writes_during_a_check_are_checked_not_discarded(seed):
    s = _store(seed=seed)
    for _round in range(3):
        stop, count = asyncio.Event(), [0]
        w = asyncio.create_task(_writer(s, stop, random.Random(seed * 10 + _round), count))
        rep = await ih.audit(s)
        stop.set()
        await w
        assert count[0] > 5
        assert rep is not None and rep["index_ok"], rep and rep["mismatches"]
        assert s._write_watch is None
        assert s.verify_indexes()["index_ok"]


async def test_a_batch_section_is_waited_out(monkeypatch):
    s = _store()
    s.enter_batch_mode()
    s.upsert_tracks_batch([_mk(random.Random(5), 70_000, "w")])

    async def close():
        await asyncio.sleep(0.2)
        await scanner._async_exit_batch_mode(s)
    c = asyncio.create_task(close())
    rep = await ih.audit(s)
    await c
    assert rep is not None and rep["index_ok"]
    monkeypatch.setattr(ih, "_QUIET_WAIT_S", 0.05)          # one that won't end: unfinished
    s.enter_batch_mode()
    assert await ih.audit(s) is None and s._write_watch is None
    await scanner._async_exit_batch_mode(s)


async def test_the_duration_check_reads_the_duration_order_only():
    s = _store()
    s._agg_genres["ghost"] = 1                                # (not looked at)
    rep = await ih.audit(s, full=False)
    assert rep["index_ok"] and set(rep["indexes"]) == {"_sorted_duration"}
    s._sorted_duration.reverse()
    rep = await ih.audit(s, full=False)
    assert [m["index"] for m in rep["mismatches"]] == ["_sorted_duration"]
    assert s._sorted_duration == sorted(s._sorted_duration)


async def test_a_replaced_library_or_a_write_burst_leaves_it_unfinished(monkeypatch):
    s = _store()

    async def replace():
        await asyncio.sleep(0.01)
        s.bulk_load(tracks=dict(s._tracks), waveforms={}, ratings={}, play_stats={},
                    playlists={}, history=[], scan_dirs={}, hash_lookups={}, config={})
        s.rebuild_indexes()
    r = asyncio.create_task(replace())
    assert await ih.audit(s) is None and s._write_watch is None
    await r
    monkeypatch.setattr(ih, "_WATCH_CAP", 3)

    async def burst():
        await asyncio.sleep(0.01)
        s.upsert_tracks_batch([_mk(random.Random(9), 80_000 + i, "w") for i in range(10)])
    b = asyncio.create_task(burst())
    assert await ih.audit(s) is None and s._write_watch is None
    await b


async def test_the_check_yields_in_short_slices(monkeypatch):
    s = _store(n=6000)
    gaps, stop = [], asyncio.Event()
    loop = asyncio.get_running_loop()

    async def ticker():
        last = loop.time()
        while not stop.is_set():
            await asyncio.sleep(0.001)
            now = loop.time()
            gaps.append(now - last)
            last = now
    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    rep = await ih.audit(s)
    stop.set()
    await t
    assert rep["index_ok"]
    assert len(gaps) > 20 and max(gaps) < 0.25           # (loose: CI machines vary)


async def test_the_sweep_heals_and_records(monkeypatch):
    s = _store()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(ih, "_last_swept_seq", None)
    recorded = []
    monkeypatch.setattr(ih, "record", lambda rep, kind, healed: recorded.append((rep, kind)))
    rep = await ih._sweep_once()
    assert rep["index_ok"] and recorded[-1][1] == "sweep"
    assert await ih._sweep_once() is None                     # (nothing changed since)
    s.update_track_fields("t00001", {"title": "renamed"})
    s._tag_album.setdefault("ghost", set()).add("t00002")
    rep = await ih._sweep_once()
    assert not rep["index_ok"] and rep["healed"]
    assert s.verify_indexes()["index_ok"]
    assert await ih._sweep_once() is None                     # (stamped after the heal)


async def test_an_instruments_write_refreshes_the_sample_tokens_without_a_check(monkeypatch):
    """``instruments`` feeds no checked index (browse-exempt: no check runs),
    but the batch-built sample-token index follows every track write."""
    s = TrackStore()
    s.upsert_tracks_batch([{"id": f"m{i}", "path": f"/m/{i}.mod", "title": f"mod {i}",
                            "format": "ProTracker", "genre": [], "added_at": 1,
                            "instruments": ["kick drum", "snare one", f"own {i}"]}
                           for i in range(3)])
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(ih, "_last_swept_seq", ih._gate_seq(s))      # (no check due)
    monkeypatch.setattr(ih, "_sim_built_seq", s._mutation_seq)
    s._build_sim_token_index()
    checks = []

    async def no_audit(store, *, full=True):
        checks.append(full)
    monkeypatch.setattr(ih, "audit", no_audit)
    assert s._sim_tok_count["m0"] == 3
    s.update_track_fields("m0", {"instruments": ["kick drum", "lead", "pad", "bass", "fx"]})
    assert ih._gate_seq(s) == ih._last_swept_seq                       # (the gate didn't move)
    await ih._sweep_once()
    assert checks == []                                                # no check ...
    assert s._sim_tok_count["m0"] == 4         # ... but a refresh ("fx": under 3 chars)
    assert ih._sim_built_seq == s._mutation_seq


@pytest.mark.parametrize("change", ["swap", "missing", "extra"])
async def test_a_large_id_set_is_compared_element_by_element(monkeypatch, change):
    """Sets above ``_BIG_SET`` are compared a slice at a time — a same-size
    swap of one member is still drift."""
    monkeypatch.setattr(ih, "_BIG_SET", 32)
    s = _store(n=600, seed=8)
    ids = s._tag_scan_root_hash["r0"]
    assert len(ids) > 32
    victim = sorted(ids)[3]
    if change in ("swap", "missing"):
        ids.discard(victim)
    if change in ("swap", "extra"):
        ids.add("zz-not-a-track")
    stop, count = asyncio.Event(), [0]
    w = asyncio.create_task(_writer(s, stop, random.Random(change), count))
    rep = await ih.audit(s)
    stop.set()
    await w
    assert [m["index"] for m in rep["mismatches"]] == ["_tag_scan_root_hash"], rep["mismatches"]
    assert rep["mismatches"][0]["sample"] == ["r0"]
    assert s.verify_indexes()["index_ok"]


async def test_a_cancelled_heal_still_finishes_the_sorted_rebuild():
    s = _store(n=800, seed=9)
    del s._sorted_artist[10]                         # (drift a sorted list)
    task = asyncio.create_task(ih.audit(s))
    while not s._batch_mode:                         # (the heal's rebuild has begun)
        await asyncio.sleep(0)
        assert not task.done()
    task.cancel()                                    # (lands inside the rebuild)
    try:
        await task
    except asyncio.CancelledError:
        pass
    for _ in range(2000):                            # (the shielded rebuild runs on)
        if not s._batch_mode and not s._dirty_sorted:
            break
        await asyncio.sleep(0.001)
    assert not s._batch_mode and not s._dirty_sorted and s._batch_depth == 0
    assert s.verify_indexes()["index_ok"]
