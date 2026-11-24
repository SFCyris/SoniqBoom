# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A network share's folder lists its OWN tracks (``tracks-with-meta?recursive=
false`` on an smb / ftp / WebDAV folder) in path order, every one of them: the
rows its recursive listing (Play all / Shuffle all on the phone, the desktop's
branch view) starts with, and the rows its scan filed under it (``dir_hash``:
the parent folder of the file's share path) — save an old build's SMB path a
level below a doubled "/" (``Box//sub/x.mod``), which is in no folder's own
listing, only in the recursive ones.  Until 2026-11-24 that listing was
``store.filter_tracks(dir_hash=…, scan_root_hash=…, limit=5000)``: newest first
and cut at 5,000 rows.  Pages and counts, the player's ``playable=1`` pages, a
seeded shuffle and "Hide duplicates when browsing folders" read the one listing.
(``test_fstree_recursive_order`` covers the recursive listings.)"""
from __future__ import annotations

import random
from collections import OrderedDict
from pathlib import PurePosixPath

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import fstree
from soniqboom.core import availability, filesource, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.data import path_hash
from soniqboom.core.store import TrackStore

ROOT = "smb://nas.invalid/Music"
RH = path_hash(ROOT)

# A share's files, relative to its root, deliberately not in path order.
TREE = [
    "top b.mp3", "top a.mp3",
    "Box/03 Third.flac", "Box/01 First.flac", "Box/02 Second.flac",
    "Box/Album/01.flac", "Box/Album 2/01.flac", "Box/Album.flac", "Box/Albums/x.flac",
    "Box/CD10/01.flac", "Box/CD2/01.flac", "Box/CD2/Bonus/x.mp3",
    "Box/pack.zip::c.mod", "Box/pack.zip::a.mod",              # an archive's top-level members
    "Box/pack.zip::inner/d.mod", "Box/pack.zip::inner/deeper/e.mod",   # a folder inside it
    "Box//legacy.mod",            # an old build's SMB path spelling (``scanner._healed_smb_rel``)
    "Box/\U0010FFFFz/2.mod", "Box/\U0001F600.mod",
    "Branch/Sub/x.mod",
]
FOLDERS = ["", "Box", "Box/Album", "Box/Album 2", "Box/Albums", "Box/CD10", "Box/CD2",
           "Box/CD2/Bonus", "Box/pack.zip::inner", "Box/pack.zip::inner/deeper",
           "Box/\U0010FFFFz", "Branch", "Branch/Sub"]


class _Stub:
    def close(self):
        pass


def _dir_hash(rel: str) -> str:
    """The ``dir_hash`` a share scan gives the file at ``rel``: its remote
    path's parent folder (``scanner``: ``PurePosixPath(remote_path).parent``)."""
    return path_hash(str(PurePosixPath("/" + rel).parent))


def _row(n: int, rel: str, root: str = ROOT, **extra) -> dict:
    # (``added_at`` grows with ``n``: a later row is a newer track)
    return {"id": f"t{n:05d}", "path": f"{root}:/{rel}", "title": rel.rsplit("/", 1)[-1],
            "format": "FLAC", "duration": 100.0 + n, "added_at": 1_700_000_000 + n,
            "dir_hash": _dir_hash(rel), "scan_root_hash": path_hash(root), **extra}


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    for name in ("_TRACKS_META_CACHE", "_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO", "_CAT_SORTED", "_ROOT_GEN"):
        monkeypatch.setattr(fstree, name, {})
    monkeypatch.setattr(fstree, "_NAT_LISTINGS", OrderedDict())
    shuffle_order.clear()
    availability.reset_for_tests()
    yield s
    shuffle_order.clear()
    availability.reset_for_tests()


def _register(store, root: str) -> None:
    store.upsert_scan_dir(root)
    filesource.register_source(root, _Stub())


@pytest.fixture
def share(store):
    _register(store, ROOT)
    yield store
    filesource.remove_source(ROOT)


def _fill(store, rels, root: str = ROOT):
    for n, rel in enumerate(rels):
        store.upsert_track(_row(n, rel, root))


def _folder(rel: str, root: str = ROOT) -> str:
    return f"{root}/{rel}" if rel else root


def _prefix(rel: str, root: str = ROOT) -> str:
    return f"{root}:/" + (f"{rel}/" if rel else "")


async def _list(rel, root: str = ROOT, **kw):
    kw.setdefault("recursive", False)
    kw.setdefault("offset", 0)
    kw.setdefault("limit", 0)
    kw.setdefault("filter_duplicates", False)
    kw.setdefault("shuffle_seed", None)
    kw.setdefault("playable", False)
    return await fstree.tracks_with_meta(path=_folder(rel, root), **kw)


def _rows(res):
    return res["tracks"] if isinstance(res, dict) else res


def _rels(rows, rel="", root: str = ROOT):
    pre = _prefix(rel, root)
    return [r["path"][len(pre):] for r in rows]


def _own_ref(rel: str, root: str = ROOT, tree=TREE) -> list[str]:
    """The folder's own files written the plain way — those whose share path's
    parent folder is the folder — in path order."""
    want = "/" + rel if rel else "/"
    return sorted(f"{root}:/{r}" for r in tree if str(PurePosixPath("/" + r).parent) == want)


def _filed_under(store, rel: str, root: str = ROOT) -> set[str]:
    """The ids the old listing read: ``filter_tracks`` by the folder's dir hash
    (unbounded here)."""
    dh = path_hash("/" + rel if rel else "/")
    return {d["id"] for d in store.filter_tracks(dir_hash=dh, scan_root_hash=path_hash(root),
                                                 limit=10**6)}


async def _check_every_folder(store, root: str = ROOT) -> None:
    for rel in FOLDERS:
        own = _rows(await _list(rel, root))
        assert [r["path"] for r in own] == _own_ref(rel, root), rel
        assert {r["id"] for r in own} == _filed_under(store, rel, root), rel   # the old rows
        deep = _rows(await _list(rel, root, recursive=True))
        assert deep[:len(own)] == own, rel             # what its recursive listing starts with
        pre = len(_prefix(rel, root))
        assert [r for r in deep if not r["path"][pre:r["path"].rfind("/")]] == own, rel
        assert (await _list(rel, root, limit=1))["total"] == len(own), rel   # the phone's count


# ── the order ──────────────────────────────────────────────────────────────

async def test_a_folder_lists_its_tracks_in_the_order_play_all_plays_them(share):
    # As found: three tracks added in this order listed 03, 02, 01 in their
    # folder, and 01, 02, 03 in its recursive listing (Play all).
    _fill(share, ["Album/01 One.flac", "Album/02 Two.flac", "Album/03 Three.flac"])
    want = ["01 One.flac", "02 Two.flac", "03 Three.flac"]
    assert _rels(_rows(await _list("Album")), "Album") == want
    assert _rels(_rows(await _list("Album", recursive=True)), "Album") == want
    page = await _list("Album", limit=2)
    assert page["total"] == 3 and _rels(page["tracks"], "Album") == want[:2]


async def test_the_order_is_the_paths_not_the_age(share):
    # Added out of order: newest first would be 01, 03, 02.
    _fill(share, ["Album/02 Two.flac", "Album/03 Three.flac", "Album/01 One.flac"])
    assert _rels(_rows(await _list("Album")), "Album") == [
        "01 One.flac", "02 Two.flac", "03 Three.flac"]


async def test_every_folder_lists_the_rows_its_scan_filed_under_it(share):
    _fill(share, TREE)
    await _check_every_folder(share)
    # The archive's top-level members are Box's own; its inner folder is not.
    box = _rels(_rows(await _list("Box")), "Box")
    assert {"pack.zip::a.mod", "pack.zip::c.mod", "/legacy.mod"} <= set(box)
    assert not [r for r in box if "inner" in r]


@pytest.mark.parametrize("root", ["ftp://nas.invalid/pub/music",
                                  "https://dav.invalid/remote.php/dav/files/me/Music"])
async def test_ftp_and_webdav_folders_list_the_same_way(store, root):
    _register(store, root)
    try:
        _fill(store, TREE, root)
        await _check_every_folder(store, root)
    finally:
        filesource.remove_source(root)


async def test_the_share_root_and_a_trailing_slash(share):
    _fill(share, TREE)
    assert _rels(_rows(await _list(""))) == ["top a.mp3", "top b.mp3"]
    slash = await fstree.tracks_with_meta(path=_folder("Box") + "/", recursive=False, offset=0,
                                          limit=0, filter_duplicates=False, shuffle_seed=None,
                                          playable=False)
    assert slash == _rows(await _list("Box"))        # (as the recursive listing reads it)


async def test_an_old_builds_path_below_a_doubled_slash_is_in_no_folders_own_listing(share):
    # ``Box//sub/x.mod`` (an old build's SMB spelling, healed by the next walk):
    # its scan filed it under Box/sub, but the index holds it apart from
    # Box/sub's own rows, so like the recursive listing of Box/sub the own
    # listing leaves it out; Box's recursive listing has it.  (A known
    # difference from the old dir-hash listing.)
    _fill(share, ["Box/sub/real.mod", "Box//sub/x.mod", "Box/a.mod"])
    assert _rels(_rows(await _list("Box/sub")), "Box/sub") == ["real.mod"]
    assert _rels(_rows(await _list("Box/sub", recursive=True)), "Box/sub") == ["real.mod"]
    assert _rels(_rows(await _list("Box")), "Box") == ["a.mod"]
    assert "/sub/x.mod" in _rels(_rows(await _list("Box", recursive=True)), "Box")


# ── every track, one order for pages, counts, the player's pages and shuffles ─

async def test_a_folder_of_more_than_5000_tracks_lists_and_pages_them_all(share):
    names = [f"Flat/{i:05d}.mod" for i in range(6001)]
    order = names[:]
    random.Random(5001).shuffle(order)
    for n, rel in enumerate(order):                     # added in a random order
        share.upsert_track(_row(n, rel))
    share.upsert_track(_row(9000, "Flat/Sub/inside.mod"))   # a subfolder's: not Flat's own
    full = _rows(await _list("Flat"))
    assert _rels(full, "Flat") == [n[len("Flat/"):] for n in names]
    ids = [r["id"] for r in full]
    for size in (1000, 2500):
        got = []
        for off in range(0, len(ids) + size, size):
            page = await _list("Flat", offset=off, limit=size)
            assert page["total"] == 6001
            got += [r["id"] for r in page["tracks"]]
        assert got == ids, size
    assert (await _list("Flat", limit=1))["total"] == 6001
    assert await _list("Flat", offset=6001, limit=50) == {"total": 6001, "tracks": []}
    # Own listings are built per request: the kept-listing slots stay with the
    # recursive listings (which cost an ordering).
    assert not fstree._NAT_LISTINGS


@pytest.mark.parametrize("dedup", [False, True])
async def test_pages_the_players_pages_and_a_shuffle_read_the_one_order(share, dedup):
    _fill(share, TREE)
    full = [r["id"] for r in _rows(await _list("Box", filter_duplicates=dedup))]
    assert len(full) == len(_own_ref("Box"))
    for size in (1, 2, 3, 100):
        got = []
        for off in range(0, len(full) + size, size):
            page = await _list("Box", offset=off, limit=size, filter_duplicates=dedup)
            assert page["total"] == len(full)
            got += [r["id"] for r in page["tracks"]]
        assert got == full, size
    got, pos, off = [], [], 0
    for _ in range(50):
        page = await _list("Box", offset=off, limit=3, playable=True, filter_duplicates=dedup)
        got += [r["id"] for r in page["tracks"]]
        pos += [r["_pos"] for r in page["tracks"]]
        off = page["next_offset"]
        if page["ended"]:
            break
    assert got == full and pos == list(range(len(full)))   # ``_pos``: the place in the listing
    shuf = []
    for off in range(0, len(full) + 3, 3):
        page = await _list("Box", offset=off, limit=3, shuffle_seed=11, filter_duplicates=dedup)
        assert page["total"] == len(full)
        shuf += [r["id"] for r in page["tracks"]]
    # The permutation depends on the ids only — not on the listing's order.
    assert shuf == shuffle_order.seeded_order(list(reversed(full)), 11)


async def test_hide_duplicates_leaves_out_a_lesser_copy_beside_a_better_one(share):
    # "Hide duplicates when browsing folders": a lesser copy beside a better
    # one in the SAME folder is left out; a copy in another folder is listed.
    _fill(share, TREE)
    n = TREE.index("Box/01 First.flac")
    share.upsert_track(_row(n, "Box/01 First.flac", duplicate_group_id="g1"))
    share.upsert_track(_row(700, "Box/01 First.mp3", format="MP3", bitrate=128_000,
                            duplicate_group_id="g1"))
    share.upsert_track(_row(701, "Box/CD2/01 First.mp3", format="MP3", bitrate=320_000,
                            duplicate_group_id="g1"))
    every = _rels(_rows(await _list("Box")), "Box")
    shown = _rels(_rows(await _list("Box", filter_duplicates=True)), "Box")
    assert "01 First.mp3" in every and shown == [r for r in every if r != "01 First.mp3"]
    assert shown == sorted(shown)
    page = await _list("Box", offset=1, limit=2, filter_duplicates=True)
    assert page["total"] == len(shown) and _rels(page["tracks"], "Box") == shown[1:3]
    cd2 = _rels(_rows(await _list("Box/CD2", filter_duplicates=True)), "Box/CD2")
    assert cd2 == ["01 First.mp3", "01.flac"]


# ── the listing follows the index ────────────────────────────────────────────

async def test_the_own_listing_follows_the_index(share):
    _fill(share, TREE)
    box = _folder("Box")

    async def own() -> list[str]:
        return [r["path"] for r in await fstree._remote_tracks_with_meta(box, False)]
    assert await own() == _own_ref("Box")
    # A track comes in (a scan's upsert): the index is built again.
    share.upsert_track(_row(500, "Box/02a Inserted.flac"))
    inserted = sorted(_own_ref("Box") + [f"{ROOT}:/Box/02a Inserted.flac"])
    assert await own() == inserted
    # A commit that moves a track (one row out, one in: the count stays the
    # same): while it is in flight the listing reads the rows from before it,
    # as the recursive listing does; once it patched the index, the new ones.
    gone = f"{ROOT}:/Box/03 Third.flac"
    tid = next(t for t, d in share._tracks.items() if d["path"] == gone)
    with fstree.root_commit([RH]):
        share.delete_track(tid)
        moved = _row(800, "Box/00 Third.flac")
        share.upsert_track(moved)
        assert await own() == inserted
        assert await fstree.patch_scan_root_rows(RH, {gone}, [share._tracks[moved["id"]]],
                                                 len(share._tag_scan_root_hash[RH]))
    after = sorted([p for p in inserted if p != gone] + [f"{ROOT}:/Box/00 Third.flac"])
    assert await own() == after
    # A field-only update (no count moves) is read once the browse caches are
    # dropped (``invalidate_browse_cache``).
    share.update_track_fields_batch([(t, {"album": "NEW"}) for t in list(share._tracks)])
    fstree.invalidate_browse_cache(drop_disk=False)
    rows = await fstree._remote_tracks_with_meta(box, False)
    assert [r["path"] for r in rows] == after and {r["album"] for r in rows} == {"NEW"}


# ── through the HTTP route ───────────────────────────────────────────────────

async def test_the_route_answers_the_same_listing(share):
    _fill(share, TREE)
    app = FastAPI()
    app.include_router(fstree.router, prefix="/api")
    own = _own_ref("Box")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/api/fstree/tracks-with-meta",
                        params={"path": _folder("Box"), "recursive": "false",
                                "offset": "2", "limit": "3"})
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["total"] == len(own) and [t["path"] for t in body["tracks"]] == own[2:5]
        r = await c.get("/api/fstree/tracks-with-meta",
                        params={"path": _folder("Box"), "recursive": "false"})
        assert [t["path"] for t in r.json()] == own
