# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one-time repair of network-share bitrates a scan's front window set
(``repair.repair_window_bitrates_once``): a FLAC stored at the window's
size over its length gets the whole file's; local files, lossy formats and
rows already right are left alone; it runs once per version."""
from __future__ import annotations

from soniqboom.core import repair
from soniqboom.core.store import TrackStore

SIZE, LENGTH = 25_000_000, 200.0
RIGHT = int(SIZE * 8 / LENGTH)                        # 1 000 000 b/s
WINDOW = int(1536 * 1024 * 8 / LENGTH)                # what the 1.5 MB window gave


def _t(n: int, path: str, fmt: str = "FLAC", bitrate: int | None = WINDOW, **kw) -> dict:
    return {"id": f"t{n:03d}", "path": path, "title": f"Song {n}", "artist": "A",
            "format": fmt, "bitrate": bitrate, "file_size": SIZE, "duration": LENGTH,
            "added_at": 1_700_000_000 + n, **kw}


async def test_share_rows_set_by_a_window_get_the_whole_files_bitrate():
    store = TrackStore()
    rows = [
        _t(1, "ftp://nas/Music FLAC:/A/01.flac"),                         # fixed
        _t(2, "ftp://nas/Music FLAC:/A/02.flac", bitrate=RIGHT),          # already right
        _t(3, "/local/A/03.flac"),                                         # local: left
        _t(4, "ftp://nas/Music:/A/04.mp3", "MP3", bitrate=128_000),        # lossy: left
        _t(5, "smb://nas/share:/A/05.flac", bitrate=None),                 # no bitrate: fixed
        _t(6, "ftp://nas/Music FLAC:/A/06.flac", file_size=0),             # no size: left
    ]
    for r in rows:
        store.upsert_track(r)
    res = await repair.repair_window_bitrates_once(store)
    assert res["repaired"] == 2
    got = {tid: store._tracks[tid]["bitrate"] for tid in store._tracks}
    assert got == {"t001": RIGHT, "t002": RIGHT, "t003": WINDOW, "t004": 128_000,
                   "t005": RIGHT, "t006": WINDOW}
    assert await repair.repair_window_bitrates_once(store) == {"skipped": True}
    store.upsert_track(_t(7, "ftp://nas/Music FLAC:/A/07.flac"))         # (a later bad row)
    assert (await repair.repair_window_bitrates_once(store, force=True))["repaired"] == 1


async def test_the_repaired_rows_are_marked_for_duplicate_regrouping():
    store = TrackStore()
    store.upsert_track(_t(1, "ftp://nas/Music FLAC:/A/01.flac"))
    store._dup_dirty.clear()
    store._dup_dirty_overflow = False
    await repair.repair_window_bitrates_once(store)
    assert "t001" in store._dup_dirty or store._dup_dirty_overflow


def test_with_the_whole_files_bitrate_flac_ranks_by_its_real_rate():
    from soniqboom.core.duplicates import primary_sort_key
    alac = {"id": "a", "format": "ALAC", "bitrate": 883_156, "added_at": 1, "path": "/x.m4a"}
    flac_window = {"id": "f", "format": "FLAC", "bitrate": 61_998, "added_at": 2,
                   "path": "/x.flac"}
    flac_whole = {**flac_window, "bitrate": 900_000}
    assert min((alac, flac_window), key=primary_sort_key)["id"] == "a"
    assert min((alac, flac_whole), key=primary_sort_key)["id"] == "f"
