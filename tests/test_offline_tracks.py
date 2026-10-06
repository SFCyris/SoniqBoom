# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → "Tracks on unavailable drives" (core/availability.py).

* What "can't play right now" means: the track's scan root is offline (status
  ``unavailable``, or a network share with no connected source) and no local
  copy can serve it — a cached remote file, a cached remote archive or a subset
  of it built for the member.  A root coming back, a cached copy appearing or
  being evicted: the state follows (cold and warm, different inputs).
* Mark: rows carry ``offline``; Hide: searches / lists / counts / group counts
  leave those tracks out with exact paging; Show: everything as before.
* Playlists and folders never hide; shuffles, radio and similar tracks never
  deal them (Mark / Hide); ``playable=1`` pages skip them with exact positions.
* The setting (admin API, ui-config), the change notification, the probes.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import itertools
import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from soniqboom.api import fstree, library, playlist as playlist_api, search, smart
from soniqboom.api import tracks as tracks_api
from soniqboom.core import availability, remote_cache
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore
from soniqboom.models.track import Track


def ph(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def tid_of(path: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, path))


def _row(path: str, root: str, i: int, **kw) -> dict:
    d = path.rsplit("/", 1)[0] if "::" not in path else path.split("::")[0].rsplit("/", 1)[0]
    row = Track(id=tid_of(path), path=path, title=f"Song {i:04d} zork", artist=f"Artist {i % 7}",
                album=f"Album {i % 5}", genre=[f"Genre {i % 3}"], year=1990 + i % 4,
                duration=100.0 + i, format=["ProTracker", "SID", "MP3"][i % 3],
                added_at=1000 + i, mtime=1.7e9, file_size=1000, dir_hash=ph(d),
                scan_root_hash=ph(root)).model_dump()
    row.pop("embedding")
    row.update(kw)
    return row


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """Two local roots on disk (A: 60 tracks, B: 40 tracks) and an empty remote
    cache, installed as THE store / cache; list caches emptied."""
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    cache = remote_cache.RemoteCache(tmp_path / "rc", 512)
    monkeypatch.setattr(remote_cache, "_cache", cache)
    for name in ("_TRACKS_META_CACHE", "_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO"):
        monkeypatch.setattr(fstree, name, {})
    monkeypatch.setattr(smart, "_MOST_PLAYED_MEMO", {"seq": -1, "ranked": []})
    monkeypatch.setattr(smart, "_TOP_RATED_MEMO", {"seq": -1, "ranked": []})
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    from soniqboom.core import shuffle_order
    shuffle_order.clear()                       # (orders are keyed by seed, not by store)
    ra, rb = str(a), str(b)
    s.upsert_scan_dir(ra)
    s.upsert_scan_dir(rb)
    rows_a = [_row(f"{ra}/d{i // 10}/t{i}.mod", ra, i) for i in range(60)]
    rows_b = [_row(f"{rb}/d{i // 10}/t{i}.mod", rb, 100 + i) for i in range(40)]
    for r in rows_a + rows_b:
        s.upsert_track(copy.deepcopy(r))
    for r in rows_a:
        (a / r["path"][len(ra) + 1:]).parent.mkdir(parents=True, exist_ok=True)
    for r in rows_b:
        (b / r["path"][len(rb) + 1:]).parent.mkdir(parents=True, exist_ok=True)

    class Lib:
        store = s
        root_a, root_b = ra, rb
        ids_a = [r["id"] for r in rows_a]
        ids_b = [r["id"] for r in rows_b]

        def offline(self, root=None):
            s.set_scan_dir_status(root or rb, "unavailable")

        def online(self, root=None):
            s.set_scan_dir_status(root or rb, "ok")

        def mode(self, m):
            s.set_config(availability.MODE_KEY, m)
    yield Lib()
    availability.reset_for_tests()


def _app() -> FastAPI:
    app = FastAPI()
    for mod in (tracks_api, search, smart, library, fstree, playlist_api):
        app.include_router(mod.router, prefix="/api")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t")


async def _get(url: str, **kw):
    async with _client() as c:
        r = await c.get(url, **kw)
    assert r.status_code == 200, (url, r.status_code, r.text[:300])
    return r.json()


async def _post(url: str, body):
    async with _client() as c:
        r = await c.post(url, json=body)
    assert r.status_code == 200, (url, r.status_code, r.text[:300])
    return r.json()


def _req() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


# ── what "can't play right now" is ───────────────────────────────────────────

def test_a_local_root_that_goes_offline_and_comes_back(lib):
    snap = availability.snapshot()
    assert not snap.dead and not snap.unavailable
    lib.offline()
    snap = availability.snapshot()
    assert snap.unavailable == frozenset(lib.ids_b)
    assert snap.kind_of(lib.store.get_track(lib.ids_b[0])) == "drive"
    assert snap.kind_of(lib.store.get_track(lib.ids_a[0])) is None
    seq_off = snap.seq
    assert availability.snapshot() is snap                    # memoised while nothing moves
    lib.online()
    snap2 = availability.snapshot()
    assert not snap2.unavailable and snap2.seq != seq_off
    # …and a different root going offline is a new state again (no stale reuse).
    lib.offline(lib.root_a)
    assert availability.snapshot().unavailable == frozenset(lib.ids_a)


def test_a_track_added_to_an_offline_root_is_unavailable_too(lib):
    lib.offline()
    n = len(availability.snapshot().unavailable)
    new = _row(f"{lib.root_b}/late/new.mod", lib.root_b, 999)
    lib.store.upsert_track(new)
    assert new["id"] in availability.snapshot().unavailable
    assert len(availability.snapshot().unavailable) == n + 1


def test_remote_share_without_a_source_plays_its_cached_files_only(lib, tmp_path):
    root = "ftp://nas.invalid/music"
    s = lib.store
    s.upsert_scan_dir(root, network_share_id="nas")            # status ok, but no source registered
    paths = [f"{root}:/Artist/t{i}.mp3" for i in range(5)]
    for i, p in enumerate(paths):
        s.upsert_track(_row(p, root, 500 + i))
    cache = remote_cache.get_cache()
    src = tmp_path / "x.mp3"
    src.write_bytes(b"ID3data")
    cache.put_file(root, "/Artist/t1.mp3", src)               # cold: one file cached
    snap = availability.snapshot()
    assert snap.dead[ph(root)] == "share"
    un = snap.unavailable
    assert tid_of(paths[1]) not in un
    assert {tid_of(p) for i, p in enumerate(paths) if i != 1} <= un
    # warm, a different input: a second file lands in the cache → state follows
    src2 = tmp_path / "y.mp3"
    src2.write_bytes(b"ID3data2")
    cache.put_file(root, "/Artist/t3.mp3", src2)
    un2 = availability.snapshot().unavailable
    assert tid_of(paths[3]) not in un2 and tid_of(paths[1]) not in un2
    # eviction of t1's copy → it can't play any more
    cache.invalidate_share(root)
    un3 = availability.snapshot().unavailable
    assert {tid_of(p) for p in paths} <= un3


def test_remote_archive_members_play_from_a_cached_archive_or_subset(lib, tmp_path):
    from soniqboom.core.remote_cache import subset_name
    root = "smb://nas.invalid/share"
    s = lib.store
    s.upsert_scan_dir(root, network_share_id="smb1")
    members = {
        "a1": f"{root}:/mods/a.zip::one.mod", "a2": f"{root}:/mods/a.zip::two.mod",
        "b1": f"{root}:/mods/b.zip::one.mod", "b2": f"{root}:/mods/b.zip::two.mod",
        "c1": f"{root}:/mods/c.lha::x.mod",
    }
    for i, p in enumerate(members.values()):
        s.upsert_track(_row(p, root, 700 + i))
    cache = remote_cache.get_cache()
    whole = tmp_path / "a.zip"
    whole.write_bytes(b"PK..")
    cache.put_file(root, "/mods/a.zip", whole)                # the whole archive a.zip
    sub = tmp_path / "sub.zip"
    sub.write_bytes(b"PK..")
    cache.put_file(root, subset_name("/mods/b.zip", "one.mod", 4096, 1700000000), sub)
    un = availability.snapshot().unavailable
    assert tid_of(members["a1"]) not in un and tid_of(members["a2"]) not in un
    assert tid_of(members["b1"]) not in un                    # its subset is cached
    assert tid_of(members["b2"]) in un                        # another member: no
    assert tid_of(members["c1"]) in un
    # the subset retired (the archive changed): b1 can't play any more
    cache.drop_stale_subsets(root, "/mods/b.zip", 9999, 1)
    assert tid_of(members["b1"]) in availability.snapshot().unavailable


@pytest.mark.asyncio
async def test_a_big_roots_archive_members_are_listed_off_the_event_loop(lib, tmp_path, monkeypatch):
    """On the event loop a big root's archive-member list is built in a worker
    thread: meanwhile its cached-archive members count as unavailable, then the
    finished list re-snapshots them as playable and tells the clients."""
    monkeypatch.setattr(availability, "_MEMBERS_SYNC_MAX", 3)
    told = []
    monkeypatch.setattr(availability, "notify_changed", lambda: told.append(1))
    root = "smb://big.invalid/share"
    s = lib.store
    s.upsert_scan_dir(root, network_share_id="big")
    members = [f"{root}:/z/a.zip::m{i}.mod" for i in range(4)]
    for i, p in enumerate(members + [f"{root}:/loose{i}.mod" for i in range(3)]):
        s.upsert_track(_row(p, root, 800 + i))
    whole = tmp_path / "a.zip"
    whole.write_bytes(b"PK")
    remote_cache.get_cache().put_file(root, "/z/a.zip", whole)
    first = availability.snapshot()
    assert all(tid_of(m) in first.unavailable for m in members)      # not known yet
    for _ in range(200):
        if availability.snapshot() is not first:
            break
        await asyncio.sleep(0.01)
    second = availability.snapshot()
    assert second.seq != first.seq and told
    assert not any(tid_of(m) in second.unavailable for m in members)
    assert tid_of(f"{root}:/loose0.mod") in second.unavailable


