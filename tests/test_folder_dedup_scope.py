# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Folder listings hide a duplicate copy only next to a better copy in the SAME
folder ("Hide duplicates when browsing folders"): a copy whose better copies
are in other folders or shares is listed, playable copies before ones that
can't play, and two files of one format and score are both listed."""
from __future__ import annotations

from soniqboom.api import fstree
from soniqboom.core.data import path_hash
from soniqboom.core.store import TrackStore


def _row(n: int, path: str, fmt: str = "FLAC", *, group: str | None = "g",
         primary: bool = True, bitrate: int | None = None) -> dict:
    return {"id": f"t{n:04d}", "path": path, "title": f"Song {n}", "format": fmt,
            "bitrate": bitrate, "added_at": 1_700_000_000 + n, "duration": 200.0,
            "duplicate_group_id": group, "is_duplicate_primary": primary}


def _ids(rows):
    return [r["id"] for r in rows]


def test_a_copy_whose_primary_is_in_another_folder_is_listed():
    # The FLAC album folder: every track's primary is an ALAC copy in another share.
    rows = [_row(i, f"ftp://nas/Music FLAC:/BOWIE/Hunky Dory/{i:02d}.flac",
                 group=f"g{i}", primary=False) for i in range(5)]
    assert _ids(fstree._drop_duplicate_alternates(rows)) == _ids(rows)


def test_encodings_side_by_side_keep_the_best():
    rows = [_row(1, "/m/Album/01 Song.mp3", "MP3", primary=True, bitrate=320_000),
            _row(2, "/m/Album/01 Song.flac", "FLAC", primary=False),
            _row(3, "/m/Album/02 Other.mp3", "MP3", group=None)]
    # Quality decides, not the library-wide (or hand-picked) primary flag.
    assert _ids(fstree._drop_duplicate_alternates(rows)) == ["t0002", "t0003"]


def test_a_better_copy_beside_a_low_primary_is_the_one_listed():
    rows = [_row(1, "/m/A/x.mp3", "MP3", primary=True, bitrate=128_000),
            _row(2, "/m/A/x (hq).mp3", "MP3", primary=False, bitrate=320_000)]
    assert _ids(fstree._drop_duplicate_alternates(rows)) == ["t0002"]


def test_a_copy_that_cannot_play_is_left_out_beside_one_that_can():
    rows = [_row(1, "/m/A/x.flac", "FLAC"),
            _row(2, "/m/A/x.mp3", "MP3", bitrate=128_000, primary=False),
            _row(3, "/m/A/x2.mp3", "MP3", bitrate=320_000, primary=False)]
    skip = frozenset({"t0001", "t0003"})
    assert _ids(fstree._drop_duplicate_alternates(rows, skip)) == ["t0002"]
    # Every copy can't play: the best one is still listed.
    assert _ids(fstree._drop_duplicate_alternates(rows, frozenset({"t0001", "t0002", "t0003"}))
                ) == ["t0001"]


def test_members_of_two_archives_in_one_folder_are_both_listed():
    rows = [_row(1, "/m/A/a.zip::tune.mod", "MOD"),
            _row(2, "/m/A/b.lha::tune.xm", "XM", primary=False)]
    assert _ids(fstree._drop_duplicate_alternates(rows)) == ["t0001", "t0002"]
    rows.append(_row(3, "/m/A/a.zip::tune.flac", "FLAC", primary=False))
    assert _ids(fstree._drop_duplicate_alternates(rows)) == ["t0002", "t0003"]


def test_a_lower_bitrate_copy_of_one_format_is_left_out():
    rows = [_row(1, "/m/A/x.mp3", "MP3", primary=False, bitrate=128_000),
            _row(2, "/m/A/x (hq).mp3", "MP3", primary=False, bitrate=320_000)]
    assert _ids(fstree._drop_duplicate_alternates(rows)) == ["t0002"]


