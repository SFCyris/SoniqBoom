# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Performance guards of the composer identities and the views on them, on
a 100K-track synthetic library (scene credits in every style, mainstream
tags, placings), heap frozen as the server does after its load:

* the identities are built off the event loop — the loop never pauses long
  while they build (a few milliseconds of snapshot on it, the rest in a
  worker thread, few objects for the cyclic GC);
* after a write on a big library no view waits for the rebuild: the index
  built before answers at once (lists, counts and drills alike) and the new
  one follows;
* every read the new views make answers within a budget a listener can't
  notice.

Budgets are several times the times measured on the dev Mac (2026-11-25:
pause ≤ 52 ms, reads ≤ 55 ms at 120K tracks — see the handover; built
on the loop the pause is the whole build, 169 ms here), so a
loaded test machine passes and a regression of an order of magnitude does
not."""
from __future__ import annotations

import asyncio
import gc
import random
import time

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import artist as artist_api
from soniqboom.api import library, search
from soniqboom.api import tracks as tracks_api
from soniqboom.core import availability, catalogue, composers
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore

N = 100_000
HVSC = "https://6581.nl/C64Music"
ML = "ftp://ftp.modland.com/pub/modules"
# (ms) the longest pause of the loop while the identities build; a read
PAUSE_BUDGET = 100
READ_BUDGET = 150
SLOW_READ_BUDGET = 300                       # (the Discover list: a dozen searches)


def _rows():
    rnd = random.Random(5)
    first = ["Ben", "Rob", "Chris", "Jeroen", "Martin", "Tim", "Søren", "Jürgen", "Karl", "Reyn"]
    last = ["Daglish", "Hubbard", "Hülsbeck", "Tel", "Galway", "Follin", "Øie", "Paasch"]
    people = [(rnd.choice(first), f"{rnd.choice(last)}{i}") for i in range(N // 60)]
    rows = {}
    for k in range(int(N * 0.62)):
        i = rnd.randrange(len(people))
        f, l = people[i]
        r = rnd.random()
        path, root, mt, fmt = f"/m/r/{k // 100}/t{k}.x", "/m/r", 1.7e9, rnd.choice(
            ["SID", "SID", "ProTracker", "SNDH", "FastTracker 2", "SPC", "NSF", "TFMX Pro", "AY"])
        if r < 0.55:
            artist = f"{f} {l}"
        elif r < 0.70:
            artist, path, root, mt, fmt = (f"{l} {f}", f"{HVSC}:/MUSICIANS/X/{l}_{f}/t{k}.sid",
                                           HVSC, 0, "SID")
        elif r < 0.78:
            artist, path, root, mt, fmt = (f"{l}_{f}", f"{ML}:/SNDH/{l}_{f}/t{k}.sndh", ML, 0,
                                           "SNDH")
        elif r < 0.90:
            artist = f"{f} {l} (Handle{i})"
        else:
            j = (i * 7 + 3) % min(len(people), 300)
            artist = f"{f} {l} & {people[j][0]} {people[j][1]}"
        d = {"id": f"r{k}", "path": path, "title": f"Tune {k}", "artist": artist, "format": fmt,
             "added_at": 1_700_000_000 + k, "mtime": mt, "year": 1984 + k % 30,
             "scan_root_hash": root, "dir_hash": f"d{k // 100}", "is_duplicate_primary": True,
             "duration": 120.0}
        if k % 60 == 0:
            d.update(scene_party=f"Party{k % 300} {1990 + k % 30}",
                     scene_compo=rnd.choice(["Amiga Music", "C64 Music", "Oldskool Music"]),
                     scene_place=1 + (k // 60) % 8)
        rows[d["id"]] = d
    for k in range(N - len(rows)):
        rows[f"p{k}"] = {"id": f"p{k}", "path": f"/m/p/{k // 12}/p{k}.mp3", "title": f"Song {k}",
                         "artist": f"Band {k % (N // 40)}", "album": f"Album {k // 12}",
                         "composer": f"Writer {k % (N // 80)}" if k % 3 == 0 else "",
                         "format": "MP3", "added_at": 1_600_000_000 + k, "mtime": 1.7e9,
                         "year": 1970 + k % 50, "scan_root_hash": "/m/p",
                         "dir_hash": f"p{k // 12}", "is_duplicate_primary": True,
                         "duration": 200.0}
    return rows, people


@pytest.fixture(scope="module")
def big():
    rows, people = _rows()
    s = TrackStore()
    s.bulk_load(rows, {}, {}, {}, {}, {}, {}, {}, {})
    s.rebuild_indexes()
    gc.collect()
    gc.freeze()                              # (as the server does after its load)
    yield s, people
    gc.unfreeze()


@pytest.fixture
def installed(big, monkeypatch, tmp_path):
    big_dir = tmp_path
    s, people = big
    from soniqboom.core import demozoo
    monkeypatch.setattr(demozoo, "_db_path", lambda: big_dir / "no-index" / "demozoo.sqlite")
    # (conftest unregisters every catalogue root before each test)
    catalogue.register_root(HVSC, key="hvsc", label="HVSC")
    catalogue.register_root(ML, key="modland", label="Modland")
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    yield s, people
    availability.reset_for_tests()
    catalogue.unregister_root(HVSC)
    catalogue.unregister_root(ML)


async def _measured(coro):
    """(result, elapsed ms, the loop's longest pause ms) while ``coro`` runs."""
    gaps: list[float] = []
    done = False

    async def tick():
        last = time.perf_counter()
        while not done:
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
    t = asyncio.create_task(tick())
    await asyncio.sleep(0.01)
    t0 = time.perf_counter()
    res = await coro
    el = (time.perf_counter() - t0) * 1000
    done = True
    await t
    return res, el, max(gaps) * 1000 if gaps else 0.0


def _client():
    app = FastAPI()
    for m in (tracks_api, library, search, artist_api):
        app.include_router(m.router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_the_identities_build_off_the_loop(installed):
    s, _people = installed
    s._facet_memo.pop("composer:index", None)
    t0 = time.perf_counter()
    composers.snapshot(s)
    snap_ms = (time.perf_counter() - t0) * 1000
    assert snap_ms < 50, f"the snapshot took {snap_ms:.0f} ms of the loop"
    idx, el, pause = await _measured(s.composer_index_async())
    assert len(idx.by_key) > 1000
    assert pause < PAUSE_BUDGET, f"the loop paused {pause:.0f} ms (build {el:.0f} ms)"
    # (built on the loop, its longest pause would be the build itself)
    assert pause < 0.6 * el, f"the loop paused {pause:.0f} ms of a {el:.0f} ms build"
    # (few objects left for the cyclic GC: about two per musician)
    tracked = sum(1 for o in gc.get_objects() if isinstance(o, composers.Identity))
    assert tracked <= 2 * len(idx.by_key)


async def test_after_a_write_no_view_waits_for_the_rebuild(installed):
    s, _people = installed
    timer = composers._scheduled.pop(s, None)
    if timer is not None:
        timer.cancel()                                          # (an earlier test's)
    s._composer_install(s._composer_stamp(), composers.build(s))
    composers._last_build[s] = (time.monotonic() - 60, 0.5)     # (a build of half a second)
    before = s.composer_index()
    assert s.composer_index_fresh()
    s.update_track_fields("r5", {"artist": "A Brand New Name"})
    async with _client() as c:
        r, el, pause = await _measured(c.get("/api/library/composers"))
        assert r.status_code == 200 and el < READ_BUDGET, el
        counts = (await c.get("/api/library/counts")).json()
        # (the index built before: the list, the counts and the drills agree)
        assert counts["composers"] == len(r.json()) == len(before.by_key)
        assert s.composer_index() is before
        # (the rebuild starts after a quiet moment, by its timer — and lands)
        assert composers._scheduled.get(s) is not None
        for _ in range(200):
            if s.composer_index_fresh():
                break
            await asyncio.sleep(0.05)
        assert s.composer_index_fresh(), "the scheduled rebuild never ran"
        fresh = s.composer_index()
        assert fresh is not before and fresh.find("A Brand New Name") is not None
        rows = (await c.get("/api/library/composers")).json()
        assert any(x["composer"] == "A Brand New Name" for x in rows)


@pytest.mark.parametrize("url,params,budget", [
    ("/api/library/composers", {}, READ_BUDGET),
    ("/api/library/artists", {}, READ_BUDGET),
    ("/api/library/counts", {}, READ_BUDGET),
    ("/api/library/chips", {}, READ_BUDGET),
    ("/api/search/page", {"q": "composer:hubbard", "limit": 200}, READ_BUDGET),
    ("/api/search/page", {"q": "place:1", "limit": 200}, READ_BUDGET),
    ("/api/search/page", {"q": 'compo:"amiga music"', "limit": 200}, READ_BUDGET),
    ("/api/search/page", {"q": "chip:paula", "limit": 200}, READ_BUDGET),
    ("/api/search/filter", {"q": "place:<=3", "order": "compo", "limit": 2000}, READ_BUDGET),
    ("/api/library/compo-summary", {"place_max": 3}, READ_BUDGET),
    ("/api/artist/composers", {"q": "daglish"}, READ_BUDGET),
    ("/api/library/discover", {}, SLOW_READ_BUDGET),
])
async def test_reads_within_budget(installed, url, params, budget):
    s, _people = installed
    await s.composer_index_async()
    async with _client() as c:
        await c.get(url, params=params)                 # (warm: the memos)
        times = []
        pauses = []
        for _ in range(3):
            r, el, pause = await _measured(c.get(url, params=params))
            assert r.status_code == 200, r.text[:200]
            times.append(el)
            pauses.append(pause)
    assert min(times) < budget, (url, params, times)
    assert min(pauses) < budget + 20, (url, params, pauses)


async def test_the_composer_page_within_budget(installed):
    s, people = installed
    idx = await s.composer_index_async()
    name = max(idx.by_key.values(), key=lambda i: len(i.ids)).name
    async with _client() as c:
        await c.get("/api/artist/composer", params={"name": name})
        _r, page_ms, _p = await _measured(c.get("/api/artist/composer", params={"name": name}))
        r, list_ms, _p = await _measured(c.get("/api/search/filter", params={
            "composer": name, "order": "platform", "limit": 2000}))
    assert r.status_code == 200 and len(r.json()) == len(idx.find(name).ids)
    assert page_ms < READ_BUDGET and list_ms < READ_BUDGET, (page_ms, list_ms)


async def test_deletes_on_a_big_library_cost_no_wait(installed):
    """Tracks deleted while the index is stale (a rebuild on its way): the
    list, the counts and the drill answer at once and agree — the deleted
    tracks subtracted from only the musicians who had them."""
    s, _people = installed
    timer = composers._scheduled.pop(s, None)
    if timer is not None:
        timer.cancel()
    s._composer_install(s._composer_stamp(), composers.build(s))
    composers._last_build[s] = (time.monotonic(), 0.5)          # (a slow build, just done)
    victim = max(s.composer_index().by_key.values(), key=lambda i: len(i.ids))
    gone = sorted(victim.ids)[:3]
    for tid in gone:
        s.delete_track(tid)
    async with _client() as c:
        for url, params in (("/api/library/composers", {}), ("/api/library/counts", {}),
                            ("/api/search/filter", {"composer": victim.name, "limit": 2000})):
            _r, el, pause = await _measured(c.get(url, params=params))
            assert el < READ_BUDGET and pause < READ_BUDGET, (url, el, pause)
        rows = {r["composer"]: r["count"] for r in (await c.get("/api/library/composers")).json()}
        drill = (await c.get("/api/search/filter", params={"composer": victim.name,
                                                           "limit": 2000})).json()
    assert not s.composer_index_fresh()
    assert rows[victim.name] == len(drill) == len(victim.ids) - 3
    timer = composers._scheduled.pop(s, None)
    if timer is not None:
        timer.cancel()


async def test_a_request_during_a_rebuild_keeps_most_of_the_cpu(installed):
    """While the identities rebuild in the worker thread, the loop's own work
    (an Artists aggregation and fold) takes little longer than alone — the
    worker naps (``_YIELD_SLEEP``) instead of taking half of the GIL."""
    import statistics
    s, _people = installed
    await s.composer_index_async()

    def loop_work():
        s._agg_cache.clear()
        s._agg_x_cache.clear()
        t0 = time.perf_counter()
        composers.fold_artist_rows(s, s.aggregate_artists())
        return time.perf_counter() - t0
    async def measure():
        alone = statistics.median(loop_work() for _ in range(5))
        during = []
        for _ in range(3):                              # (a few rebuilds: enough samples)
            fut = asyncio.ensure_future(asyncio.to_thread(composers.build_from, composers.snapshot(s)))
            await asyncio.sleep(0.02)
            while not fut.done():
                during.append(loop_work())
                await asyncio.sleep(0.005)
            await fut
        assert len(during) >= 3, "the rebuild ended before the loop could work beside it"
        return statistics.median(during) / alone, alone
    # The best of three attempts: other test processes of a parallel full run
    # load the CPU in bursts (4.67× seen once), while a worker that takes half
    # the GIL is slow in EVERY attempt.
    tries = []
    for _ in range(3):
        ratio, alone = await measure()
        tries.append(ratio)
        if ratio < 1.7:
            break
    # (1.85 under a loaded 8-worker full run — 1.71 × 3 seen once; a worker that
    # takes half of the GIL is ~2× in every try)
    assert min(tries) < 1.85, f"loop work {min(tries):.2f}× slower during a rebuild ({alone * 1000:.0f} ms alone; tries {tries})"


def _fold_reference(store, rows, *, primary_only=False, exclude=None, swap=None):
    """``fold_artist_rows`` as it was before the 2.3.0 speed-up (a full
    re-sort of every row) — what the fast fold must equal, row for row."""
    idx = composers.index_for(store)
    if not idx.by_artist:
        return rows
    counts = {r["composer"]: r["count"] for r in
              store.aggregate_facet("composer", primary_only=primary_only, exclude=exclude,
                                    swap=swap)}
    out, done = [], set()
    for r in rows:
        if r.get("label"):
            out.append(r)
            continue
        ik = idx.by_artist.get((r.get("artist") or "").strip().lower())
        ident = idx.by_key.get(ik) if ik else None
        if ident is None:
            out.append(r)
            continue
        if ident.key in done:
            continue
        done.add(ident.key)
        n = counts.get(ident.name)
        if n:
            out.append({"artist": ident.name, "count": n, "composer": ident.name})
    out.sort(key=lambda x: (bool(x.get("label")), (x.get("artist") or "").lower()))
    return out


async def test_the_fast_fold_equals_the_full_resort(installed):
    """The fold keeps the unfolded rows in order and merges the sorted folded
    rows in (no re-sort of 35K rows), and ``/library/counts`` counts it
    without building it: both equal the old full re-sort exactly — order,
    ties, labels — and the count its length, also after a write and with
    the dedup on."""
    s, _people = installed
    await s.composer_index_async()
    idx = composers.index_for(s)
    assert idx.by_artist
    for kw in ({}, {"primary_only": True}):
        rows = s.aggregate_artists(**kw)
        want = _fold_reference(s, rows, **kw)
        got = composers.fold_artist_rows(s, rows, **kw)
        assert got == want and len(got) < len(rows)
        assert composers.folded_artist_count(s, rows, **kw) == len(want)
    rows = list(s.aggregate_artists())
    # A musician whose list name is a tag left unfolded (its key dropped from
    # the index for this check) sorts equal to his folded row; another of his
    # tags sorts first: the stable sort puts the folded row — at that tag
    # row's place — before the unfolded rows spelled like it.
    cands = sorted((i for i in idx.by_key.values()
                    if len(i.artist_keys) > 1 and i.name.lower() in i.artist_keys
                    and min(k for k in i.artist_keys if k != i.name.lower()) < i.name.lower()),
                   key=lambda i: i.key)
    assert cands
    ident = cands[len(cands) // 2]
    saved = idx.by_artist.pop(ident.name.lower())
    try:
        crafted = (rows + [{"artist": ident.name.upper(), "count": 1}]
                   + [{"artist": "", "count": 3, "label": "[No Artist]"},
                      {"artist": "a", "count": 4, "label": "[Other]"}])
        crafted.sort(key=lambda x: (x.get("artist") or "").lower())   # (as aggregate_artists)
        want = _fold_reference(s, crafted)
        ties = [r for r in want if r["artist"].lower() == ident.name.lower()]
        assert len(ties) >= 2 and ties[0].get("composer") == ident.name   # (the case is real)
        assert composers.fold_artist_rows(s, crafted) == want
        assert (composers.folded_artist_count(s, crafted)
                == sum(1 for r in want if not r.get("label")))
    finally:
        idx.by_artist[ident.name.lower()] = saved
    # after a write (another input)
    victim = next(r for r in rows if r["artist"].strip().lower() in idx.by_artist)
    tid = min(s._tag_artist[victim["artist"].lower()])
    s.update_track_fields(tid, {"artist": "Some New Spelling 77"})
    rows = s.aggregate_artists()
    assert composers.fold_artist_rows(s, rows) == _fold_reference(s, rows)
    assert composers.folded_artist_count(s, rows) == len(_fold_reference(s, rows))