def test_a_local_archive_member_on_an_offline_drive_is_unavailable(lib):
    # Its extract is reused only while the archive itself can be checked
    # (stream._get_or_extract_zip_member) — so an offline drive's members don't play.
    p = f"{lib.root_b}/zips/pack.zip::song.mod"
    lib.store.upsert_track(_row(p, lib.root_b, 900))
    lib.offline()
    assert tid_of(p) in availability.snapshot().unavailable


def test_a_share_the_monitor_flagged_counts_even_with_a_registered_source(lib, monkeypatch):
    from soniqboom.core import filesource
    root = "ftp://nas2.invalid/m"
    s = lib.store
    s.upsert_scan_dir(root, network_share_id="nas2")
    s.upsert_track(_row(f"{root}:/x.mp3", root, 1))
    monkeypatch.setitem(filesource._active_sources, root, object())   # registered → reachable
    assert ph(root) not in availability.snapshot().dead
    s.set_scan_dir_status(root, "unavailable")                         # the health monitor's flag
    assert availability.snapshot().dead.get(ph(root)) == "share"


def test_a_catalogue_root_is_kind_catalogue(lib, tmp_path, monkeypatch):
    from soniqboom.core import catalogue
    root = "ftp://cat.invalid/pub/mods"
    catalogue.register_root(root, key="testcat", label="TestCat")
    try:
        s = lib.store
        s.upsert_scan_dir(root, status="unavailable")
        pa, pb = f"{root}:/A/one.mod", f"{root}:/A/two.mod"
        s.upsert_track(_row(pa, root, 1))
        s.upsert_track(_row(pb, root, 2))
        f = tmp_path / "one.mod"
        f.write_bytes(b"M.K.")
        remote_cache.get_cache().put_file(root, "/A/one.mod", f)   # played before → cached
        snap = availability.snapshot()
        assert snap.dead[ph(root)] == "catalogue"
        assert snap.kind_of(s.get_track(tid_of(pb))) == "catalogue"
        assert snap.kind_of(s.get_track(tid_of(pa))) is None
        st = availability.state()
        cat = next(r for r in st["roots"] if r["path"] == root)
        assert cat["label"] == "TestCat" and cat["tracks"] == 2 and cat["unavailable"] == 1
    finally:
        catalogue.unregister_root(root)


# ── Mark / Hide / Show on the lists ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_flags_rows_everywhere_and_hides_nothing(lib):
    lib.offline()
    lib.mode("mark")
    rows = await _get("/api/tracks?limit=500")
    assert len(rows) == 100
    flagged = {r["id"] for r in rows if r.get("offline")}
    assert flagged == set(lib.ids_b)
    assert {r["offline"] for r in rows if r.get("offline")} == {"drive"}
    assert (await _get("/api/tracks/count"))["visible"] == 100
    hits = await _get("/api/search?q=zork&limit=200")
    assert {r["id"] for r in hits if r.get("offline")} == set(lib.ids_b)
    quick = await _get("/api/search/quick?q=song&limit=20")
    assert all(("offline" in r) == (r["id"] in set(lib.ids_b)) for r in quick)
    flt = await _get("/api/search/filter?genre=Genre%201&limit=2000")
    assert any(r.get("offline") for r in flt) and len(flt) == sum(
        1 for t in lib.store._tracks.values() if t["genre"] == ["Genre 1"])
    one = await _get(f"/api/tracks/{lib.ids_b[0]}")
    assert one["offline"] == "drive"
    batch = await _post("/api/tracks/meta/batch", {"ids": lib.ids_b[:3] + lib.ids_a[:2]})
    assert [bool(r.get("offline")) for r in batch] == [True, True, True, False, False]


@pytest.mark.asyncio
async def test_show_lists_everything_as_before(lib):
    lib.offline()
    lib.mode("show")
    rows = await _get("/api/tracks?limit=500")
    assert len(rows) == 100 and not any("offline" in r for r in rows)
    assert not any("offline" in r for r in await _get("/api/search?q=zork&limit=200"))
    assert (await _get("/api/tracks/count"))["visible"] == 100
    shuf = await _get("/api/tracks/shuffled?seed=7&limit=500")
    assert shuf["total"] == 100


@pytest.mark.asyncio
async def test_hide_leaves_them_out_with_exact_totals_and_paging(lib):
    lib.offline()
    lib.mode("hide")
    cnt = await _get("/api/tracks/count")
    assert cnt == {"count": 100, "visible": 60}
    for sort in ("", "&sort=title&order=asc", "&sort=year&order=desc"):
        got = []
        for off in range(0, 80, 7):                           # odd page size: no holes, no repeats
            got += [r["id"] for r in await _get(f"/api/tracks?limit=7&offset={off}{sort}")]
        assert len(got) == 60 and set(got) == set(lib.ids_a), sort
        assert not any(r.get("offline") for r in await _get(f"/api/tracks?limit=500{sort}"))
    hits = await _get("/api/search?q=zork&limit=200")
    assert {r["id"] for r in hits} == set(lib.ids_a)
    assert not {r["id"] for r in await _get("/api/search/quick?q=song&limit=20")} & set(lib.ids_b)
    flt = await _get("/api/search/filter?format=SID&limit=2000")
    assert flt and not {r["id"] for r in flt} & set(lib.ids_b)
    # The rows of a hidden track still come back when asked for by id (queue / history).
    batch = await _post("/api/tracks/meta/batch", {"ids": lib.ids_b[:2]})
    assert len(batch) == 2 and all(r["offline"] == "drive" for r in batch)


@pytest.mark.asyncio
async def test_hide_deep_pages_slice_the_memoised_order(lib, monkeypatch):
    # A deep page (offset ≥ _PRIMARY_ORDER_MIN_OFFSET) slices an order built once.
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 10)
    lib.offline()
    lib.mode("hide")
    for dedup in (False, True):
        lib.store.set_config("filter_duplicates", dedup)
        for sort in ("", "&sort=title", "&sort=duration&order=desc"):
            got = []
            for off in range(0, 70, 9):
                got += [r["id"] for r in await _get(f"/api/tracks?limit=9&offset={off}{sort}")]
            assert len(got) == 60 and set(got) == set(lib.ids_a), (dedup, sort)
            ref = [r["id"] for r in lib.store.filter_tracks(
                limit=100, offset=0, sort_by=sort.split("=")[1].split("&")[0] if sort else None,
                sort_order="desc" if "desc" in sort else None, filter_duplicates=dedup)
                if r["id"] not in set(lib.ids_b)]
            assert got == ref, (dedup, sort)


@pytest.mark.asyncio
async def test_hide_group_counts_match_their_drill_downs(lib):
    lib.offline()
    lib.mode("hide")
    store = lib.store
    hidden = set(lib.ids_b)

    def shown(pred):
        return sum(1 for tid, t in store._tracks.items() if tid not in hidden and pred(t))
    artists = await library.list_artists(_req())
    import json as _json
    arts = _json.loads(artists.body)
    for a in arts:
        if a.get("label"):
            continue
        assert a["count"] == shown(lambda t, n=a["artist"]: t["artist"] == n), a
    genres = _json.loads((await library.list_genres(_req())).body)
    for g in genres:
        assert g["count"] == shown(lambda t, n=g["genre"]: n in t["genre"]), g
    years = _json.loads((await library.list_years(_req())).body)
    for y in years:
        assert y["count"] == shown(lambda t, n=y["year"]: t["year"] == n), y
    fmts = _json.loads((await library.list_formats(_req())).body)
    for f in fmts:
        n = len(await _get(f"/api/tracks?format={f['format']}&limit=500"))
        assert f["count"] == n == shown(lambda t, x=f["format"]: t["format"] == x), f
    albums = _json.loads((await library.list_albums(_req())).body)
    for al in albums:
        assert al["count"] == shown(lambda t, n=al["album"]: t["album"] == n), al
        assert al["track_id"] not in hidden                     # its cover comes from a listed track
    # Back to Mark: the counts include them again (cache keys follow the mode).
    lib.mode("mark")
    arts2 = _json.loads((await library.list_artists(_req())).body)
    assert sum(a["count"] for a in arts2) == 100


@pytest.mark.asyncio
async def test_a_root_coming_back_refreshes_hide_counts_and_lists(lib):
    lib.offline()
    lib.mode("hide")
    assert (await _get("/api/tracks/count"))["visible"] == 60
    import json as _json
    assert sum(f["count"] for f in _json.loads((await library.list_formats(_req())).body)) == 60
    lib.online()
    assert (await _get("/api/tracks/count"))["visible"] == 100
    assert sum(f["count"] for f in _json.loads((await library.list_formats(_req())).body)) == 100
    assert len(await _get("/api/tracks?limit=500")) == 100


_SEEDS = itertools.count(1000)
_UNTAGGED_BUCKETS = {"artist": (library.list_artists, "[No Artist]"),
                     "album_artist": (library.list_album_artists, "[No Album Artist]")}