def test_two_files_of_one_format_and_score_are_both_listed():
    # An album that holds a track twice (16 and 17), both FLAC.
    rows = [_row(16, "/m/OST/16. Warcry.flac", primary=True),
            _row(17, "/m/OST/17. Warcry.flac", primary=False)]
    assert _ids(fstree._drop_duplicate_alternates(rows)) == ["t0016", "t0017"]


def test_copies_in_two_album_folders_of_a_recursive_listing_are_both_listed():
    rows = [_row(1, "/m/DM/CD14 Spirit/01 Going backwards.flac", primary=True),
            _row(2, "/m/DM/Spirit (Deluxe)/CD1/01 Going Backwards.flac", primary=False)]
    assert _ids(fstree._drop_duplicate_alternates(rows)) == ["t0001", "t0002"]


def test_a_playable_copy_beats_one_that_cannot_play():
    rows = [_row(1, "/m/A/x.flac", "FLAC", primary=True),
            _row(2, "/m/A/x.mp3", "MP3", primary=False, bitrate=320_000)]
    assert _ids(fstree._drop_duplicate_alternates(rows)) == ["t0001"]
    assert _ids(fstree._drop_duplicate_alternates(rows, frozenset({"t0001"}))) == ["t0002"]


def test_the_memo_follows_what_can_play(monkeypatch):
    # A listing big enough to be memoised; the pair's FLAC goes offline, comes back.
    n = fstree._LISTING_MEMO_MIN_ROWS + 10
    rows = [_row(i, f"/m/F/{i:05d}.mp3", "MP3", group=None) for i in range(n)]
    rows += [_row(90001, "/m/F/pair.flac", "FLAC", group="p", primary=True),
             _row(90002, "/m/F/pair.mp3", "MP3", group="p", primary=False, bitrate=320_000)]
    fstree._DEDUP_MEMO.clear()
    state = {"skip": None}
    monkeypatch.setattr("soniqboom.core.availability.unplayable", lambda store=None: state["skip"])
    first = fstree._deduped(rows)
    assert "t90001" in _ids(first) and "t90002" not in _ids(first)
    assert fstree._deduped(rows) is first                       # warm: the same list
    state["skip"] = frozenset({"t90001"})
    swapped = fstree._deduped(rows)
    assert "t90002" in _ids(swapped) and "t90001" not in _ids(swapped)
    assert fstree._deduped(rows) is swapped
    state["skip"] = frozenset({"t90001", "t00001"})      # a new set, same contested ids
    assert fstree._deduped(rows) is swapped
    state["skip"] = None
    back = fstree._deduped(rows)
    assert "t90001" in _ids(back) and "t90002" not in _ids(back)


async def test_folder_endpoint_lists_copies_whose_primary_is_elsewhere(monkeypatch, tmp_path):
    """Through ``tracks_with_meta`` (store fast path, recursive and direct): an
    album whose every track has its primary in another folder lists in full,
    with the setting on."""
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    monkeypatch.setattr(fstree, "_STORE_RECURSIVE_CACHE", {}, raising=False)
    root = str(tmp_path.resolve())
    (tmp_path / "flac" / "Album").mkdir(parents=True)
    (tmp_path / "alac").mkdir()
    store.upsert_scan_dir(root)
    rh = path_hash(root)
    for i in range(6):
        store.upsert_track({**_row(i, f"{root}/flac/Album/{i:02d}.flac", group=f"g{i}",
                                   primary=False),
                            "scan_root_hash": rh,
                            "dir_hash": path_hash(f"{root}/flac/Album")})
        store.upsert_track({**_row(100 + i, f"{root}/alac/{i:02d}.m4a", "ALAC",
                                   group=f"g{i}", primary=True),
                            "scan_root_hash": rh, "dir_hash": path_hash(f"{root}/alac")})
    for path, recursive in ((f"{root}/flac", True), (f"{root}/flac/Album", False)):
        res = await fstree.tracks_with_meta(path=path, recursive=recursive, offset=0,
                                            limit=100, filter_duplicates=True,
                                            shuffle_seed=None)
        assert res["total"] == 6, (path, recursive, res["total"])
