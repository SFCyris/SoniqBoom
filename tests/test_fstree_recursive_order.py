# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recursive folder listings (``tracks-with-meta?recursive=true``: a branch
folder's flat view and its queue on the desktop, Play all / Shuffle all on the
phone) go folder by folder in the order the folder lists show — every folder
of a row's path by ``fstree._folder_sort_key`` (CD2 before CD10) — a folder's
own tracks before its subfolders', each subfolder whole, and the tracks of one
folder in path order (what that folder's own listing shows).  Pages,
``playable=1`` pages and a seeded shuffle read that one listing; the scan
roots' sorted path indexes stay in plain path order (they are bisected).
Local folders, network shares and catalogue roots each build the listing their
own way, so each is checked."""
from __future__ import annotations

import asyncio
import random
from collections import OrderedDict

import pytest

from soniqboom.api import fstree
from soniqboom.core import availability, catalogue, filesource, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.data import path_hash
from soniqboom.core.store import TrackStore

# A box set, its files deliberately out of order.  Paths relative to "Box".
BOX = [
    "CD10/01 Never Let Me Down Again.flac",
    "CD10/02 The Things You Said.flac",
    "CD2/01 Leave in Silence.flac",
    "CD2/Bonus 10/x.mp3",
    "CD2/Bonus 9/x.mp3",
    "CD1/01 New Life.flac",
    "CD1/10 Big Muff.flac",
    "CD1/9 Photographic.flac",
    "CD1/02 Puppets.flac",
    "cd11/01 Personal Jesus.flac",
    "CD14/01 Precious.flac",
    "Disc 10/01.flac",
    "Disc 9/01.flac",
    "booklet intro.mp3",
    "CD15 hidden track.mp3",
    "pack.zip::c.mod",
    "pack.zip::inner/d.mod",
]
BOX_ORDER = [
    # Box's own tracks first, in path order (an archive's top-level member is one)
    "CD15 hidden track.mp3", "booklet intro.mp3", "pack.zip::c.mod",
    # then its folders as the folder list shows them, each one whole; the
    # tracks of one folder in path order ("10 …" before "9 …", as CD1 lists them)
    "CD1/01 New Life.flac", "CD1/02 Puppets.flac", "CD1/10 Big Muff.flac", "CD1/9 Photographic.flac",
    "CD2/01 Leave in Silence.flac", "CD2/Bonus 9/x.mp3", "CD2/Bonus 10/x.mp3",
    "CD10/01 Never Let Me Down Again.flac", "CD10/02 The Things You Said.flac",
    "cd11/01 Personal Jesus.flac",
    "CD14/01 Precious.flac",
    "Disc 9/01.flac", "Disc 10/01.flac",
    "pack.zip::inner/d.mod",                 # a folder inside an archive is a subfolder
]
BOX_FOLDERS = ["CD1", "CD2", "CD10", "cd11", "CD14", "Disc 9", "Disc 10", "pack.zip::inner"]


class _Stub:
    def close(self):
        pass


def _row(n: int, path: str, root_hash: str, **extra) -> dict:
    return {"id": f"t{n:05d}", "path": path, "title": path.rsplit("/", 1)[-1],
            "format": "FLAC", "duration": 100.0 + n, "added_at": 1_700_000_000 + n,
            "scan_root_hash": root_hash, **extra}


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


@pytest.fixture
def local(store, tmp_path):
    root = str(tmp_path.resolve())
    (tmp_path / "Box" / "CD1").mkdir(parents=True)
    store.upsert_scan_dir(root)
    rh = path_hash(root)
    for i, rel in enumerate(BOX):
        store.upsert_track(_row(i, f"{root}/Box/{rel}", rh))
    return root


@pytest.fixture
def share(store):
    root = "smb://nas.invalid/Music"
    store.upsert_scan_dir(root)
    filesource.register_source(root, _Stub())
    rh = path_hash(root)
    for i, rel in enumerate(BOX):
        store.upsert_track(_row(i, f"{root}:/Box/{rel}", rh))
    yield root
    filesource.remove_source(root)


CAT_ROOT = "ftp://catalogue.invalid/pub/modules"


@pytest.fixture
def cat(store):
    catalogue.register_root(CAT_ROOT, key="testcat", label="Test catalogue")
    store.upsert_scan_dir(CAT_ROOT)
    filesource.register_source(CAT_ROOT, _Stub())
    rh = path_hash(CAT_ROOT)
    for i, rel in enumerate(BOX):
        store.upsert_track(_row(i, f"{CAT_ROOT}:/Box/{rel}", rh))
    yield CAT_ROOT
    filesource.remove_source(CAT_ROOT)
    catalogue.unregister_root(CAT_ROOT)


async def _list(path, **kw):
    kw.setdefault("recursive", True)
    kw.setdefault("offset", 0)
    kw.setdefault("limit", 0)
    kw.setdefault("filter_duplicates", False)
    kw.setdefault("shuffle_seed", None)
    kw.setdefault("playable", False)
    return await fstree.tracks_with_meta(path=path, **kw)


def _rows(res):
    return res["tracks"] if isinstance(res, dict) else res


def _rel(rows, base):
    return [r["path"][len(base):] for r in rows]


def _folder_key_ref(rel: str) -> tuple:
    """The reference order, written the plain way: each folder of the path by
    ``_folder_sort_key``, then the path."""
    return tuple(fstree._folder_sort_key(f) for f in rel.split("/")[:-1]), rel


def _bases(kind, root):
    """``(folder path to ask for, prefix of its rows' paths)``."""
    if kind == "local":
        return f"{root}/Box", f"{root}/Box/"
    return f"{root}/Box", f"{root}:/Box/"


@pytest.fixture(params=["local", "share", "catalogue"])
def any_root(request):
    name = {"local": "local", "share": "share", "catalogue": "cat"}[request.param]
    return request.param, request.getfixturevalue(name)


# ── the order ──────────────────────────────────────────────────────────────

async def test_a_box_set_lists_in_folder_order(any_root):
    kind, root = any_root
    folder, base = _bases(kind, root)
    rows = _rows(await _list(folder))
    assert _rel(rows, base) == BOX_ORDER
    assert BOX_ORDER == sorted(BOX, key=_folder_key_ref)       # (the rule, written plainly)
    # The folders come in the order the folder list shows them.
    first_seen = list(dict.fromkeys(r.split("/")[0] for r in BOX_ORDER if "/" in r))
    assert first_seen == BOX_FOLDERS
    if kind == "local":
        kids = fstree._local_children_from_store(store_mod._store, folder, root)
    else:
        kids = fstree._remote_children_from_store(store_mod._store, root, "/Box")
    assert [k["name"] for k in kids] == BOX_FOLDERS


async def test_the_folder_list_order_is_the_tree_order_at_every_depth(local):
    # A whole root: Box is one folder among others, CD2's bonus folders nest.
    rows = _rows(await _list(local))
    assert _rel(rows, local + "/Box/") == BOX_ORDER


async def test_one_folder_lists_its_tracks_as_the_recursive_listing_does(local):
    box = f"{local}/Box"
    deep = _rel(_rows(await _list(box)), box + "/")
    own = _rel(_rows(await _list(box, recursive=False)), box + "/")
    assert own == ["CD15 hidden track.mp3", "booklet intro.mp3", "pack.zip::c.mod"]
    assert deep[:len(own)] == own
    cd1 = _rel(_rows(await _list(f"{box}/CD1", recursive=False)), box + "/")
    assert cd1 == [r for r in BOX_ORDER if r.startswith("CD1/")]
    i = deep.index(cd1[0])
    assert deep[i:i + len(cd1)] == cd1


# ── one order for pages, counts, the player's pages and shuffles ─────────────

@pytest.mark.parametrize("dedup", [False, True])
async def test_pages_tile_the_listing(any_root, dedup):
    kind, root = any_root
    folder, base = _bases(kind, root)
    full = [r["id"] for r in _rows(await _list(folder, filter_duplicates=dedup))]
    assert len(full) == len(BOX)
    count = await _list(folder, limit=1, filter_duplicates=dedup)       # the phone's count
    assert count["total"] == len(full) and [r["id"] for r in count["tracks"]] == full[:1]
    for size in (1, 2, 5, 7, 100):
        got = []
        for off in range(0, len(full) + size, size):
            page = await _list(folder, offset=off, limit=size, filter_duplicates=dedup)
            assert page["total"] == len(full)
            got += [r["id"] for r in page["tracks"]]
        assert got == full, size
    past = await _list(folder, offset=len(full) + 3, limit=5, filter_duplicates=dedup)
    assert past == {"total": len(full), "tracks": []}


async def test_the_players_pages_read_the_same_order(any_root):
    kind, root = any_root
    folder, _base = _bases(kind, root)
    full = [r["id"] for r in _rows(await _list(folder))]
    got, pos, off = [], [], 0
    for _ in range(50):
        page = await _list(folder, offset=off, limit=4, playable=True)
        got += [r["id"] for r in page["tracks"]]
        pos += [r["_pos"] for r in page["tracks"]]
        off = page["next_offset"]
        if page["ended"]:
            break
    assert got == full
    assert pos == list(range(len(full)))          # ``_pos``: the place in the ordered listing


async def test_a_shuffle_deals_the_listing_once_whatever_its_order(any_root):
    kind, root = any_root
    folder, _base = _bases(kind, root)
    full = [r["id"] for r in _rows(await _list(folder))]
    pages = []
    for off in range(0, len(full) + 4, 4):
        page = await _list(folder, offset=off, limit=4, shuffle_seed=11)
        assert page["total"] == len(full)
        pages += [r["id"] for r in page["tracks"]]
    assert sorted(pages) == sorted(full) and pages != full
    # The permutation depends on the ids only — not on the listing's order.
    assert pages == shuffle_order.seeded_order(list(reversed(full)), 11)
    again = await _list(folder, offset=0, limit=4, shuffle_seed=11)
    assert [r["id"] for r in again["tracks"]] == pages[:4]


# ── the index stays bisectable ───────────────────────────────────────────────

async def test_the_sorted_path_indexes_stay_in_plain_order(any_root):
    kind, root = any_root
    folder, _base = _bases(kind, root)
    await _list(folder)
    await _list(folder, limit=3)
    h = path_hash(root)
    if kind == "catalogue":
        paths = fstree._CAT_SORTED[h]["paths"]
    else:
        paths = fstree._SCAN_ROOT_FULL_CACHE[h]["paths"]
    assert paths == sorted(paths) and len(paths) == len(BOX)


# ── kept listings follow the index ───────────────────────────────────────────

@pytest.mark.parametrize("kind", ["share", "catalogue"])
async def test_a_kept_listing_is_not_served_after_the_index_changed(kind, request, monkeypatch):
    monkeypatch.setattr(fstree, "_NAT_MIN_ROWS", 1)        # keep even a small listing
    root = request.getfixturevalue("share" if kind == "share" else "cat")
    folder, base = _bases(kind, root)
    first = _rows(await _list(folder))
    assert len(fstree._NAT_LISTINGS) == 1
    again = _rows(await _list(folder))
    assert _rel(again, base) == BOX_ORDER and _rel(first, base) == BOX_ORDER
    # A track comes in (a scan): the index is rebuilt, the listing with it.
    store_mod._store.upsert_track(_row(500, f"{root}:/Box/CD3/01 Sacred.flac", path_hash(root)))
    now = _rel(_rows(await _list(folder)), base)
    assert now == BOX_ORDER[:10] + ["CD3/01 Sacred.flac"] + BOX_ORDER[10:]
    count = await _list(folder, limit=1)
    assert count["total"] == len(BOX) + 1


async def test_a_dropped_root_drops_its_kept_listings(share, monkeypatch):
    monkeypatch.setattr(fstree, "_NAT_MIN_ROWS", 1)
    await _list(f"{share}/Box")
    assert fstree._NAT_LISTINGS
    fstree.invalidate_scan_root(path_hash(share))
    assert not fstree._NAT_LISTINGS


# ── nested roots, folders not indexed yet, odd names ─────────────────────────

async def test_nested_scan_roots_list_as_one_folder(store, tmp_path):
    # Two registered roots hold a folder (the outer one and the folder itself);
    # each track is tagged with the root that last scanned it.
    outer = str(tmp_path.resolve())
    inner = f"{outer}/Box/CD2"
    (tmp_path / "Box" / "CD2").mkdir(parents=True)
    store.upsert_scan_dir(outer)
    store.upsert_scan_dir(inner)
    tagged = {"01 Leave in Silence.flac": inner, "Bonus 10/x.mp3": inner,
              "Bonus 9/x.mp3": outer, "Bonus 2/y.mp3": outer, "Bonus 10/z.mp3": outer}
    for i, (rel, root) in enumerate(tagged.items()):
        store.upsert_track(_row(i, f"{inner}/{rel}", path_hash(root)))
    rows = _rows(await _list(inner))
    assert _rel(rows, inner + "/") == ["01 Leave in Silence.flac", "Bonus 2/y.mp3",
                                       "Bonus 9/x.mp3", "Bonus 10/x.mp3", "Bonus 10/z.mp3"]
    assert len({r["id"] for r in rows}) == len(rows) == len(tagged)
    page = await _list(inner, offset=3, limit=2)
    assert page["total"] == 5 and _rel(page["tracks"], inner + "/") == ["Bonus 10/x.mp3",
                                                                        "Bonus 10/z.mp3"]


# The other way round: a scan root BELOW the listed folder.  The scanner tags a
# file with whichever root read it last (each root's scan re-reads the files the
# other one holds; the watcher and a folder click's refresh re-tag single files),
# so the folders of the outer root list the tracks of the roots under them too.
NESTED = {"Box/booklet.mp3": "outer", "Box/CD1/01 New Life.flac": "outer",
          "Box/CD10/01 Never Let Me Down Again.flac": "outer",
          "Box/CD2/01 Leave in Silence.flac": "inner", "Box/CD2/Bonus 10/x.mp3": "inner",
          "Box/CD2/Bonus 9/x.mp3": "inner"}
NESTED_ORDER = ["booklet.mp3", "CD1/01 New Life.flac", "CD2/01 Leave in Silence.flac",
                "CD2/Bonus 9/x.mp3", "CD2/Bonus 10/x.mp3",
                "CD10/01 Never Let Me Down Again.flac"]


@pytest.fixture
def nested(store, tmp_path):
    outer = str(tmp_path.resolve())
    inner = f"{outer}/Box/CD2"
    (tmp_path / "Box" / "CD2").mkdir(parents=True)
    store.upsert_scan_dir(outer)
    store.upsert_scan_dir(inner)
    roots = {"outer": outer, "inner": inner}
    for i, (rel, which) in enumerate(NESTED.items()):
        store.upsert_track(_row(i, f"{outer}/{rel}", path_hash(roots[which])))
    return outer, inner


async def test_a_scan_root_below_the_folder_lists_with_it(nested):
    outer, inner = nested
    box = f"{outer}/Box"
    rows = _rows(await _list(box))
    assert _rel(rows, box + "/") == NESTED_ORDER
    assert len({r["id"] for r in rows}) == len(rows)
    # The phone's count, a page, a shuffle and the play queue's pages read it.
    assert (await _list(box, limit=1))["total"] == len(NESTED)
    assert _rel((await _list(box, offset=2, limit=3))["tracks"], box + "/") == NESTED_ORDER[2:5]
    dealt = _rows(await _list(box, shuffle_seed=7))
    assert sorted(r["id"] for r in dealt) == sorted(r["id"] for r in rows)
    queue = await _list(box, playable=True, limit=50)
    assert queue["ended"] and _rel(queue["tracks"], box + "/") == NESTED_ORDER
    # The outer root itself lists them too; a folder's own tracks stay its own.
    assert _rel(_rows(await _list(outer)), outer + "/Box/") == NESTED_ORDER
    assert _rel(_rows(await _list(box, recursive=False)), box + "/") == ["booklet.mp3"]
    assert _rel(_rows(await _list(inner, recursive=False)), inner + "/") == [
        "01 Leave in Silence.flac"]


async def test_the_folder_tree_lists_a_folder_a_root_below_holds(nested):
    outer, _inner = nested
    res = await fstree.get_children(path=f"{outer}/Box", root=None)
    assert [c["name"] for c in res["children"]] == ["CD1", "CD2", "CD10"]


async def test_a_kept_listing_sees_a_track_added_under_a_root_below(nested, store):
    outer, inner = nested
    box = f"{outer}/Box"
    assert len(_rows(await _list(box))) == len(NESTED)          # (kept for revisits)
    store.upsert_track(_row(50, f"{inner}/Bonus 9/y.mp3", path_hash(inner)))
    assert "CD2/Bonus 9/y.mp3" in _rel(_rows(await _list(box)), box + "/")
    assert (await _list(box, limit=1))["total"] == len(NESTED) + 1


async def test_a_kept_listing_is_not_served_once_other_roots_sit_below(nested, store):
    # The same bucket sizes and generations, other roots: a kept listing names
    # the roots it was read from, not only their sizes.
    outer, inner = nested
    box, other = f"{outer}/Box", f"{outer}/Box/CD3"
    for i in range(3):                       # as many tracks as the inner root holds
        store.upsert_track(_row(60 + i, f"{other}/{i:02d}.flac", path_hash(other)))
    assert "CD3/00.flac" not in _rel(_rows(await _list(box)), box + "/")   # (not a root yet)
    store.delete_scan_dir(inner)
    store.upsert_scan_dir(other)
    rel = _rel(_rows(await _list(box)), box + "/")
    assert "CD3/00.flac" in rel and not any(r.startswith("CD2/") for r in rel)


async def test_a_track_whose_tag_moves_between_the_roots_stays_listed_once(nested, store):
    # The scanner moves tags between nested roots (a scan of both swaps the
    # inner folder's; the watcher or a folder click re-tags single files), and
    # a swap can leave both roots' counts as they were.
    outer, inner = nested
    box, oh, ih = f"{outer}/Box", path_hash(outer), path_hash(inner)
    ids = [r["id"] for r in _rows(await _list(box))]
    track = {t["path"]: t for t in store._tracks.values()}
    store.upsert_track({**track[f"{inner}/01 Leave in Silence.flac"], "scan_root_hash": oh})
    rows = _rows(await _list(box))
    assert _rel(rows, box + "/") == NESTED_ORDER and [r["id"] for r in rows] == ids
    # Same counts as before: one track each way.
    store.upsert_track({**track[f"{inner}/Bonus 9/x.mp3"], "scan_root_hash": oh})
    store.upsert_track({**track[f"{inner}/01 Leave in Silence.flac"], "scan_root_hash": ih})
    cd2 = [r[len("CD2/"):] for r in NESTED_ORDER if r.startswith("CD2/")]
    for folder, want in ((box, NESTED_ORDER), (inner, cd2)):
        rows = _rows(await _list(folder))
        assert _rel(rows, folder + "/") == want
        assert len({r["id"] for r in rows}) == len(rows)
    assert (await _list(box, limit=1))["total"] == len(NESTED)


async def test_archive_members_a_root_below_holds_list_in_folder_order(store, tmp_path):
    outer = str(tmp_path.resolve())
    inner = f"{outer}/Box/CD2"
    (tmp_path / "Box" / "CD2").mkdir(parents=True)
    store.upsert_scan_dir(outer)
    store.upsert_scan_dir(inner)
    tagged = {"Box/CD1/a.mod": outer, "Box/CD2/pack.zip::inner/d.mod": inner,
              "Box/CD2/pack.zip::c.mod": inner, "Box/CD2/b.mod": outer}
    for i, (rel, root) in enumerate(tagged.items()):
        store.upsert_track(_row(70 + i, f"{outer}/{rel}", path_hash(root)))
    box = f"{outer}/Box"
    # (an archive's top-level member is a track of its folder; a folder in it is a subfolder)
    assert _rel(_rows(await _list(box)), box + "/") == [
        "CD1/a.mod", "CD2/b.mod", "CD2/pack.zip::c.mod", "CD2/pack.zip::inner/d.mod"]
    assert _rel(_rows(await _list(inner, recursive=False)), inner + "/") == [
        "b.mod", "pack.zip::c.mod"]
    kids = await fstree.get_children(path=inner, root=inner)
    assert [c["name"] for c in kids["children"]] == ["pack.zip::inner"]


async def test_the_startup_warmup_keeps_the_outer_roots_listing_whole(nested):
    outer, _inner = nested
    fstree.warmup_scan_root_caches()
    assert _rel(fstree._STORE_RECURSIVE_CACHE[outer]["results"], outer + "/Box/") == NESTED_ORDER


async def test_a_folder_not_indexed_yet_lists_in_folder_order(store, tmp_path):
    root = tmp_path.resolve()
    store.upsert_scan_dir(str(root))
    for rel in ("Box/CD10/a.mod", "Box/CD2/a.mod", "Box/CD1/b.mod", "Box/CD1/A.mod",
                "Box/z.mod", "Box/Y.mod"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\0" * 32)
    rows = _rows(await _list(str(root / "Box")))
    assert all(r.get("_scanned") is False for r in rows)        # (from the walk, not the index)
    # Box's own files by lower-cased path, as its own listing shows them.
    assert _rel(rows, f"{root}/Box/") == ["Y.mod", "z.mod", "CD1/A.mod", "CD1/b.mod",
                                          "CD2/a.mod", "CD10/a.mod"]
    own = _rel(_rows(await _list(str(root / "Box"), recursive=False)), f"{root}/Box/")
    assert own == ["Y.mod", "z.mod"]


async def test_a_name_starting_with_the_last_code_point_is_listed(any_root):
    # (The index bisect's end sentinel is U+10FFFF: a folder whose name starts
    # with it must still be listed — and counted.)
    kind, root = any_root
    folder, base = _bases(kind, root)
    for i, rel in enumerate(["a/1.mod", "\U0010FFFFz/2.mod", "\U0001F600/3.mod"]):
        store_mod._store.upsert_track(_row(900 + i, base + rel, path_hash(root)))
    rows = _rows(await _list(folder))
    assert _rel(rows, base) == (BOX_ORDER[:3] + ["a/1.mod"] + BOX_ORDER[3:]
                                + ["\U0001F600/3.mod", "\U0010FFFFz/2.mod"])
    assert (await _list(folder, limit=1))["total"] == len(BOX) + 3


def test_folder_order_matches_a_plain_sort_on_random_trees():
    rng = random.Random(20261124)
    words = ["CD", "cd", "Disc ", "Disc", "Bonus", "A", "a", "Ä", "ラブ", "\U0001F600",
             "x-", "x ", "Ghost", "Ghostemane", "pack.zip::inner", "01", "1", "10", "2", "007"]

    def name():
        return "".join(rng.choice(words) + (str(rng.randint(0, 120)) if rng.random() < .6 else "")
                       for _ in range(rng.randint(1, 2)))

    for _ in range(40):
        rels = set()
        for _ in range(rng.randint(1, 300)):
            depth = rng.randint(0, 4)
            rels.add("/".join([name() for _ in range(depth)] + [name() + ".mod"]))
        base = "/M/Lib/"
        paths = sorted(base + r for r in rels)
        order = fstree._subtree_order(paths, 0, len(paths), len(base))
        got = [paths[i][len(base):] for i in order]
        assert got == sorted(rels, key=_folder_key_ref)
        # The same rows in any order: one folder's rows keep THEIR order.
        rows = [{"path": p, "id": p} for p in paths]
        rng.shuffle(rows)
        got = fstree._in_folder_order(rows)
        want = sorted(rows, key=lambda r: tuple(fstree._folder_sort_key(f)
                                                for f in r["path"].split("/")[:-1]))
        assert got == want


def test_each_track_once_keeps_the_first_row():
    a, b, c = {"id": "1", "n": 1}, {"id": "2"}, {"id": "1", "n": 2}
    assert fstree._first_of_each_id([a, b, c]) == [a, b]
    rows = [a, b]
    assert fstree._first_of_each_id(rows) is rows


@pytest.mark.parametrize("kind", ["local", "share"])
async def test_hidden_duplicates_leave_the_order_and_the_pages_whole(kind, request):
    # "Hide duplicates when browsing folders": a lesser copy beside a better one
    # in the SAME folder is left out; copies in other folders are both listed.
    root = request.getfixturevalue(kind)
    folder, base = _bases(kind, root)
    h = path_hash(root)
    extra = [("CD1/01 New Life.mp3", "MP3", 128_000, "g1"),      # beside the FLAC: left out
             ("CD10/03 Behind the Wheel.flac", "FLAC", None, "g2"),
             ("CD2/03 Behind the Wheel.mp3", "MP3", 320_000, "g2")]   # other folder: listed
    n = BOX.index("CD1/01 New Life.flac")                 # (the same track, now in group g1)
    store_mod._store.upsert_track(_row(n, base + "CD1/01 New Life.flac", h, duplicate_group_id="g1"))
    for i, (rel, fmt, br, g) in enumerate(extra):
        store_mod._store.upsert_track(_row(700 + i, base + rel, h, format=fmt, bitrate=br,
                                           duplicate_group_id=g))
    every = _rel(_rows(await _list(folder)), base)
    shown = _rel(_rows(await _list(folder, filter_duplicates=True)), base)
    assert "CD1/01 New Life.mp3" in every and "CD1/01 New Life.mp3" not in shown
    assert shown == [r for r in every if r != "CD1/01 New Life.mp3"]
    assert shown == sorted(shown, key=_folder_key_ref)
    assert {"CD10/03 Behind the Wheel.flac", "CD2/03 Behind the Wheel.mp3"} <= set(shown)
    ids = [r["id"] for r in _rows(await _list(folder, filter_duplicates=True))]
    got = []
    for off in range(0, len(ids) + 3, 3):
        page = await _list(folder, offset=off, limit=3, filter_duplicates=True)
        assert page["total"] == len(ids)
        got += [r["id"] for r in page["tracks"]]
    assert got == ids
    play = await _list(folder, offset=0, limit=len(ids) + 5, playable=True, filter_duplicates=True)
    assert [r["id"] for r in play["tracks"]] == ids and play["ended"] is True
    shuf = await _list(folder, offset=0, limit=len(ids) + 5, shuffle_seed=3, filter_duplicates=True)
    assert shuf["total"] == len(ids) and sorted(r["id"] for r in shuf["tracks"]) == sorted(ids)


@pytest.mark.parametrize("kind", ["local", "share"])
async def test_a_commit_that_swaps_tracks_reorders_the_listing(kind, request, monkeypatch):
    # A scan commit that moves a track (one row out, one in: the count stays
    # the same) patches the root's index; the next listing is rebuilt from it.
    monkeypatch.setattr(fstree, "_NAT_MIN_ROWS", 1)        # a share keeps even a small listing
    root = request.getfixturevalue(kind)
    folder, base = _bases(kind, root)
    h = path_hash(root)
    assert _rel(_rows(await _list(folder)), base) == BOX_ORDER
    s = store_mod._store
    gone = f"{base}CD14/01 Precious.flac"
    tid = next(t for t, d in s._tracks.items() if d["path"] == gone)
    s.delete_track(tid)
    moved = _row(800, f"{base}CD3/01 Precious.flac", h)
    s.upsert_track(moved)
    with fstree.root_commit([h]):
        assert await fstree.patch_scan_root_rows(h, {gone}, [s._tracks[moved["id"]]],
                                                 len(s._tag_scan_root_hash[h]))
    want = [r for r in BOX_ORDER if r != "CD14/01 Precious.flac"]
    want.insert(want.index("CD10/01 Never Let Me Down Again.flac"), "CD3/01 Precious.flac")
    assert _rel(_rows(await _list(folder)), base) == want
    assert (await _list(folder, limit=1))["total"] == len(BOX)


@pytest.mark.parametrize("kind", ["local", "share"])
async def test_a_big_listing_is_ordered_off_the_loop_the_same_way(kind, request, monkeypatch):
    monkeypatch.setattr(fstree, "_ORDER_IN_THREAD_ROWS", 1)  # every listing goes to a thread
    threads = []
    real = fstree.asyncio.to_thread

    async def _spy(fn, *a, **kw):
        threads.append(getattr(fn, "__name__", "?"))
        return await real(fn, *a, **kw)
    monkeypatch.setattr(fstree.asyncio, "to_thread", _spy)
    root = request.getfixturevalue(kind)
    folder, base = _bases(kind, root)
    assert _rel(_rows(await _list(folder)), base) == BOX_ORDER
    assert ("_recursive_rows" if kind == "local" else "_ordered") in threads
    page = await _list(folder, offset=3, limit=4)
    assert _rel(page["tracks"], base) == BOX_ORDER[3:7] and page["total"] == len(BOX)


async def test_a_branch_folders_probe_builds_the_listing_its_pages_read(store, tmp_path):
    # The desktop asks a folder for its own tracks first; a branch folder has
    # none (total 0), and the recursive pages that follow read the listing
    # that probe already built and cached.
    root = str(tmp_path.resolve())
    for d in ("Set/CD1", "Set/CD2", "Set/CD10"):
        (tmp_path / d).mkdir(parents=True)
    store.upsert_scan_dir(root)
    for i, rel in enumerate(["CD10/a.flac", "CD2/a.flac", "CD1/b.flac", "CD1/a.flac"]):
        store.upsert_track(_row(i, f"{root}/Set/{rel}", path_hash(root)))
    probe = await _list(f"{root}/Set", recursive=False, limit=2000)
    assert probe == {"total": 0, "tracks": []}
    kept = fstree._STORE_RECURSIVE_CACHE[f"{root}/Set"]["results"]
    assert _rel(kept, f"{root}/Set/") == ["CD1/a.flac", "CD1/b.flac", "CD2/a.flac", "CD10/a.flac"]
    pages = []
    for off in (0, 2, 4):
        page = await _list(f"{root}/Set", offset=off, limit=2)
        assert page["total"] == 4
        pages += page["tracks"]
    assert [r["id"] for r in pages] == [r["id"] for r in kept]
    assert fstree._STORE_RECURSIVE_CACHE[f"{root}/Set"]["results"] is kept     # (read, not rebuilt)


async def test_a_cache_drop_while_a_listing_is_ordered_is_not_undone(local, monkeypatch):
    # A field-only update drops the browse caches (``invalidate_browse_cache``:
    # no count or generation moves).  A listing a worker thread was ordering
    # from the old rows meanwhile is returned to its request, but must not be
    # kept: the next request reads the new values.
    import threading
    monkeypatch.setattr(fstree, "_ORDER_IN_THREAD_ROWS", 1)
    started, release = threading.Event(), threading.Event()
    real = fstree._recursive_rows

    def _held(plan):
        started.set()
        release.wait(10)
        return real(plan)
    monkeypatch.setattr(fstree, "_recursive_rows", _held)
    folder = f"{local}/Box"
    pending = asyncio.ensure_future(_list(folder))
    assert await asyncio.to_thread(started.wait, 10)
    s = store_mod._store
    s.update_track_fields_batch([(tid, {"album": "NEW"}) for tid in list(s._tracks)])
    fstree.invalidate_browse_cache(drop_disk=False)
    release.set()
    old = _rows(await pending)
    assert {r.get("album") for r in old} == {""}                   # (built before the drop)
    monkeypatch.setattr(fstree, "_recursive_rows", real)
    now = _rows(await _list(folder))
    assert {r.get("album") for r in now} == {"NEW"}
    assert _rel(now, folder + "/") == BOX_ORDER