def _untag_some(lib) -> dict[str, set[str]]:
    """Give the library multi-artist / multi-album-artist tags, blank tags of
    every untagged field and non-primary duplicate copies, spread over both
    roots (B goes offline).  Returns the ids whose tag is blank, per field —
    built from the edits, not from the store's own blank test."""
    blank = {f: set() for f in ("artist", "album_artist", "album", "genre")}
    for k, tid in enumerate(lib.ids_a + lib.ids_b):
        t = copy.deepcopy(lib.store.get_track(tid))
        if k % 6 in (0, 1):
            t["artist"] = "" if k % 6 == 0 else "   "
            blank["artist"].add(tid)
        elif k % 6 in (2, 3):                      # in two artist buckets
            t["artist"] = f"Artist {k % 7} • Guest {k % 3}"
        t["album_artist"] = ("", f"AA 1 • AA {k % 3 + 2}",
                             f"AA 5 • AA 1 • AA 2", "AA 4")[k % 4]
        if k % 4 == 0:
            blank["album_artist"].add(tid)
        if k % 7 in (0, 1):
            t["album"] = "" if k % 7 == 0 else "  "
            blank["album"].add(tid)
        if k % 8 in (0, 1):
            t["genre"] = [] if k % 8 == 0 else [" "]
            blank["genre"].add(tid)
        elif k % 8 == 2:                           # listed under a derived genre
            t["genre"], t["genre_derived"] = [], ["Chiptune"]
        elif k % 8 == 3:
            t["genre"] = ["", "Rock"]
        if k % 5 == 1:                             # a duplicate copy
            t["is_duplicate_primary"], t["duplicate_group_id"] = False, f"g{k}"
        lib.store.upsert_track(t)
    return blank


async def _untagged_rows(field: str) -> list[str]:
    """The ids the "[No …]" drill-down lists, page by page (odd page size)."""
    got: list[str] = []
    for off in range(0, 200, 9):
        page = await _get(f"/api/tracks?untagged={field}&limit=9&offset={off}")
        got += [r["id"] for r in page]
        if len(page) < 9:
            break
    return got


async def _assert_untagged_counts_match(lib, blank, dedup: bool, mode: str) -> None:
    store = lib.store
    hidden = set(lib.ids_b) if mode == "hide" else set()
    primary = {tid for tid, t in store._tracks.items() if t.get("is_duplicate_primary", True)}
    visible_total = (await _get("/api/tracks/count"))["visible"]
    for field in ("artist", "album_artist", "album", "genre"):
        want = {tid for tid in blank[field]
                if tid not in hidden and (not dedup or tid in primary)}
        where = (field, dedup, mode)
        rows = await _untagged_rows(field)
        assert len(rows) == len(set(rows)) and set(rows) == want, where   # no holes, no repeats
        one = await _get(f"/api/tracks?untagged={field}&limit=5000")   # the library.js fetch
        assert len(one) == len(want), where
        n = store.untagged_count(field, primary_only=dedup, exclude=availability.hidden(store))
        assert n == len(rows), where
        # The play queue's list-order pages (Mark / Hide: without offline tracks).
        offline = set(lib.ids_b) if mode != "show" else set()
        play = await _get(f"/api/tracks?untagged={field}&playable=1&limit=500")
        assert {r["id"] for r in play["tracks"]} == want - offline and play["ended"], where
        if mode != "mark":                         # (Mark: shuffles skip offline tracks)
            # A new seed each time: a seed's order is kept for its session.
            seed = next(_SEEDS)
            shuf = await _get(f"/api/tracks/shuffled?seed={seed}&limit=500&untagged={field}")
            assert shuf["total"] == len(rows), where
        if field in _UNTAGGED_BUCKETS:
            endpoint, label = _UNTAGGED_BUCKETS[field]
            groups = json.loads((await endpoint(_req())).body)
            buckets = [g for g in groups if g.get("label") == label]
            assert [g["count"] for g in buckets] == ([len(rows)] if rows else []), where
            # The fixture is one where "visible total minus the tagged buckets"
            # (the old formula) is wrong: multi-name tags count twice.
            tagged = sum(g["count"] for g in groups if not g.get("label"))
            assert visible_total - tagged != len(rows), where


@pytest.mark.asyncio
@pytest.mark.parametrize("walk", [False, True])
@pytest.mark.parametrize("mode", ["show", "mark", "hide"])
@pytest.mark.parametrize("dedup", [False, True])
async def test_untagged_bucket_counts_equal_their_drill_down_rows(lib, dedup, mode, walk,
                                                                  monkeypatch):
    """A "[No Artist]" / "[No Album Artist]" bucket counts exactly the rows its
    drill-down (``/api/tracks?untagged=…``) lists — with multi-artist tags, with
    duplicates hidden or not, with tracks on an offline drive hidden or not —
    and the same holds for the album and genre drill-downs.  ``walk``: pages
    walk the sort index (a big set) instead of sorting the candidates."""
    if walk:
        monkeypatch.setattr(store_mod, "_SMALL_CANDIDATE_SET", 0)
    blank = _untag_some(lib)
    # The fixture has, per field, blank-tag tracks that are primary + online,
    # duplicate copies, and on the drive that goes offline.
    primary = {tid for tid, t in lib.store._tracks.items() if t.get("is_duplicate_primary", True)}
    for field, ids in blank.items():
        assert ids & primary & set(lib.ids_a) and ids - primary and ids & set(lib.ids_b), field
    lib.offline()
    lib.mode(mode)
    lib.store.set_config("filter_duplicates", dedup)
    library.invalidate_agg_cache()                 # (what the settings API does on a toggle)
    await _assert_untagged_counts_match(lib, blank, dedup, mode)
    # Warm, then a different input: a tag edit, and the drive coming back.
    await _assert_untagged_counts_match(lib, blank, dedup, mode)
    tid = next(t for t in lib.ids_a if t not in blank["artist"] and t in primary)
    lib.store.update_track_fields(tid, {"artist": ""})
    blank["artist"].add(tid)
    await _assert_untagged_counts_match(lib, blank, dedup, mode)
    lib.online()
    await _assert_untagged_counts_match(lib, blank, dedup, "show")


def test_untagged_counts_follow_the_mode_between_writes(lib):
    # No track write between the calls: the memo must still keep the modes apart.
    blank = _untag_some(lib)
    primary = {tid for tid, t in lib.store._tracks.items() if t.get("is_duplicate_primary", True)}
    hidden = frozenset(lib.ids_b)
    for field, ids in blank.items():
        for _ in range(2):                         # cold, then warm
            assert lib.store.untagged_count(field) == len(ids), field
            assert lib.store.untagged_count(field, primary_only=True) == len(ids & primary), field
            assert lib.store.untagged_count(field, exclude=hidden) == len(ids - hidden), field
            assert lib.store.untagged_count(field, primary_only=True, exclude=hidden) == len(
                (ids & primary) - hidden), field


@pytest.mark.asyncio
async def test_no_untagged_bucket_when_every_track_is_tagged(lib):
    for k, tid in enumerate(lib.ids_a + lib.ids_b):
        lib.store.update_track_fields(tid, {"artist": f"Artist {k % 4} \u2022 Guest {k % 3}",
                                            "album_artist": f"AA {k % 2} \u2022 AA 9"})
    for field, (endpoint, label) in _UNTAGGED_BUCKETS.items():
        groups = json.loads((await endpoint(_req())).body)
        assert groups and not [g for g in groups if g.get("label")], field
        assert await _get(f"/api/tracks?untagged={field}&limit=500") == [], field
        assert lib.store.untagged_count(field) == 0, field


def test_untagged_count_rejects_an_unknown_field(lib):
    with pytest.raises(ValueError):
        lib.store.untagged_count("title")


@pytest.mark.asyncio
async def test_the_untagged_list_rejects_an_unknown_field(lib):
    async with _client() as c:
        r = await c.get("/api/tracks?untagged=title")
    assert r.status_code == 422


# ── auto-play: shuffles, radio, similar, playable pages ─────────────────────

@pytest.mark.asyncio
async def test_shuffles_never_deal_offline_tracks_in_mark_or_hide(lib):
    lib.offline()
    for seed, m in ((11, "mark"), (12, "hide")):
        lib.mode(m)
        out = await _get(f"/api/tracks/shuffled?seed={seed}&limit=500")
        assert out["total"] == 60, m
        assert {t["id"] for t in out["tracks"]} == set(lib.ids_a), m
    lib.mode("show")
    assert (await _get("/api/tracks/shuffled?seed=13&limit=500"))["total"] == 100
    # A session's order is never re-dealt under it: a drive that comes back
    # joins the next shuffle (a new seed) …
    lib.mode("mark")
    first = await _get("/api/tracks/shuffled?seed=14&limit=500")
    lib.online()
    again = await _get("/api/tracks/shuffled?seed=14&limit=500")
    assert again["tracks"] == first["tracks"] and again["total"] == 60
    assert (await _get("/api/tracks/shuffled?seed=15&limit=500"))["total"] == 100
    # … and one that goes away mid-session has its tracks skipped page by page,
    # every other track keeping its position.
    order = [t["id"] for t in (await _get("/api/tracks/shuffled?seed=15&limit=500"))["tracks"]]
    lib.offline(lib.root_a)
    page = await _get("/api/tracks/shuffled?seed=15&limit=10&offset=10")
    assert page["total"] == 100
    assert [t["id"] for t in page["tracks"]] == [i for i in order[10:20] if i not in set(lib.ids_a)]


@pytest.mark.asyncio
async def test_playable_pages_skip_offline_rows_with_exact_positions(lib):
    lib.offline()
    lib.mode("mark")
    full = [r["id"] for r in await _get("/api/tracks?limit=500&sort=title")]
    off = set(lib.ids_b)
    got, pos, nxt, ended = [], [], 0, False
    while not ended:
        page = await _get(f"/api/tracks?limit=8&offset={nxt}&sort=title&playable=1")
        got += [r["id"] for r in page["tracks"]]
        pos += [r["_pos"] for r in page["tracks"]]
        assert page["next_offset"] >= nxt
        nxt, ended = page["next_offset"], page["ended"]
    assert got == [t for t in full if t not in off]
    assert [full[p] for p in pos] == got                       # _pos = the row's list position
    # /search/filter speaks the same, positions included
    drill = [r["id"] for r in await _get("/api/search/filter?genre=Genre%200&limit=500")]
    got, nxt, ended = [], 0, False
    while not ended:
        page = await _get(f"/api/search/filter?genre=Genre%200&limit=5&offset={nxt}&playable=1")
        assert set(page) == {"tracks", "next_offset", "ended"}
        got += [(r["_pos"], r["id"]) for r in page["tracks"]]
        nxt, ended = page["next_offset"], page["ended"]
    assert [i for _p, i in got] == [t for t in drill if t not in off]
    assert all(drill[p] == i for p, i in got)
    # Show mode: the plain page, in the same shape (positions too)
    lib.mode("show")
    page = await _get("/api/tracks?limit=500&offset=0&playable=1")
    assert len(page["tracks"]) == 100 and page["ended"] is True
    assert [r["_pos"] for r in page["tracks"]] == list(range(100))


@pytest.mark.asyncio
async def test_a_playable_page_can_start_after_a_given_track(lib):
    """Hide mode's list changed length under a queue: its next page starts
    right after the last track it holds (``after``), wherever that is now."""
    lib.mode("hide")
    lib.offline()
    full_hidden = [r["id"] for r in await _get("/api/tracks?limit=500&sort=title")]
    anchor = full_hidden[10]
    lib.online()                                          # the list grows
    full = [r["id"] for r in await _get("/api/tracks?limit=500&sort=title")]
    page = await _get(f"/api/tracks?limit=5&offset=10&sort=title&playable=1&after={anchor}")
    at = full.index(anchor)
    assert [r["id"] for r in page["tracks"]] == full[at + 1:at + 6]
    assert [r["_pos"] for r in page["tracks"]] == list(range(at + 1, at + 6))
    # an anchor the list doesn't hold: the offset is used
    page = await _get("/api/tracks?limit=3&offset=4&sort=title&playable=1&after=nope")
    assert [r["_pos"] for r in page["tracks"]] == [4, 5, 6]
    drill = [r["id"] for r in await _get("/api/search/filter?genre=Genre%201&limit=500")]
    page = await _get(f"/api/search/filter?genre=Genre%201&limit=2&playable=1&after={drill[3]}")
    assert [r["id"] for r in page["tracks"]] == drill[4:6]


def test_a_playable_page_reads_its_list_at_most_twice(lib):
    """A filtered list re-walks its index on every read: one long offline run
    costs two reads, not one per few pages."""
    lib.offline()
    lib.mode("mark")
    calls = []
    rows = [{"id": i, "path": f"{lib.root_b}/x{i}.mod", "scan_root_hash": ph(lib.root_b)}
            for i in range(3000)] + [{"id": "ok", "path": f"{lib.root_a}/y.mod",
                                      "scan_root_hash": ph(lib.root_a)}]

    def fetch(off, lim):
        calls.append((off, lim))
        return rows[off:off + lim]
    page = availability.player_page(fetch, 0, 20, lib.store)
    assert [r["id"] for r in page["tracks"]] == ["ok"] and page["tracks"][0]["_pos"] == 3000
    assert len(calls) == 2 and page["ended"] is True


@pytest.mark.asyncio
async def test_playable_page_crosses_a_long_offline_run_within_its_budget(lib, monkeypatch):
    monkeypatch.setattr(availability, "PLAYABLE_SCAN_BUDGET", 10)
    lib.offline()
    lib.mode("mark")
    # oldest first: root A was added first → B's 40 rows end the list; newest first → they lead
    page = await _get("/api/tracks?limit=5&offset=0&playable=1")    # newest first: B's rows first
    first_b = availability.snapshot().kind_of
    assert page["ended"] is False
    assert page["tracks"] == [] or all(not first_b(r) for r in page["tracks"])
    nxt, hops = page["next_offset"], 0
    while not page["tracks"]:
        hops += 1
        page = await _get(f"/api/tracks?limit=5&offset={nxt}&playable=1")
        nxt = page["next_offset"]
    assert hops >= 1 and page["tracks"][0]["id"] in set(lib.ids_a)


@pytest.mark.asyncio
async def test_radio_and_similar_never_suggest_an_offline_track(lib, monkeypatch):
    lib.offline()
    lib.mode("mark")
    seed = lib.ids_a[0]
    mix = await _get(f"/api/smart/radio?seed={seed}&limit=60")
    assert mix[0]["id"] == seed
    assert not {t["id"] for t in mix[1:]} & set(lib.ids_b)
    sim = await _get(f"/api/search/similar/{seed}?k=50")
    assert sim and not {r["track"]["id"] for r in sim} & set(lib.ids_b)
    # An offline seed is the listener's pick: kept, flagged.
    mix2 = await _get(f"/api/smart/radio?seed={lib.ids_b[0]}&limit=30")
    assert mix2[0]["id"] == lib.ids_b[0] and mix2[0]["offline"] == "drive"
    assert not {t["id"] for t in mix2[1:]} & set(lib.ids_b)
    lib.mode("show")
    mix3 = await _get(f"/api/smart/radio?seed={seed}&limit=60")
    assert {t["id"] for t in mix3} & set(lib.ids_b)            # Show: as before


@pytest.mark.asyncio
async def test_smart_lists_hide_but_still_fill_their_limit(lib):
    for i, t in enumerate(lib.ids_b + lib.ids_a):
        lib.store.record_play(t)
        lib.store.set_rating(t, 5 if i < 50 else 3)
    lib.offline()
    lib.mode("hide")
    for view in ("recently-added", "most-played", "top-rated"):
        rows = await _get(f"/api/smart/{view}?limit=20")
        assert len(rows) == 20 and not {r["id"] for r in rows} & set(lib.ids_b), view
    lib.mode("mark")
    rows = await _get("/api/smart/recently-added?limit=100")
    assert len(rows) == 100 and {r["id"] for r in rows if r.get("offline")} == set(lib.ids_b)


# ── playlists and folders never hide ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_playlists_mark_their_offline_entries_and_never_drop_one(lib, monkeypatch):
    from soniqboom.api import users as users_api
    entries = lib.ids_b[:3] + lib.ids_a[:2]
    lib.store.create_playlist("pl1", "Mixed", track_ids=entries)
    lib.store.create_playlist("pl2", "Smart", query="zork")
    lib.offline()
    app = _app()
    app.dependency_overrides[users_api.current_user] = lambda: None
    monkeypatch.setattr(playlist_api, "_can_read", lambda pl, u: True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for m in ("hide", "mark"):
            lib.mode(m)
            pl = (await c.get("/api/playlists/pl1")).json()
            assert [t["id"] for t in pl["tracks"]] == entries, m
            assert [bool(t.get("offline")) for t in pl["tracks"]] == [True, True, True, False, False]
            sp = (await c.get("/api/playlists/pl2")).json()
            assert len(sp["tracks"]) == 100, m                 # a smart playlist hides nothing either
            assert sum(1 for t in sp["tracks"] if t.get("offline")) == 40
        lib.mode("show")
        pl = (await c.get("/api/playlists/pl1")).json()
        assert not any("offline" in t for t in pl["tracks"])


@pytest.mark.asyncio
async def test_folder_listing_marks_in_hide_mode_and_its_shuffle_skips(lib, monkeypatch):
    lib.offline()
    lib.mode("hide")
    d = f"{lib.root_b}/d0"
    rows = await _get(f"/api/fstree/tracks-with-meta?path={d}&recursive=false")
    assert len(rows) == 10 and all(r.get("offline") == "drive" for r in rows)
    page = await _get(f"/api/fstree/tracks-with-meta?path={lib.root_b}&recursive=true&limit=500")
    assert page["total"] == 40 and all(r.get("offline") for r in page["tracks"])
    shuf = await _get(f"/api/fstree/tracks-with-meta?path={lib.root_b}&recursive=true"
                      f"&limit=50&shuffle_seed=5")
    # (dealt over the whole folder — never re-dealt mid-session; its offline
    # tracks are left out page by page)
    assert shuf["total"] == 40 and shuf["tracks"] == []
    play = await _get(f"/api/fstree/tracks-with-meta?path={lib.root_b}&recursive=true"
                      f"&limit=50&playable=1")
    assert play["tracks"] == [] and play["ended"] is True
    # the cached listing itself was never written into
    again = await _get(f"/api/fstree/tracks-with-meta?path={d}&recursive=false")
    assert all(r.get("offline") for r in again)
    lib.online()
    back = await _get(f"/api/fstree/tracks-with-meta?path={d}&recursive=false")
    assert not any("offline" in r for r in back)


@pytest.mark.asyncio
async def test_a_folder_shuffle_is_never_re_dealt_when_a_drive_goes_or_comes(lib):
    """A folder's shuffle order is dealt over the whole folder: while its
    drive is offline the pages leave its tracks out (same total), and when
    it is back the order is the one dealt before — nothing moved."""
    lib.mode("mark")
    url = f"/api/fstree/tracks-with-meta?path={lib.root_b}&recursive=true&limit=500&shuffle_seed=9"
    first = await _get(url)
    assert first["total"] == 40 and len(first["tracks"]) == 40
    lib.offline()
    gap = await _get(url)
    assert gap["total"] == 40 and gap["tracks"] == []
    # the queue's page (playable=1) reads on through the order by itself
    play = await _get(url.replace("limit=500", "limit=10") + "&playable=1")
    assert play["tracks"] == [] and play["ended"] is True and play["next_offset"] == 40
    lib.online()
    again = await _get(url)
    assert [t["id"] for t in again["tracks"]] == [t["id"] for t in first["tracks"]]
    play = await _get(url.replace("limit=500", "limit=10") + "&playable=1")
    assert [t["id"] for t in play["tracks"]] == [t["id"] for t in first["tracks"][:10]]


# ── the state endpoints, the setting, the notification ───────────────────────

@pytest.mark.asyncio
async def test_availability_endpoints(lib):
    st = await _get("/api/library/availability")
    assert st["mode"] == "mark" and st["unavailable"] == 0 and st["roots"] == []
    lib.offline()
    st2 = await _get("/api/library/availability")
    assert st2["token"] != st["token"] and st2["unavailable"] == 40
    assert st2["roots"] == [{"path": lib.root_b, "path_hash": ph(lib.root_b), "kind": "drive",
                             "label": None, "tracks": 40, "unavailable": 40}]
    out = await _post("/api/tracks/meta/availability",
                      {"ids": lib.ids_a[:2] + lib.ids_b[:2] + ["nope", 7]})
    assert out["offline"] == {lib.ids_b[0]: "drive", lib.ids_b[1]: "drive"}
    # one token everywhere, naming this server process (a client holding one
    # from before a restart never mistakes the new first generation for it)
    assert out["token"] == st2["token"] == availability.token()
    assert st2["token"].split(":")[1] == availability._BOOT
    lib.mode("hide")
    assert (await _get("/api/library/availability"))["token"].startswith("hide:")


@pytest.mark.asyncio
async def test_the_setting_through_the_admin_api_and_ui_config(lib, monkeypatch):
    from soniqboom.api import admin
    from soniqboom import main
    calls = []
    monkeypatch.setattr(availability, "notify_changed", lambda: calls.append(1))
    got = await admin.get_settings(_tok="t")
    assert got["offline_tracks"] == "mark"
    await admin.update_settings({"offline_tracks": "hide"}, _tok="t")
    assert lib.store.get_config(availability.MODE_KEY) == "hide" and calls == [1]
    await admin.update_settings({"offline_tracks": "bogus"}, _tok="t")     # ignored
    assert availability.mode() == "hide" and calls == [1]
    await admin.update_settings({"offline_tracks": "hide"}, _tok="t")      # no change, no event
    assert calls == [1]
    assert (await main.ui_config())["offline_tracks"] == "hide"
    await admin.update_settings({"offline_tracks": "SHOW"}, _tok="t")
    assert availability.mode() == "show" and calls == [1, 1]


@pytest.mark.asyncio
async def test_a_status_flip_is_broadcast_once_things_settle(lib, monkeypatch):
    sent = []

    async def fake_broadcast(data):
        sent.append(data)
    monkeypatch.setattr(library, "_broadcast", fake_broadcast)
    monkeypatch.setattr(availability, "_NOTIFY_DELAY_S", 0.01)
    monkeypatch.setattr(availability, "WATCH_INTERVAL_S", 3600)
    availability.install(lib.store)
    try:
        lib.offline()
        lib.online()
        lib.offline()                                          # a burst: one event, after it settled
        await asyncio.sleep(0.15)
        assert len(sent) == 1 and sent[0]["event"] == "availability_changed"
        assert sent[0]["token"] == availability.token()
        lib.store.set_scan_dir_status(lib.root_b, "unavailable")   # no change → nothing
        await asyncio.sleep(0.05)
        assert len(sent) == 1
        lib.online()
        await asyncio.sleep(0.15)
        assert len(sent) == 2
    finally:
        availability.uninstall()
        lib.store._on_scan_dirs_changed = None


@pytest.mark.asyncio
async def test_the_local_probe_marks_a_renamed_root_and_never_upgrades_a_share(lib, tmp_path):
    from soniqboom.core import data
    Path(lib.root_b).rename(tmp_path / "B-gone")
    assert await data.refresh_local_root_availability() == 1
    assert lib.store._scan_dirs[lib.root_b]["status"] == "unavailable"
    assert lib.ids_b[0] in availability.snapshot().unavailable
    (tmp_path / "B-gone").rename(lib.root_b)
    assert await data.refresh_local_root_availability() == 1
    assert lib.store._scan_dirs[lib.root_b]["status"] == "ok"
    # A share the health monitor marked down keeps that mark through a folder-tree probe.
    from soniqboom.core import filesource
    root = "ftp://nas3.invalid/x"
    lib.store.upsert_scan_dir(root, network_share_id="n3", status="unavailable")
    filesource._active_sources[root] = object()                # its source is still registered
    try:
        await data.refresh_scan_dir_availability(force=True)
        assert lib.store._scan_dirs[root]["status"] == "unavailable"
        del filesource._active_sources[root]                       # …and with no source: marked
        lib.store.set_scan_dir_status(root, "ok")
        await data.refresh_scan_dir_availability(force=True)
        assert lib.store._scan_dirs[root]["status"] == "unavailable"
    finally:
        filesource._active_sources.pop(root, None)


@pytest.mark.asyncio
async def test_a_failed_play_on_a_vanished_drive_marks_it(lib, tmp_path):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    real = Path(lib.root_b) / "d0" / "t0.mod"
    real.write_bytes(b"M.K.")
    Path(lib.root_b).rename(tmp_path / "B-ejected")
    track = await get_track(lib.ids_b[0])
    with pytest.raises(HTTPException) as ei:
        await stream._resolve_play_source(lib.ids_b[0], track)
    assert ei.value.status_code == 503 and "Source offline" in ei.value.detail
    assert lib.store._scan_dirs[lib.root_b]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_a_hung_mount_holds_one_probe_worker_and_never_flips_the_others(lib, tmp_path,
                                                                            monkeypatch):
    """A probe stuck on a hung mount is waited on, never started again, so the
    healthy roots keep being probed; the hung root counts as gone only once
    its probe has been stuck for two probe timeouts."""
    import os
    import threading
    from soniqboom.core import data
    hung = tmp_path / "HUNG"
    (hung / "x").mkdir(parents=True)
    lib.store.upsert_scan_dir(str(hung))
    lib.store.upsert_track(_row(f"{hung}/x/h.mod", str(hung), 900))
    release = threading.Event()
    stuck = []
    real = os.scandir

    def scandir(path, *a, **k):
        if str(path) == str(hung):
            stuck.append(threading.get_ident())
            release.wait(30)                      # a stat that never returns
        return real(path, *a, **k)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(data, "_AVAIL_PROBE_TIMEOUT", 0.2)
    monkeypatch.setattr(data, "_AVAIL_STALLED_S", 0.5)
    monkeypatch.setattr(data, "_local_probes", {})

    def status():
        return {Path(sd["path"]).name: sd["status"] for sd in lib.store.list_scan_dirs()}
    try:
        await data.refresh_local_root_availability()
        assert status() == {"A": "ok", "B": "ok", "HUNG": "ok"}     # one slow answer: as is
        for _ in range(8):                        # (more cycles than the pool has workers)
            await asyncio.sleep(0.1)
            await data.refresh_local_root_availability()
            assert status()["A"] == "ok" and status()["B"] == "ok"
        assert status()["HUNG"] == "unavailable"
        assert len(stuck) == 1                    # one worker held, not one per cycle
        assert len(availability.snapshot().unavailable) == 1
    finally:
        release.set()
    await asyncio.sleep(0.05)
    await data.refresh_local_root_availability()
    assert status()["HUNG"] == "ok"               # answered again: back at once


@pytest.mark.asyncio
async def test_many_hung_mounts_never_delay_another_roots_probe(lib, tmp_path, monkeypatch):
    """Ten mounts hang: a healthy drive unplugged meanwhile is still marked
    at once (each probe has its own thread)."""
    import os
    import threading
    from soniqboom.core import data
    release = threading.Event()
    hung = []
    for k in range(10):
        h = tmp_path / f"HUNG{k}"
        (h / "x").mkdir(parents=True)
        lib.store.upsert_scan_dir(str(h))
        hung.append(str(h))
    real = os.scandir

    def scandir(path, *a, **k):
        if str(path) in hung:
            release.wait(30)
        return real(path, *a, **k)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(data, "_AVAIL_PROBE_TIMEOUT", 0.2)
    monkeypatch.setattr(data, "_AVAIL_STALLED_S", 0.5)
    monkeypatch.setattr(data, "_local_probes", {})
    try:
        await data.refresh_local_root_availability()
        Path(lib.root_b).rename(tmp_path / "B-unplugged")
        t0 = time.monotonic()
        await data.refresh_local_root_availability()
        assert time.monotonic() - t0 < 1.0                # (no wait for a free worker)
        assert lib.store._scan_dirs[lib.root_b]["status"] == "unavailable"
        assert lib.store._scan_dirs[lib.root_a]["status"] == "ok"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_an_empty_mount_point_counts_as_gone_while_it_has_tracks(lib, tmp_path):
    """An unplugged drive's mount point can stay behind as an empty folder."""
    from soniqboom.core import data
    for p in Path(lib.root_b).iterdir():
        p.rmdir()
    empty_new = tmp_path / "NEW"
    empty_new.mkdir()
    lib.store.upsert_scan_dir(str(empty_new))                  # a new root, nothing indexed
    await data.refresh_local_root_availability()
    assert lib.store._scan_dirs[lib.root_b]["status"] == "unavailable"
    assert lib.store._scan_dirs[str(empty_new)]["status"] == "ok"
    assert lib.store._scan_dirs[lib.root_a]["status"] == "ok"
    (Path(lib.root_b) / "d0").mkdir()                          # the drive is back
    await data.refresh_local_root_availability()
    assert lib.store._scan_dirs[lib.root_b]["status"] == "ok"


@pytest.mark.asyncio
async def test_a_play_on_a_drive_known_offline_never_touches_the_file(lib, tmp_path, monkeypatch):
    """A root already marked unavailable: the play is answered "offline" at
    once (a probe on its own thread decides, never a stat on the event loop);
    a root that answers again is marked available and the play goes on."""
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    real = Path(lib.root_b) / "d0" / "t0.mod"
    real.write_bytes(b"M.K.")
    Path(lib.root_b).rename(tmp_path / "B-gone")
    lib.offline()
    touched = []
    real_exists = Path.exists

    def exists(self, *a, **k):
        if str(self).startswith(lib.root_b):
            touched.append(str(self))
        return real_exists(self, *a, **k)
    monkeypatch.setattr(Path, "exists", exists)
    track = await get_track(lib.ids_b[0])
    with pytest.raises(HTTPException) as ei:
        await stream._resolve_play_source(lib.ids_b[0], track)
    assert ei.value.status_code == 503 and touched == []
    (tmp_path / "B-gone").rename(lib.root_b)              # plugged in again
    try:
        await stream._resolve_play_source(lib.ids_b[0], track)
    except HTTPException as exc:
        assert exc.status_code != 503, exc.detail
    except Exception:
        pass                                              # (later steps need a renderer)
    assert lib.store._scan_dirs[lib.root_b]["status"] == "ok"


def test_the_health_monitor_helper_ignores_catalogue_roots(lib):
    from soniqboom import main
    from soniqboom.core import catalogue
    root = "ftp://cat2.invalid/pub"
    lib.store.upsert_scan_dir(root, status="unavailable")
    assert main._stored_unavailable(root) is True
    catalogue.register_root(root, key="c2", label="C2")
    try:
        assert main._stored_unavailable(root) is False
    finally:
        catalogue.unregister_root(root)
    assert main._stored_unavailable("ftp://unknown.invalid/") is False


# ── cost ─────────────────────────────────────────────────────────────────────

def test_cost_of_a_flip_and_of_hide_pages_on_a_big_store(monkeypatch):
    """60 000 tracks, 25 000 of them on a root that goes offline: the flip and
    the Hide set stay cheap, and a deep Hide page costs about what a Show page
    does (generous bounds; the real numbers are in the report)."""
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    availability.reset_for_tests()
    ra, rb = "/Volumes/BigA", "/Volumes/BigB"
    tracks = {}
    for i in range(60_000):
        root = rb if i < 25_000 else ra
        p = f"{root}/d{i // 40}/t{i}.mod"
        t = _row(p, root, i)
        tracks[t["id"]] = t
    sd = {r: {"path": r, "track_count": 0, "added_at": 0, "last_scanned": 0,
              "network_share_id": None, "status": "ok", "path_hash": ph(r)} for r in (ra, rb)}
    s.bulk_load(tracks, {}, {}, {}, {}, [], sd, {}, {})
    s.rebuild_indexes()
    s.set_config(availability.MODE_KEY, "hide")
    t0 = time.perf_counter()
    s.set_scan_dir_status(rb, "unavailable")
    snap = availability.snapshot(s)
    un = snap.unavailable
    flip_ms = (time.perf_counter() - t0) * 1000
    assert len(un) == 25_000
    assert flip_ms < 250, flip_ms
    excl = availability.hidden(s)
    t0 = time.perf_counter()
    deep = s.filter_tracks(limit=2000, offset=30_000, exclude=excl, sort_by="title")
    first_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    deep2 = s.filter_tracks(limit=2000, offset=32_000, exclude=excl, sort_by="title")
    warm_ms = (time.perf_counter() - t0) * 1000
    assert len(deep) == 2000 and len(deep2) == 2000 and not {r["id"] for r in deep} & un
    assert first_ms < 500 and warm_ms < 60, (first_ms, warm_ms)
    assert s.visible_track_count(exclude=excl) == 35_000
    availability.reset_for_tests()


# ── duplicates: a playable copy stands in for a primary that can't play ─────

# name, on root B (the one that goes offline)?, file, format, bitrate,
# stored primary, group, added_at
_DUP_SPEC = (
    ("p1", True, "dup/g1.flac", "FLAC", 900_000, True, "G1", 5000),
    ("c1", False, "dup/g1.mp3", "MP3", 320_000, False, "G1", 2000),
    ("c1b", False, "dup/g1b.mp3", "MP3", 128_000, False, "G1", 2500),
    ("p2", True, "dup/g2.flac", "FLAC", 900_000, True, "G2", 5100),
    ("c2", True, "dup/g2.mp3", "MP3", 320_000, False, "G2", 2100),
    ("p3", False, "dup/g3.flac", "FLAC", 900_000, True, "G3", 5200),
    ("c3", True, "dup/g3.mp3", "MP3", 320_000, False, "G3", 2200),
)


def _add_dups(lib) -> dict[str, str]:
    """Three duplicate groups across the roots, "Hide duplicates" on — G1: its
    FLAC primary on B, two MP3 copies on A (320 and 128 kbps); G2: every copy
    on B; G3: the primary on A, its copy on B.  A copy differs from its
    primary in everything but what groups them (title, artist, duration): its
    format, year, genre and album artist (the primaries have none — they sit
    in "[No Album Artist]"), so every count can tell which one is listed.
    Returns ``{name: track id}``."""
    ids = {}
    for k, (name, on_b, rel, fmt, br, prim, gid, added) in enumerate(_DUP_SPEC):
        root = lib.root_b if on_b else lib.root_a
        g = int(gid[1])
        row = _row(f"{root}/{rel}", root, 900 + k, title=f"Dup {gid} zork",
                   artist="Dup Artist", album_artist="" if prim else "Copy AA",
                   album=f"Dup Album {gid}",
                   genre=["Dup Genre"] if prim else ["Copy Genre"],
                   year=1985 + g + (0 if prim else 10), duration=200.0 + g, format=fmt,
                   bitrate=br, added_at=added, duplicate_group_id=gid,
                   is_duplicate_primary=prim)
        lib.store.upsert_track(row)
        ids[name] = row["id"]
    lib.store.set_config("filter_duplicates", True)
    return ids


def _dup_expect(lib, d: dict, mode: str) -> tuple[set[str], set[str]]:
    """(what a deduped list shows, what can't play) with B offline — from the
    spec, not from the store's own swap: G1's best playable copy (the 320 kbps
    MP3) stands in for its primary; G2 has no playable copy (its primary stays:
    flagged in Mark, gone in Hide); G3's primary plays."""
    stored = {tid for tid, t in lib.store._tracks.items() if t.get("is_duplicate_primary", True)}
    offline = set(lib.ids_b) | {d[n] for n, on_b, *_rest in _DUP_SPEC if on_b}
    shown = (stored - {d["p1"]}) | {d["c1"]}
    if mode == "hide":
        shown -= offline
    return shown, offline


async def _paged(url: str, n: int = 7, upto: int = 140) -> list[str]:
    got: list[str] = []
    for off in range(0, upto, n):                            # odd page size: no holes, no repeats
        got += [r["id"] for r in await _get(f"{url}&limit={n}&offset={off}")]
    return got


async def _agg(fn, **kw) -> list[dict]:
    return json.loads((await fn(_req(), **kw)).body)


async def _assert_lists(lib, want: set[str], where) -> None:
    """The deduped list, its count and every group count show exactly ``want``
    — each group count equal to its drill-down's rows."""
    store = lib.store
    assert (await _get("/api/tracks/count"))["visible"] == len(want), where
    for sort in ("sort=added", "sort=added&order=asc", "sort=title", "sort=format&order=desc"):
        got = await _paged(f"/api/tracks?{sort}")
        assert len(got) == len(set(got)) == len(want) and set(got) == want, (where, sort)

    def n_shown(pred) -> int:
        return sum(1 for tid in want if pred(store._tracks[tid]))
    for a in await _agg(library.list_artists):
        if a.get("label"):
            assert a["count"] == n_shown(lambda t: not t["artist"].strip()), (where, a)
            continue
        n = len(await _get(f"/api/search/filter?artist={a['artist']}&limit=2000"))
        assert a["count"] == n == n_shown(lambda t, x=a["artist"]: t["artist"] == x), (where, a)
    for a in await _agg(library.list_album_artists):
        if a.get("label"):
            n = len(await _paged("/api/tracks?untagged=album_artist", n=50))
            assert a["count"] == n == n_shown(lambda t: not t["album_artist"].strip()), (where, a)
        else:
            n = len(await _get(f"/api/search/filter?album_artist={a['album_artist']}&limit=2000"))
            assert a["count"] == n == n_shown(
                lambda t, x=a["album_artist"]: t["album_artist"] == x), (where, a)
    for g in await _agg(library.list_genres):
        n = len(await _get(f"/api/search/filter?genre={g['genre']}&limit=2000"))
        assert g["count"] == n == n_shown(lambda t, x=g["genre"]: x in t["genre"]), (where, g)
    for y in await _agg(library.list_years):
        n = len(await _get(f"/api/search/filter?year_min={y['year']}&year_max={y['year']}"
                           f"&limit=2000"))
        assert y["count"] == n == n_shown(lambda t, x=y["year"]: t["year"] == x), (where, y)
    for f in await _agg(library.list_formats):
        n = len(await _get(f"/api/tracks?format={f['format']}&limit=500"))
        assert f["count"] == n == n_shown(lambda t, x=f["format"]: t["format"] == x), (where, f)
    for al in await _agg(library.list_albums):
        assert al["count"] == n_shown(lambda t, x=al["album"]: t["album"] == x), (where, al)
        assert al["track_id"] in want, (where, al)           # its cover comes from a listed track


@pytest.mark.asyncio
@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize("mode", ["mark", "hide"])
async def test_dedup_lists_the_playable_copy_while_the_primary_is_offline(lib, mode, deep,
                                                                         monkeypatch):
    if deep:                                     # every page past the first slices a memoised order
        monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 10)
    d = _add_dups(lib)
    lib.mode(mode)
    lib.offline()
    shown, offline = _dup_expect(lib, d, mode)
    store = lib.store

    # counts == the rows paged (every sort) == the group counts and their drill-downs
    await _assert_lists(lib, shown, "B offline")
    rows = {r["id"]: r for r in await _get("/api/tracks?limit=500")}
    assert d["c1"] in rows and "offline" not in rows[d["c1"]]
    assert not {d["p1"], d["c1b"], d["c2"], d["c3"]} & rows.keys() and d["p3"] in rows
    if mode == "mark":
        assert rows[d["p2"]]["offline"] == "drive"        # no copy can play: the primary stays, flagged
    else:
        assert d["p2"] not in rows
    fmts = {f["format"]: f["count"] for f in await _agg(library.list_formats)}
    assert fmts["FLAC"] == (2 if mode == "mark" else 1)
    scoped = await _agg(library.list_albums, artist="Dup Artist")
    assert {al["album"]: al["count"] for al in scoped}.get("Dup Album G1") == 1
    assert next(al for al in scoped if al["album"] == "Dup Album G1")["track_id"] == d["c1"]

    # search, quick search, drill-downs
    dups = {i for i in shown if store._tracks[i]["artist"] == "Dup Artist"}
    assert {r["id"] for r in await _get("/api/search?q=dup%20zork&limit=200")} == dups
    assert {r["id"] for r in await _get("/api/search/quick?q=dup&limit=20")} == dups
    assert {r["id"] for r in await _get("/api/search/filter?artist=Dup%20Artist&limit=500")} == dups
    assert [r["id"] for r in await _get("/api/search/filter?album=Dup%20Album%20G1")] == [d["c1"]]
    assert {r["id"] for r in await _get("/api/search/filter?scene_group=x&artist=Dup%20Artist")} == set()

    # shuffles deal the playable copy (all, and over a filter)
    shuf = await _get("/api/tracks/shuffled?seed=4242&limit=500")
    assert shuf["total"] == len(shown - offline)
    assert {t["id"] for t in shuf["tracks"]} == shown - offline
    shuf = await _get("/api/tracks/shuffled?seed=4243&artist=Dup%20Artist&limit=50")
    assert {t["id"] for t in shuf["tracks"]} == {d["c1"], d["p3"]}

    # the player's pages (auto-advance) carry it, positions exact
    full = [r["id"] for r in await _get("/api/tracks?limit=500&sort=title")]
    got, pos, nxt, ended = [], [], 0, False
    while not ended:
        page = await _get(f"/api/tracks?limit=8&offset={nxt}&sort=title&playable=1")
        got += [r["id"] for r in page["tracks"]]
        pos += [r["_pos"] for r in page["tracks"]]
        nxt, ended = page["next_offset"], page["ended"]
    assert got == [t for t in full if t not in offline] and d["c1"] in got
    assert [full[p] for p in pos] == got

    # The drive back, away again, then the OTHER drive away instead — no track
    # written in between, so only the availability moves: the lists, counts and
    # memoised orders / group counts follow each time.
    stored = {tid for tid, t in store._tracks.items() if t.get("is_duplicate_primary", True)}
    on_a = set(lib.ids_a) | {d[n] for n, on_b, *_rest in _DUP_SPEC if not on_b}
    a_away = (stored - {d["p3"]}) | {d["c3"]}            # G3's copy on B stands in
    if mode == "hide":
        a_away -= on_a
    lib.online()
    await _assert_lists(lib, stored, "back")
    lib.offline()
    await _assert_lists(lib, shown, "B offline again")
    lib.online()
    lib.offline(lib.root_a)
    await _assert_lists(lib, a_away, "A offline")


async def _dup_state(lib, seed: int) -> dict:
    return {
        "rows": [r["id"] for r in await _get("/api/tracks?limit=500")],
        "count": (await _get("/api/tracks/count"))["visible"],
        "formats": {f["format"]: f["count"] for f in await _agg(library.list_formats)},
        "artists": {a["artist"]: a["count"] for a in await _agg(library.list_artists)},
        "albums": {(a["album"], a["track_id"]) for a in await _agg(library.list_albums)},
        "shuffle": {t["id"] for t in (await _get(f"/api/tracks/shuffled?seed={seed}&limit=500"))["tracks"]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mark", "hide"])
async def test_dedup_the_primary_returns_when_its_drive_is_back(lib, mode):
    d = _add_dups(lib)
    lib.mode(mode)
    before = await _dup_state(lib, 501)
    assert d["p1"] in before["rows"] and d["c1"] not in before["rows"]
    assert d["p1"] in before["shuffle"] and d["c1"] not in before["shuffle"]
    lib.offline()
    during = await _dup_state(lib, 502)
    shown, offline = _dup_expect(lib, d, mode)
    assert set(during["rows"]) == shown and during["count"] == len(shown)
    assert d["c1"] in during["shuffle"] and d["p1"] not in during["shuffle"]
    if mode == "mark":
        # Nothing was written to the store and Mark hides nothing: only the
        # swap moved these counts (the HTTP aggregate cache follows it).
        assert during["formats"]["FLAC"] == before["formats"]["FLAC"] - 1
        assert during["formats"]["MP3"] == before["formats"]["MP3"] + 1
        assert during["artists"] == before["artists"]
        assert ("Dup Album G1", d["c1"]) in during["albums"]
    lib.online()
    after = await _dup_state(lib, 503)
    assert {k: v for k, v in after.items() if k != "shuffle"} == \
        {k: v for k, v in before.items() if k != "shuffle"}
    assert after["shuffle"] == before["shuffle"]
    assert availability.primary_swap(lib.store) is None


@pytest.mark.asyncio
async def test_dedup_a_shuffle_session_swaps_the_copy_into_the_same_slot(lib):
    """A shuffle order is never re-dealt mid-session: the copy plays in its
    primary's slot while the primary's drive is offline, and the primary takes
    the slot back when the drive returns — in either order of events."""
    d = _add_dups(lib)
    lib.mode("mark")
    url = "/api/tracks/shuffled?seed=777&limit=500"
    first = [t["id"] for t in (await _get(url))["tracks"]]      # dealt with every drive connected
    assert d["p1"] in first and d["c1"] not in first
    lib.offline()
    _shown, offline = _dup_expect(lib, d, "mark")
    gap = await _get(url)
    assert gap["total"] == len(first)
    assert [t["id"] for t in gap["tracks"]] == [
        d["c1"] if i == d["p1"] else i for i in first if i not in offline or i == d["p1"]]
    lib.online()
    assert [t["id"] for t in (await _get(url))["tracks"]] == first
    # dealt while the drive is offline: the copy holds the slot until the primary is back
    lib.offline()
    url2 = "/api/tracks/shuffled?seed=778&limit=500"
    dealt = [t["id"] for t in (await _get(url2))["tracks"]]
    assert d["c1"] in dealt and d["p1"] not in dealt
    lib.online()
    assert [t["id"] for t in (await _get(url2))["tracks"]] == [
        d["p1"] if i == d["c1"] else i for i in dealt]
    # A filtered shuffle keeps to its filter: in a FLAC-only shuffle the MP3
    # copy takes no slot (the FLAC is skipped while it can't play).
    url3 = "/api/tracks/shuffled?seed=779&format=FLAC&limit=500"
    flac = [t["id"] for t in (await _get(url3))["tracks"]]
    assert d["p1"] in flac
    lib.offline()
    gap = [t["id"] for t in (await _get(url3))["tracks"]]
    assert d["c1"] not in gap and gap == [i for i in flac if i not in offline]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mark", "hide"])
async def test_dedup_a_group_with_two_stale_primaries_counts_what_it_lists(lib, mode):
    """Stale flags — two primaries in one group, both offline — get one
    stand-in between them: the count drops by one, like the list."""
    s = lib.store
    ids = []
    for k, (root, rel, fmt, prim) in enumerate((
            (lib.root_b, "g4a.flac", "FLAC", True), (lib.root_b, "g4b.flac", "FLAC", True),
            (lib.root_a, "g4.mp3", "MP3", False))):
        row = _row(f"{root}/dup4/{rel}", root, 950 + k, title="Dup G4 zork", artist="Dup Artist",
                   format=fmt, duplicate_group_id="g4", is_duplicate_primary=prim)
        s.upsert_track(row)
        ids.append(row["id"])
    s.set_config("filter_duplicates", True)
    lib.mode(mode)
    lib.offline()
    assert availability.primary_swap(s).stand_in == {ids[0]: ids[2], ids[1]: ids[2]}
    stored = {tid for tid, t in s._tracks.items() if t.get("is_duplicate_primary", True)}
    want = (stored - set(ids[:2])) | {ids[2]}
    if mode == "hide":
        want -= set(lib.ids_b)
    got = await _paged("/api/tracks?sort=title")
    assert len(got) == len(set(got)) == len(want) and set(got) == want
    assert (await _get("/api/tracks/count"))["visible"] == len(want)
    assert len(s.filter_track_ids(filter_duplicates=True, swap=availability.primary_swap(s))) \
        == len((stored - set(ids[:2])) | {ids[2]})


@pytest.mark.asyncio
async def test_dedup_show_mode_lists_the_primaries_as_before(lib):
    d = _add_dups(lib)
    lib.mode("show")
    lib.offline()
    stored = {tid for tid, t in lib.store._tracks.items() if t.get("is_duplicate_primary", True)}
    rows = {r["id"]: r for r in await _get("/api/tracks?limit=500")}
    assert set(rows) == stored and d["p1"] in rows and "offline" not in rows[d["p1"]]
    assert (await _get("/api/tracks/count"))["visible"] == len(stored)
    shuf = await _get("/api/tracks/shuffled?seed=31&limit=500")
    assert {t["id"] for t in shuf["tracks"]} == stored
    assert {f["format"]: f["count"] for f in await _agg(library.list_formats)}["FLAC"] == 3
    assert availability.primary_swap(lib.store) is None


@pytest.mark.asyncio
async def test_dedup_off_lists_every_copy_as_before(lib):
    d = _add_dups(lib)
    lib.store.set_config("filter_duplicates", False)
    lib.mode("mark")
    lib.offline()
    rows = {r["id"]: r for r in await _get("/api/tracks?limit=500")}
    assert len(rows) == 107 and rows[d["p1"]]["offline"] == "drive"
    assert (await _get("/api/tracks/count"))["visible"] == 107
    assert availability.primary_swap(lib.store) is None


def test_primary_swap_picks_the_best_playable_copy_and_follows_writes(lib):
    d = _add_dups(lib)
    s = lib.store
    lib.mode("mark")
    assert availability.primary_swap(s) is None                # every drive connected
    lib.offline()
    sw = availability.primary_swap(s)
    assert sw.demoted == {d["p1"]} and sw.promoted == {d["c1"]}
    assert sw.stand_in == {d["p1"]: d["c1"]}
    assert sw.is_primary(d["c1"], s.get_track(d["c1"])) and not sw.is_primary(
        d["p1"], s.get_track(d["p1"])) and not sw.is_primary(d["c1b"], s.get_track(d["c1b"]))
    assert availability.primary_swap(s) is sw                  # memoised
    # Both builds — from the unplayable ids (fewer than the groups) and from the
    # groups (fewer than the ids) — find the same, with group ids filed under
    # their index key ("G1" → "g1").
    few = frozenset({d["p1"], d["p2"], d["c2"]})              # 3 ids, 3 groups
    assert len(few) <= len(s._tag_dup_group)
    assert s.primary_swap(few).stand_in == {d["p1"]: d["c1"]}
    assert s.primary_swap(frozenset({d["p2"]})).stand_in == {d["p2"]: d["c2"]}
    many = frozenset({d["p3"]} | set(lib.ids_a[:10]))         # 11 ids: G3's primary only
    assert len(many) > len(s._tag_dup_group)
    assert s.primary_swap(many).stand_in == {d["p3"]: d["c3"]}
    assert s.shown_member(d["c1"]) == d["p1"] and s.shown_member(d["p1"], sw) == d["c1"]
    assert s.shown_member(d["c1b"], sw) == d["c1"] and s.shown_member(lib.ids_a[0], sw) == lib.ids_a[0]
    sw = availability.primary_swap(s)                          # (rebuilt: the direct calls took the memo)
    assert sw.stand_in == {d["p1"]: d["c1"]}
    # a write that changes the pick (the 320 kbps copy is gone): the 128 kbps one stands in
    s.delete_track(d["c1"])
    sw2 = availability.primary_swap(s)
    assert sw2.stand_in == {d["p1"]: d["c1b"]}
    # a write that changes nothing about it: the same object (memo keys hold)
    t = copy.deepcopy(s.get_track(lib.ids_a[5]))
    t["title"] = "Retitled zork"
    s.upsert_track(t)
    assert availability.primary_swap(s) is sw2
    # the primary itself moved to a playable copy (set primary): nothing to swap
    s.update_track_fields_batch([(d["p1"], {"is_duplicate_primary": False}),
                           (d["c1b"], {"is_duplicate_primary": True})])
    assert availability.primary_swap(s) is None
    s.update_track_fields_batch([(d["p1"], {"is_duplicate_primary": True}),
                           (d["c1b"], {"is_duplicate_primary": False})])
    assert availability.primary_swap(s).stand_in == {d["p1"]: d["c1b"]}
    # "Hide duplicates" off: nothing to swap
    s.set_config("filter_duplicates", False)
    assert availability.primary_swap(s) is None
    s.set_config("filter_duplicates", True)
    # the drive back: nothing; a different root offline: G3's primary on A, its copy on B plays
    lib.online()
    assert availability.primary_swap(s) is None
    lib.offline(lib.root_a)
    assert availability.primary_swap(s).stand_in == {d["p3"]: d["c3"]}


def test_cost_of_the_primary_swap_on_a_big_store(monkeypatch):
    """60 000 tracks, 20 000 on a root that goes offline, 10 000 of those FLAC
    primaries whose MP3 copy sits on the other root: the swap is built once per
    availability change, and the deduped pages, counts and shuffle candidates
    with it stay in the cost range of the Hide set's (generous bounds; the
    real numbers are in the report)."""
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    availability.reset_for_tests()
    ra, rb = "/Volumes/BigA", "/Volumes/BigB"
    tracks = {}
    for i in range(60_000):
        root = rb if i < 20_000 else ra
        kw = {}
        if i < 10_000:
            kw = dict(format="FLAC", duplicate_group_id=f"g{i}", is_duplicate_primary=True)
        elif 20_000 <= i < 30_000:
            kw = dict(format="MP3", duplicate_group_id=f"g{i - 20_000}", is_duplicate_primary=False)
        t = _row(f"{root}/d{i // 40}/t{i}.mod", root, i, **kw)
        tracks[t["id"]] = t
    sd = {r: {"path": r, "track_count": 0, "added_at": 0, "last_scanned": 0,
              "network_share_id": None, "status": "ok", "path_hash": ph(r)} for r in (ra, rb)}
    s.bulk_load(tracks, {}, {}, {}, {}, [], sd, {}, {})
    s.rebuild_indexes()
    s.set_config("filter_duplicates", True)
    s.set_config(availability.MODE_KEY, "mark")
    s.set_scan_dir_status(rb, "unavailable")
    availability.snapshot(s).unavailable                      # (the Hide set's own cost: not measured here)
    t0 = time.perf_counter()
    sw = availability.primary_swap(s)
    build_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    assert availability.primary_swap(s) is sw
    warm_us = (time.perf_counter() - t0) * 1e6
    assert len(sw.demoted) == len(sw.promoted) == 10_000
    assert build_ms < 250 and warm_us < 2000, (build_ms, warm_us)
    t0 = time.perf_counter()
    first = s.filter_tracks(limit=200, offset=0, filter_duplicates=True, swap=sw)
    first_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    deep = s.filter_tracks(limit=2000, offset=30_000, filter_duplicates=True, swap=sw,
                           sort_by="title")
    deep_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    deep2 = s.filter_tracks(limit=2000, offset=32_000, filter_duplicates=True, swap=sw,
                            sort_by="title")
    deep_warm_ms = (time.perf_counter() - t0) * 1000
    assert len(first) == 200 and len(deep) == len(deep2) == 2000
    assert not {r["id"] for r in first + deep + deep2} & sw.demoted
    assert first_ms < 60 and deep_ms < 500 and deep_warm_ms < 60, (first_ms, deep_ms, deep_warm_ms)
    t0 = time.perf_counter()
    n = s.visible_track_count(primary_only=True, swap=sw)
    ids = s.filter_track_ids(filter_duplicates=True, swap=sw)
    count_ms = (time.perf_counter() - t0) * 1000
    assert n == len(ids) == len(set(ids)) == 50_000 and sw.promoted <= set(ids)
    assert count_ms < 250, count_ms
    print(f"\nswap build {build_ms:.1f} ms, warm {warm_us:.0f} µs; first page {first_ms:.2f} ms; "
          f"deep page {deep_ms:.1f} ms → {deep_warm_ms:.2f} ms; count+ids {count_ms:.1f} ms")
    availability.reset_for_tests()
