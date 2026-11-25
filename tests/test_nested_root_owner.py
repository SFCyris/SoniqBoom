# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Nested scan roots (``Music`` and ``Music/Box/CD2`` both registered): a file
belongs to the innermost registered root holding it (Sebastian, 2026-11-24).

A track has one id under both roots, so it carries ONE ``scan_root_hash``.
Before, each root's scan counted only its own tracks as indexed and read the
other root's files again, re-tagging them: every scan of both roots swapped
the inner folder's tags and reported a change.  Now each root's walk skips
the roots inside it, a full scan first hands misplaced tracks to their owner
(no file read), a watcher's change goes to its owner's scan, a full scan of
a root also scans the roots inside it, and a folder click refreshes with the
folder's own root.  Real scans of generated files."""
from __future__ import annotations

import asyncio
import logging
import math
import struct
import wave
from collections import OrderedDict
from pathlib import Path

import pytest

from soniqboom.api import fstree
from soniqboom.core import availability, removals, scanner
from soniqboom.core import store as store_mod
from soniqboom.core.data import path_hash
from soniqboom.core.store import TrackStore

FILES = {"Box/CD1/a.wav": 300, "Box/CD2/b.wav": 360, "Box/CD2/Bonus/c.wav": 420,
         "Box/CD3/d.wav": 480}
INNER_FILES = {"Box/CD2/b.wav", "Box/CD2/Bonus/c.wav"}


def make_wav(path: Path, freq: float, secs: float = 1.0, rate: int = 8000) -> None:
    n = int(secs * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
                               for i in range(n)))


@pytest.fixture
def lib(tmp_path, monkeypatch, tmp_data_dir):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())
    monkeypatch.setattr(scanner, "_scan_queue", [])
    monkeypatch.setattr(scanner, "_scan_task", None)
    monkeypatch.setattr(scanner, "_current_scan_dirs", frozenset())

    async def _no_full_dup():
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _no_full_dup)
    for name in ("_TRACKS_META_CACHE", "_SCAN_ROOT_FULL_CACHE", "_STORE_RECURSIVE_CACHE",
                 "_DEDUP_MEMO", "_DIRECT_MEMO", "_BYID_MEMO", "_CAT_SORTED", "_ROOT_GEN"):
        monkeypatch.setattr(fstree, name, {})
    monkeypatch.setattr(fstree, "_NAT_LISTINGS", OrderedDict())
    removals.reset_for_tests()
    availability.reset_for_tests()
    outer = (tmp_path / "lib" / "Music")
    for rel, freq in FILES.items():
        f = outer / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        make_wav(f, freq)
    outer = outer.resolve()
    inner = outer / "Box" / "CD2"
    s.upsert_scan_dir(str(outer))
    s.upsert_scan_dir(str(inner))
    s.paths = {"outer": str(outer), "inner": str(inner)}
    yield s
    removals.reset_for_tests()
    availability.reset_for_tests()


def _tags(s) -> dict:
    """{path relative to the outer root: "outer" | "inner" | "?"}"""
    names = {path_hash(s.paths["outer"]): "outer", path_hash(s.paths["inner"]): "inner"}
    base = s.paths["outer"] + "/"
    return {t["path"][len(base):]: names.get(t["scan_root_hash"], "?") for t in s._tracks.values()}


def _owners() -> dict:
    return {rel: ("inner" if rel in INNER_FILES else "outer") for rel in FILES}


def _extracted(caplog) -> dict:
    """{root: files sent to extraction} from the scanner's per-root summary."""
    out: dict = {}
    for r in caplog.records:
        if r.name == "soniqboom.core.scanner" and r.msg.startswith("Extraction starting for"):
            out[str(r.args[0])] = out.get(str(r.args[0]), 0) + int(r.args[1])
    return out


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.INFO, logger="soniqboom.core.scanner")
    return caplog


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["outer first", "inner first"])
async def test_a_first_scan_of_both_roots_gives_each_file_its_innermost_root(lib, logs, order):
    roots = [lib.paths["outer"], lib.paths["inner"]]
    await scanner._run_scan(roots if order == "outer first" else roots[::-1])
    assert _tags(lib) == _owners()
    assert lib.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_scanning_both_roots_again_reads_nothing_and_changes_nothing(lib, logs):
    both = [lib.paths["outer"], lib.paths["inner"]]
    await scanner._run_scan(both)
    for _ in range(2):
        logs.clear()
        assert await scanner._run_scan(both) is False
        assert not any(_extracted(logs).values()) and _tags(lib) == _owners()


@pytest.mark.asyncio
async def test_a_scan_of_one_root_leaves_the_other_roots_files_alone(lib, logs):
    await scanner._run_scan([lib.paths["outer"], lib.paths["inner"]])
    for root in (lib.paths["outer"], lib.paths["inner"]):
        logs.clear()
        assert await scanner._run_scan([root]) is False
        assert not any(_extracted(logs).values()) and _tags(lib) == _owners()


@pytest.mark.asyncio
async def test_tags_older_scans_left_are_handed_over_without_reading_the_files(lib, logs):
    # The swapped tags a library has from before: the inner folder's files
    # tagged with the outer root.  The next full scan fixes them, reading none.
    await scanner._run_scan([lib.paths["outer"], lib.paths["inner"]])
    oh = path_hash(lib.paths["outer"])
    lib.update_track_fields_batch([(t["id"], {"scan_root_hash": oh}) for t in lib._tracks.values()])
    assert set(_tags(lib).values()) == {"outer"}
    logs.clear()
    await scanner._run_scan([lib.paths["outer"], lib.paths["inner"]])
    assert not any(_extracted(logs).values()) and _tags(lib) == _owners()
    assert lib.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_adding_an_inner_root_takes_its_files_over_without_reading_them(lib, logs):
    inner = lib.paths["inner"]
    lib.delete_scan_dir(inner)
    await scanner._run_scan([lib.paths["outer"]])          # (only the outer root: all its)
    assert set(_tags(lib).values()) == {"outer"}
    lib.upsert_scan_dir(inner)                              # (Settings → add folder)
    logs.clear()
    await scanner._run_scan([inner])
    assert not any(_extracted(logs).values()) and _tags(lib) == _owners()


@pytest.mark.asyncio
async def test_a_change_under_the_inner_root_goes_to_that_roots_scan(lib, logs):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    b = Path(inner) / "b.wav"
    make_wav(b, 360, secs=3.0)                              # (really changed)
    logs.clear()
    # Only the outer root's watch reported it (both usually do).
    await scanner.start_scan([], scope={outer: [str(b)]})
    await scanner._scan_task
    assert _extracted(logs) == {inner: 1}
    assert _tags(lib) == _owners()
    tid = next(t["id"] for t in lib._tracks.values() if t["path"] == str(b))
    assert round(lib._tracks[tid]["duration"]) == 3


@pytest.mark.asyncio
async def test_a_full_scan_of_a_root_also_scans_the_roots_inside_it(lib):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner.start_scan([outer])
    assert scanner._scan_queue[0][0] == frozenset({outer, inner})
    await scanner._scan_task
    assert _tags(lib) == _owners()


@pytest.mark.asyncio
async def test_a_folder_click_refreshes_with_the_folders_own_root(lib, monkeypatch):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    read: list = []
    real = scanner._extract_one

    def _counted(p):
        read.append(str(p))
        return real(p)
    monkeypatch.setattr(scanner, "_extract_one", _counted)
    # The client browses the outer root's tree: inside the inner folder …
    await scanner.refresh_subtree_under_root(outer, str(Path(inner) / "Bonus"))
    # … and above it (the inner root is refreshed on its own).
    await scanner.refresh_subtree_under_root(outer, str(Path(outer) / "Box"))
    assert read == [] and _tags(lib) == _owners()
    make_wav(Path(inner) / "Bonus" / "e.wav", 540)          # (a new file)
    res = await scanner.refresh_subtree_under_root(outer, str(Path(outer) / "Box"))
    assert res["added"] == 1 and _tags(lib)["Box/CD2/Bonus/e.wav"] == "inner"


@pytest.mark.asyncio
async def test_a_re_tag_in_a_commit_moves_the_row_between_listings(lib, monkeypatch):
    # A changed file still tagged with the outer root, which the hand-over
    # before the scan missed (a removal undone while the scan ran): its
    # root's scan re-reads it, and the commit moves its row from the outer
    # root's listing to the inner one's — no browse cache is dropped.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])

    async def _missed(*_a, **_k):
        return None
    monkeypatch.setattr(scanner, "_release_under", _missed)
    b = Path(inner) / "b.wav"
    tid = next(t["id"] for t in lib._tracks.values() if t["path"] == str(b))
    oh, ih = path_hash(outer), path_hash(inner)
    lib.update_track_fields_batch([(tid, {"scan_root_hash": oh})])
    for h in (oh, ih):
        fstree._get_or_build_scan_root_sorted(lib, h)       # (both listings in use)
    from soniqboom.core import folder_album
    dropped: list = []
    monkeypatch.setattr(folder_album, "invalidate_album_caches", lambda: dropped.append(1))
    reshaped: list = []
    real_reshape = folder_album.refresh_album_caches

    async def _reshape(ids):
        reshaped.extend(ids)
        await real_reshape(ids)
    monkeypatch.setattr(folder_album, "refresh_album_caches", _reshape)
    windows: list = []
    real_commit = fstree.root_commit

    class _seen(real_commit):
        def __init__(self, hashes):
            windows.append(set(hashes))
            super().__init__(hashes)
    monkeypatch.setattr(fstree, "root_commit", _seen)
    invalidated: list = []
    real_inv = fstree.invalidate_scan_root

    def _inv(h):
        invalidated.append(h)
        return real_inv(h)
    monkeypatch.setattr(fstree, "invalidate_scan_root", _inv)
    make_wav(b, 360, secs=2.0)
    assert await scanner._run_scan([inner], scope={inner: frozenset({str(b)})}) is True
    assert lib._tracks[tid]["scan_root_hash"] == ih and not dropped
    assert not invalidated                                  # (patched, not rebuilt)
    for h, has_b in ((oh, False), (ih, True)):              # (each entry in step as it is)
        e = fstree._SCAN_ROOT_FULL_CACHE[h]
        assert (str(b) in e["paths"]) is has_b and e["size"] == len(lib._tag_scan_root_hash[h])
    assert any({oh, ih} <= w for w in windows)               # (both roots in the window)
    o_paths, _ = fstree._get_or_build_scan_root_sorted(lib, oh)
    i_paths, _ = fstree._get_or_build_scan_root_sorted(lib, ih)
    assert str(b) not in o_paths and str(b) in i_paths
    assert tid in reshaped                                  # (aggregations, metadata listings)


@pytest.mark.asyncio
async def test_a_removed_inner_roots_files_go_back_to_the_outer_root_unread(lib, logs, monkeypatch):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    from soniqboom.core.data import delete_scan_dir
    assert await delete_scan_dir(inner)                     # (removed, tracks kept)
    while removals._side:
        await asyncio.gather(*list(removals._side), return_exceptions=True)
    assert set(_tags(lib).values()) == {"outer"}
    logs.clear()
    assert await scanner._run_scan([outer]) is False
    assert not any(_extracted(logs).values())


def _swap_all_to_outer(s) -> None:
    """The tags a library has from before the owner rule: every track tagged
    with the outer root, the inner folder's too."""
    oh = path_hash(s.paths["outer"])
    s.update_track_fields_batch([(t["id"], {"scan_root_hash": oh}) for t in s._tracks.values()])
    assert set(_tags(s).values()) == {"outer"}


def _count_reads(monkeypatch) -> list:
    read: list = []
    real = scanner._extract_one

    def _counted(p, *a, **k):
        read.append(str(p))
        return real(p, *a, **k)
    monkeypatch.setattr(scanner, "_extract_one", _counted)
    return read


async def _drain() -> None:
    if scanner._scan_task is not None:
        await scanner._scan_task


async def _settle_removals(store, rounds: int = 300) -> None:
    for _ in range(rounds):
        if not removals.registry(store) and not removals.tasks():
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_scan_in_an_inner_roots_undo_window_keeps_the_tracks_it_handed_over(lib, logs):
    # Removing the inner root hands its tracks to the outer one first; until
    # the removal is final they are the outer root's — its scan lists them.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    ids0 = set(lib._tracks)
    assert await removals.begin(inner, store=lib)
    assert set(_tags(lib).values()) == {"outer"}
    for run in (lambda: scanner._run_scan([outer]), lambda: scanner._run_scan([outer], light=True)):
        logs.clear()
        assert await run() is False
        assert set(lib._tracks) == ids0 and not any(_extracted(logs).values())
        assert set(_tags(lib).values()) == {"outer"}


@pytest.mark.asyncio
async def test_a_change_in_an_inner_folder_being_removed_goes_to_the_outer_root(lib, logs):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    assert await removals.begin(inner, store=lib)
    b = Path(inner) / "b.wav"
    make_wav(b, 360, secs=3.0)                              # (really changed)
    logs.clear()
    await scanner.start_scan([], scope={inner: [str(b)]})   # (the inner root's own watch)
    await scanner._scan_task
    assert _extracted(logs) == {outer: 1}
    assert _tags(lib)["Box/CD2/b.wav"] == "outer"


@pytest.mark.asyncio
async def test_old_tags_under_a_changed_parent_folder_are_handed_over_not_deleted(lib, logs):
    # A watcher change on a folder holding the inner root, before any full
    # scan fixed the old tags: the outer root's walk skips the inner folder,
    # which must not make its tracks look deleted.
    outer = lib.paths["outer"]
    await scanner._run_scan([outer, lib.paths["inner"]])
    _swap_all_to_outer(lib)
    ids0 = set(lib._tracks)
    logs.clear()
    await scanner.start_scan([], scope={outer: [str(Path(outer) / "Box")]})
    await scanner._scan_task
    assert set(lib._tracks) == ids0
    assert not any(_extracted(logs).values()) and _tags(lib) == _owners()


@pytest.mark.asyncio
async def test_a_folder_click_over_old_tags_hands_them_over_unread(lib, monkeypatch):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    added0 = {tid: t.get("added_at") for tid, t in lib._tracks.items()}
    for sub in (str(Path(inner) / "Bonus"), str(Path(outer) / "Box")):
        _swap_all_to_outer(lib)
        read = _count_reads(monkeypatch)
        res = await scanner.refresh_subtree_under_root(outer, sub)
        assert read == [] and res["removed"] == 0 and res["added"] == 0, sub
        assert {tid: t.get("added_at") for tid, t in lib._tracks.items()} == added0
        if sub.endswith("Box"):
            assert _tags(lib) == _owners()
        else:
            assert _tags(lib)["Box/CD2/Bonus/c.wav"] == "inner"


@pytest.mark.asyncio
async def test_a_removal_begun_while_the_outer_root_walks_deletes_nothing(lib, monkeypatch):
    # The outer root's walk skipped the inner folder; the inner root's removal
    # then hands its tracks to the outer root before the outer root's stale
    # check: they are not deleted as gone.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    ids0 = set(lib._tracks)
    loop = asyncio.get_running_loop()
    real = scanner._find_audio_files

    def _walk_then_remove(*a, **k):
        out = real(*a, **k)
        asyncio.run_coroutine_threadsafe(removals.begin(inner, store=lib), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", _walk_then_remove)
    await scanner._run_scan([outer])
    assert set(lib._tracks) == ids0
    assert set(_tags(lib).values()) == {"outer"}


@pytest.mark.asyncio
async def test_a_folder_click_never_deletes_the_tracks_of_a_skipped_inner_folder(lib, monkeypatch):
    # Old tags the hand-over before the click missed: the outer root's pass
    # skips the inner folder and must not count its tracks as vanished.
    outer = lib.paths["outer"]
    await scanner._run_scan([outer, lib.paths["inner"]])
    added0 = {tid: t.get("added_at") for tid, t in lib._tracks.items()}
    _swap_all_to_outer(lib)

    async def _missed(*_a, **_k):
        return None
    monkeypatch.setattr(scanner, "_release_under", _missed)
    res = await scanner.refresh_subtree_under_root(outer, str(Path(outer) / "Box"))
    assert res["removed"] == 0
    assert {tid: t.get("added_at") for tid, t in lib._tracks.items()} == added0
    assert _tags(lib) == _owners()


def _zip_wav(zpath: Path, member: str, freq: float) -> None:
    import zipfile
    tmp = zpath.with_suffix(".tmp.wav")
    make_wav(tmp, freq)
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(tmp, member)
    tmp.unlink()


@pytest.mark.asyncio
async def test_archive_members_with_old_tags_are_handed_over_unread(lib, logs, monkeypatch):
    # The hand-over finds the tracks in the inner folder through the folder
    # index (archive members too), not by a pass over every outer track.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    _zip_wav(Path(inner) / "Bonus" / "pack.zip", "z1.wav", 600)
    _zip_wav(Path(outer) / "Box" / "CD1" / "pack.zip", "z2.wav", 660)
    await scanner._run_scan([outer, inner])
    want = _tags(lib)
    assert want["Box/CD2/Bonus/pack.zip::z1.wav"] == "inner"
    assert want["Box/CD1/pack.zip::z2.wav"] == "outer"
    from soniqboom.core import data
    calls: list = []
    real = data.hand_over_covered

    async def _spy(store, root, exclude=(), **kw):
        calls.append(kw.get("only"))
        return await real(store, root, exclude, **kw)
    monkeypatch.setattr(data, "hand_over_covered", _spy)
    for scan in (lambda: scanner._run_scan([outer, inner]),
                 lambda: scanner._run_scan([outer], light=True)):
        _swap_all_to_outer(lib)
        logs.clear()
        await scan()
        assert not any(_extracted(logs).values()) and _tags(lib) == want
    assert calls and all(c is not None for c in calls)        # (never a whole-root pass)


@pytest.mark.asyncio
async def test_three_nested_roots_each_keep_their_own_files(lib, logs, monkeypatch):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    mid = str(Path(outer) / "Box")
    lib.upsert_scan_dir(mid)
    names = {path_hash(outer): "outer", path_hash(mid): "mid", path_hash(inner): "inner"}
    base = outer + "/"

    def tags() -> dict:
        return {t["path"][len(base):]: names.get(t["scan_root_hash"], "?")
                for t in lib._tracks.values()}
    want = {"Box/CD1/a.wav": "mid", "Box/CD3/d.wav": "mid", "Box/CD2/b.wav": "inner",
            "Box/CD2/Bonus/c.wav": "inner"}
    await scanner.start_scan([outer])
    assert scanner._scan_queue[0][0] == frozenset({outer, mid, inner})
    await scanner._scan_task
    assert tags() == want
    logs.clear()
    assert await scanner._run_scan([outer, mid, inner]) is False
    assert not any(_extracted(logs).values())
    _swap_all_to_outer(lib)                                 # (older tags: one full scan)
    logs.clear()
    await scanner.start_scan([outer])
    await scanner._scan_task
    assert not any(_extracted(logs).values()) and tags() == want
    read = _count_reads(monkeypatch)
    await scanner.refresh_subtree_under_root(outer, outer)
    assert read == [] and tags() == want
    b = Path(inner) / "b.wav"
    make_wav(b, 360, secs=2.0)
    logs.clear()
    await scanner.start_scan([], scope={outer: [str(b)]})   # (reported by the outer watch)
    await scanner._scan_task
    assert _extracted(logs) == {inner: 1} and tags() == want


@pytest.mark.asyncio
async def test_a_hand_over_tells_dlna_though_the_scan_reports_no_change(lib, monkeypatch):
    # Tracks moving between roots change the per-root containers of DLNA
    # (and Subsonic's music folders), not the library's content: the scan
    # reports no change (no library-wide post-scan passes) but DLNA is told.
    inner = lib.paths["inner"]
    lib.delete_scan_dir(inner)
    await scanner._run_scan([lib.paths["outer"]])
    lib.upsert_scan_dir(inner)
    from soniqboom.api import dlna_upnp
    told: list = []
    monkeypatch.setattr(dlna_upnp, "notify_library_changed", lambda: told.append(1))
    seq0 = lib._catalog_seq
    assert await scanner._run_scan([inner]) is False
    assert told and lib._catalog_seq > seq0 and _tags(lib) == _owners()
    told.clear()
    assert await scanner._run_scan([lib.paths["outer"], inner]) is False
    assert not told                                         # (nothing moved: nobody told)


@pytest.mark.asyncio
async def test_what_the_outer_root_gave_up_in_the_inner_folder_is_forgotten(lib):
    # A file the outer root's scan gave up on before the inner root owned it:
    # the outer root's walk never lists it again, so its record would never
    # settle — the outer root's scan drops it.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    import time as _time
    b = Path(inner) / "b.wav"
    e = Path(outer) / "Box" / "CD1" / "e.wav"               # (new: given up, still waiting)
    make_wav(e, 540)
    key = scanner._GIVEUP_KEY + path_hash(outer)
    lib.set_config(key, {str(f): [f.stat().st_mtime, f.stat().st_size, "crash", 1, _time.time()]
                         for f in (b, e)})
    await scanner._run_scan([outer], light=True)            # (automatic: e still waits)
    assert set(lib.get_config(key) or {}) == {str(e)}


@pytest.mark.asyncio
async def test_the_folder_index_built_in_slices_matches_one_built_at_once(monkeypatch):
    # ``_FolderPaths.sync_sliced`` (the hand-over's lookup) builds a big index
    # in slices of the loop.  Folders added and removed meanwhile, and another
    # ``sync`` in between (it removes by binary search: the list must be
    # sorted at every yield), leave it equal to an index built in one go.
    from soniqboom.core import modland_catalog
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)        # (a yield after each folder)
    s = TrackStore()

    def rows(lo: int, hi: int) -> list:                          # (last path first)
        return [{"id": f"t{i}", "path": f"/L/{i:05d}/x.flac", "title": "t", "duration": 1.0,
                 "dir_hash": path_hash(f"/L/{i:05d}"), "scan_root_hash": path_hash("/L")}
                for i in range(hi - 1, lo - 1, -1)]
    s.upsert_tracks_batch(rows(0, 2600))
    fp = scanner._FolderPaths()
    yields = 0
    building = {"on": False}
    during: list = []

    async def meanwhile() -> None:
        nonlocal yields
        for k in range(60):
            await asyncio.sleep(0)
            yields += 1
            if k == 5:
                during.append(building["on"])                    # (the build still running)
                s.delete_track_ids(["t2599"])                    # (a folder the build took)
            if k == 10:
                s.upsert_tracks_batch(rows(2600, 2620))          # (new folders)
            if k == 20:
                s.delete_track_ids([f"t{i}" for i in range(0, 2599, 100)])  # (folders gone)
            if k in (6, 14, 21, 28):
                fp.sync(s)                                       # (another user meanwhile)
    task = asyncio.ensure_future(meanwhile())
    building["on"] = True
    await fp.sync_sliced(s)
    building["on"] = False
    await task
    want = scanner._FolderPaths()
    want.sync(s)
    assert during == [True] and yields > 20
    assert fp.sync(s) == want.sorted and len(want.sorted) == 2600 - 27 + 20


# ── Removals racing scans, routing gaps, folder names (second review) ──────

def _nonzero(counts: dict) -> dict:
    return {k: v for k, v in counts.items() if v}


def _dur(s, p) -> "int | None":
    t = next((t for t in s._tracks.values() if t["path"] == str(p)), None)
    return None if t is None else round(t["duration"])


async def _hold_scans(monkeypatch):
    """Another scan running: requests queue until ``_let_scans_run``."""
    gate = asyncio.Event()
    busy = asyncio.create_task(gate.wait())
    monkeypatch.setattr(scanner, "_scan_task", busy)
    return gate, busy


async def _let_scans_run(monkeypatch, gate, busy) -> None:
    gate.set()
    await busy
    monkeypatch.setattr(scanner, "_scan_task", None)
    await scanner._drain_scan_queue()


def _discovered(caplog) -> dict:
    """{root: files its walk listed} from the walk's log line."""
    out: dict = {}
    for r in caplog.records:
        if r.name == "soniqboom.core.scanner" and r.msg.startswith("Discovered %d audio files in"):
            out[str(r.args[1])] = int(r.args[0])
    return out


def test_the_path_checks_are_per_component():
    assert scanner._strictly_below("/a/CD2/x", "/a/CD2")
    assert not scanner._strictly_below("/a/CD2 Extra", "/a/CD2")
    assert not scanner._strictly_below("/a/CD2", "/a/CD2")
    assert scanner._strictly_below("/a", "/")
    assert scanner._inside_any("/a/CD2/x.zip::m.mod", ["/a/CD2"])
    assert not scanner._inside_any("/a/CD2 Extra/x.wav", ["/a/CD2"])
    assert scanner._innermost_root("/a/CD2/x.wav", ["/a", "/a/CD2", "/a/CD2 Extra"]) == "/a/CD2"
    assert scanner._innermost_root("/a/CD2 Extra/x.wav", ["/a", "/a/CD2"]) == "/a"
    assert scanner._innermost_root("/b/x.wav", ["/a"]) is None
    assert scanner._innermost_root("/b/x.wav", ["/", "/a"]) == "/"
    roots = sorted(["/a/CD2", "/a/CD2 Extra", "/a/CD2/Bonus", "/a/CD3"])
    assert scanner._sorted_roots_inside("/a/CD2", roots) == ["/a/CD2/Bonus"]
    assert scanner._roots_pruned_below("/a", {"/a", "/a/CD2", "/b"}) == ["/a/CD2"]


@pytest.mark.asyncio
async def test_a_folder_named_like_the_inner_root_is_not_inside_it(lib, logs, monkeypatch):
    # "Box/CD2 Extra" starts with "Box/CD2" but is not inside it: every check
    # is per path component.  As a plain folder it is the outer root's (a
    # deleted file in it pruned, the other kept); as a root of its own, no
    # scan or click reads anything again.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    sib = Path(outer) / "Box" / "CD2 Extra"
    sib.mkdir()
    make_wav(sib / "x.wav", 700)
    make_wav(sib / "y.wav", 710)
    await scanner.start_scan([outer])
    await _drain()
    t = _tags(lib)
    assert t["Box/CD2 Extra/x.wav"] == "outer" and t["Box/CD2 Extra/y.wav"] == "outer"
    (sib / "y.wav").unlink()
    await scanner.start_scan([outer])
    await _drain()
    t = _tags(lib)
    assert "Box/CD2 Extra/x.wav" in t and "Box/CD2 Extra/y.wav" not in t
    lib.upsert_scan_dir(str(sib))                           # (a root of its own now)
    names = {path_hash(outer): "outer", path_hash(inner): "inner", path_hash(str(sib)): "sib"}
    base = outer + "/"

    def tags() -> dict:
        return {t["path"][len(base):]: names.get(t["scan_root_hash"], "?")
                for t in lib._tracks.values()}
    await scanner.start_scan([outer])
    await _drain()
    want = {**_owners(), "Box/CD2 Extra/x.wav": "sib"}
    assert tags() == want
    ids0 = set(lib._tracks)
    read = _count_reads(monkeypatch)
    logs.clear()
    for scope in (None, {outer: [str(Path(inner) / "b.wav")], inner: [str(Path(inner) / "b.wav")]},
                  {outer: [str(Path(outer) / "Box")]}, {outer: [str(sib / "x.wav")]}):
        if scope is None:
            await scanner.start_scan([outer], light=True)
        else:
            await scanner.start_scan([], scope=scope)
        await _drain()
    for sub in (outer, str(Path(outer) / "Box"), inner, str(sib)):
        await scanner.refresh_subtree_under_root(outer, sub)
    assert read == [] and not any(_extracted(logs).values())
    assert set(lib._tracks) == ids0 and tags() == want


@pytest.mark.asyncio
async def test_a_change_at_a_folder_holding_the_inner_root_reaches_its_files(lib, logs):
    # Only the outer root's watch reports, and only a folder above the inner
    # root (the inner watch down, or a scope widened past 1024 paths): the
    # inner root's folder is its own walk's, so it gets a full, light scan.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    make_wav(Path(inner) / "e.wav", 540)                    # (a new file)
    logs.clear()
    await scanner.start_scan([], scope={outer: [str(Path(outer) / "Box")]})
    await _drain()
    assert _tags(lib).get("Box/CD2/e.wav") == "inner"
    assert _nonzero(_extracted(logs)) == {inner: 1}         # (the rest known: not read)


@pytest.mark.asyncio
async def test_the_inner_roots_own_folder_reported_by_the_outer_watch(lib, logs):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    make_wav(Path(inner) / "new.wav", 640)
    logs.clear()
    await scanner.start_scan([], scope={outer: [inner]})
    await _drain()
    assert _tags(lib).get("Box/CD2/new.wav") == "inner"
    assert _nonzero(_extracted(logs)) == {inner: 1}         # (its full scan: the rest known)


@pytest.mark.asyncio
async def test_an_unchanged_file_with_an_old_tag_reported_by_a_watch_is_not_read(lib, logs):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    _swap_all_to_outer(lib)
    b = str(Path(inner) / "b.wav")                          # (a file, not a folder)
    logs.clear()
    await scanner.start_scan([], scope={outer: [b], inner: [b]})
    await _drain()
    assert not any(_extracted(logs).values()) and _tags(lib)["Box/CD2/b.wav"] == "inner"


@pytest.mark.asyncio
async def test_a_full_scan_of_a_root_being_removed_inside_another_reads_nothing(lib, logs):
    # The inner root's watch overflowing, the fallback poll, a re-armed watch:
    # a full scan of the inner root in its undo window.  Its files are the
    # outer root's then: nothing is read or tagged with it (hidden).
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    assert await removals.begin(inner, store=lib)
    ids0 = set(lib._tracks)
    for run in (lambda: scanner.start_scan([inner], rescan_if_running=True, light=True),
                lambda: scanner._run_scan([inner], light=True)):      # (queued before)
        logs.clear()
        await run()
        await _drain()
        assert not any(_extracted(logs).values()) and set(_tags(lib).values()) == {"outer"}
    hidden, _ = removals.hidden_ids()
    assert not (hidden or set()) & ids0


@pytest.mark.asyncio
async def test_the_outer_root_being_removed_still_leaves_the_inner_roots_files_alone(lib, logs,
                                                                                    monkeypatch):
    # The outer root being removed, only the inner root owns files: the outer
    # root's walk still skips the inner folder (nothing read or tagged with
    # the root being removed).
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    assert await removals.begin(outer, store=lib)
    logs.clear()
    await scanner.start_scan([outer], rescan_if_running=True, light=True)
    await _drain()
    t = _tags(lib)
    assert all(t[r] == "inner" for r in INNER_FILES) and not any(_extracted(logs).values())
    logs.clear()
    await scanner.start_scan([outer])                       # (a manual one walks the disk)
    await _drain()
    assert _discovered(logs).get(outer) == len(FILES) - len(INNER_FILES)   # (CD2 not walked)
    read = _count_reads(monkeypatch)
    await scanner.refresh_subtree_under_root(outer, outer)  # (a click on it)
    t = _tags(lib)
    assert read == [] and all(t[r] == "inner" for r in INNER_FILES)


@pytest.mark.asyncio
async def test_a_removal_begun_during_a_scans_hand_over_hides_nothing(lib, monkeypatch):
    # Old tags being handed to the inner root as the user removes it: the
    # root being removed takes nothing, and its own walk in that scan is
    # skipped (its folder is the outer root's now).
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    _swap_all_to_outer(lib)
    from soniqboom.core import data
    real = data.hand_over_covered
    began: list = []

    async def _meanwhile(store, root, exclude=(), **kw):
        if kw.get("only") is not None and not began:
            began.append(await removals.begin(inner, store=lib))   # (Settings → Remove)
        return await real(store, root, exclude, **kw)
    monkeypatch.setattr(data, "hand_over_covered", _meanwhile)
    ids0 = set(lib._tracks)
    await scanner._run_scan([outer, inner])
    hidden, _ = removals.hidden_ids()
    assert began and not (hidden or set()) & ids0
    assert set(lib._tracks) == ids0 and set(_tags(lib).values()) == {"outer"}


@pytest.mark.asyncio
async def test_a_hand_over_still_running_when_the_purge_starts_deletes_nothing(lib, monkeypatch):
    # The same race carried to the purge: a hand-over of very many old tags
    # outlasting the undo window.  What the root being removed was given
    # after its removal began its purge would delete — so it takes nothing
    # (the hand-over reads its targets again after every yield).
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    for i in range(40):
        make_wav(Path(inner) / "Bonus" / f"n{i:02d}.wav", 200 + i, secs=0.2)
    await scanner._run_scan([outer, inner])
    _swap_all_to_outer(lib)
    ids0 = set(lib._tracks)
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)       # (a yield after every step)
    monkeypatch.setattr(data, "_HAND_OVER_STEP", 1)             # (one track a step)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)                # (the window over at once)
    real = data.hand_over_covered

    async def _scans(store, root, exclude=(), **kw):
        if kw.get("only") is not None and root == outer:
            async def _remove_soon() -> None:
                for _ in range(5):
                    await asyncio.sleep(0)
                await removals.begin(inner, store=lib)
            asyncio.ensure_future(_remove_soon())
        return await real(store, root, exclude, **kw)
    monkeypatch.setattr(data, "hand_over_covered", _scans)
    await scanner._run_scan([outer, inner])
    await _settle_removals(lib)
    assert inner not in lib._scan_dirs and not removals.registry(lib)   # (purged)
    assert set(lib._tracks) == ids0                          # (every file is still there)
    assert set(_tags(lib).values()) == {"outer"}


@pytest.mark.asyncio
async def test_an_undo_during_the_outer_roots_scan_reads_nothing_again(lib, logs, monkeypatch):
    # The inner root's removal undone after the outer root's walk listed its
    # folder (the outer root owned it then): the outer root's pass leaves
    # those files out — the Undo handed their tracks back.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    assert await removals.begin(inner, store=lib)
    loop = asyncio.get_running_loop()
    real = scanner._find_audio_files

    async def _undo() -> None:
        assert removals.undo(inner, lib)

    def _walk_then_undo(*a, **k):
        out = real(*a, **k)
        asyncio.run_coroutine_threadsafe(_undo(), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", _walk_then_undo)
    ids0 = set(lib._tracks)
    logs.clear()
    await scanner._run_scan([outer])
    assert not any(_extracted(logs).values()) and _tags(lib) == _owners()
    assert set(lib._tracks) == ids0


@pytest.mark.asyncio
async def test_a_change_routed_away_then_undone_before_its_scan_runs_is_indexed(lib, logs,
                                                                                 monkeypatch):
    # A change in the inner folder during its removal window goes to the
    # outer root; the removal is undone before that queued scan runs: when
    # it starts, the path is the inner root's again.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    assert await removals.begin(inner, store=lib)
    b = Path(inner) / "b.wav"
    make_wav(b, 360, secs=3.0)                              # (really changed)
    gate = asyncio.Event()
    busy = asyncio.create_task(gate.wait())                 # (another scan running: requests queue)
    monkeypatch.setattr(scanner, "_scan_task", busy)
    await scanner.start_scan([], scope={inner: [str(b)]})   # (the inner root's own watch)
    assert scanner._scan_queue[0][0] == frozenset({outer})  # (routed to the owner then)
    assert removals.undo(inner, lib)
    await asyncio.sleep(0.05)
    gate.set()
    await busy
    monkeypatch.setattr(scanner, "_scan_task", None)
    logs.clear()
    await scanner._drain_scan_queue()
    tid = next(t["id"] for t in lib._tracks.values() if t["path"] == str(b))
    assert round(lib._tracks[tid]["duration"]) == 3 and _tags(lib) == _owners()
    assert _nonzero(_extracted(logs)) == {inner: 1}


@pytest.mark.asyncio
async def test_three_roots_with_the_middle_one_being_removed(lib, logs):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    mid = str(Path(outer) / "Box")
    lib.upsert_scan_dir(mid)
    names = {path_hash(outer): "outer", path_hash(mid): "mid", path_hash(inner): "inner"}
    base = outer + "/"

    def tags() -> dict:
        return {t["path"][len(base):]: names.get(t["scan_root_hash"], "?")
                for t in lib._tracks.values()}
    await scanner.start_scan([outer])
    await _drain()
    assert await removals.begin(mid, store=lib)
    want = {"Box/CD1/a.wav": "outer", "Box/CD3/d.wav": "outer", "Box/CD2/b.wav": "inner",
            "Box/CD2/Bonus/c.wav": "inner"}
    assert tags() == want
    ids0 = set(lib._tracks)
    for old_tags in (False, True):
        for run in (lambda: scanner.start_scan([outer]),
                    lambda: scanner.start_scan([outer], light=True),
                    lambda: scanner.start_scan([], scope={outer: [str(Path(mid) / "CD1")]}),
                    lambda: scanner.start_scan([], scope={mid: [str(Path(mid) / "CD2")]})):
            if old_tags:
                _swap_all_to_outer(lib)
                await scanner.start_scan([outer])           # (handed over first)
                await _drain()
            logs.clear()
            await run()
            await _drain()
            assert not any(_extracted(logs).values()) and set(lib._tracks) == ids0
            assert tags() == want
    res = await scanner.refresh_subtree_under_root(outer, mid)
    assert res["removed"] == 0 and tags() == want
    hidden, _ = removals.hidden_ids()
    assert not (hidden or set()) & ids0


@pytest.mark.asyncio
async def test_a_folder_clicks_passes_share_its_file_budget(lib):
    outer = lib.paths["outer"]
    await scanner._run_scan([outer, lib.paths["inner"]])
    box = str(Path(outer) / "Box")
    for budget, skipped, checked in ((2, True, 2), (3, True, 4), (4, False, 4)):
        # (CD1 + CD3 first; CD2's 2 files fit only in what is left)
        res = await scanner.refresh_subtree_under_root(outer, box, max_files=budget)
        assert (res["skipped"], res["checked"]) == (skipped, checked), budget
    cd3 = str(Path(outer) / "Box" / "CD3")                  # (a second inner root: d.wav)
    lib.upsert_scan_dir(cd3)
    await scanner._run_scan([outer, lib.paths["inner"], cd3])
    for budget, skipped, checked in ((3, True, 3), (4, False, 4)):
        # (CD1, then CD2's 2 files, then CD3's 1 — the passes count together)
        res = await scanner.refresh_subtree_under_root(outer, box, max_files=budget)
        assert (res["skipped"], res["checked"]) == (skipped, checked), budget


@pytest.mark.asyncio
async def test_a_failing_hand_over_never_fails_the_scan_or_the_click(lib, monkeypatch):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])

    async def _boom(*_a, **_k):
        raise RuntimeError("hand-over failed")
    monkeypatch.setattr(scanner, "_release_to_inner_roots", _boom)
    monkeypatch.setattr(scanner, "_release_under", _boom)
    ids0 = set(lib._tracks)
    assert await scanner._run_scan([outer, inner]) is False
    assert await scanner._run_scan([outer], scope={outer: frozenset({str(Path(outer) / "Box")})}) \
        is False
    res = await scanner.refresh_subtree_under_root(outer, str(Path(outer) / "Box"))
    assert res["removed"] == 0 and set(lib._tracks) == ids0 and scanner._scan_count == 0


@pytest.mark.asyncio
async def test_a_re_tag_in_a_folder_clicks_commit_moves_the_row_between_listings(lib, monkeypatch):
    # As a scan's commit: a click's re-tag (the hand-over before it missed
    # the track) moves the row, inside a window that covers both roots.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])

    async def _missed(*_a, **_k):
        return None
    monkeypatch.setattr(scanner, "_release_under", _missed)
    b = Path(inner) / "b.wav"
    tid = next(t["id"] for t in lib._tracks.values() if t["path"] == str(b))
    oh, ih = path_hash(outer), path_hash(inner)
    lib.update_track_fields_batch([(tid, {"scan_root_hash": oh})])
    for h in (oh, ih):
        fstree._get_or_build_scan_root_sorted(lib, h)
    windows: list = []
    real_commit = fstree.root_commit

    class _seen(real_commit):
        def __init__(self, hashes):
            windows.append(set(hashes))
            super().__init__(hashes)
    monkeypatch.setattr(fstree, "root_commit", _seen)
    make_wav(b, 360, secs=2.0)
    await scanner.refresh_subtree_under_root(outer, inner)
    assert lib._tracks[tid]["scan_root_hash"] == ih
    assert any({oh, ih} <= w for w in windows)
    o_paths, _ = fstree._get_or_build_scan_root_sorted(lib, oh)
    i_paths, _ = fstree._get_or_build_scan_root_sorted(lib, ih)
    assert str(b) not in o_paths and str(b) in i_paths


@pytest.mark.asyncio
async def test_a_hand_over_limited_to_some_tracks_moves_only_those(lib):
    from soniqboom.core.data import hand_over_covered
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    _swap_all_to_outer(lib)
    b = next(t["id"] for t in lib._tracks.values() if t["path"].endswith("/b.wav"))
    assert await hand_over_covered(lib, outer, exclude=[outer], only=[b]) == {b: path_hash(inner)}
    assert _tags(lib)["Box/CD2/Bonus/c.wav"] == "outer"     # (not asked for: left)


@pytest.mark.asyncio
async def test_a_click_counts_its_inner_passes_unchanged_re_reads(lib):
    import os
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    b = Path(inner) / "b.wav"
    st = b.stat()
    os.utime(b, (st.st_atime, st.st_mtime + 5))             # (touched, not changed)
    res = await scanner.refresh_subtree_under_root(outer, str(Path(outer) / "Box"))
    assert res["unchanged"] == 1 and res["updated"] == 0


@pytest.mark.asyncio
async def test_clicks_and_a_scan_interleaving_over_old_tags_lose_and_read_nothing(lib, logs,
                                                                                  monkeypatch):
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)       # (yield as often as possible)
    monkeypatch.setattr(data, "_HAND_OVER_STEP", 1)
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    for i in range(30):
        make_wav(Path(inner) / "Bonus" / f"n{i:02d}.wav", 200 + i, secs=0.2)
    await scanner._run_scan([outer, inner])
    want = _tags(lib)
    added0 = {tid: t.get("added_at") for tid, t in lib._tracks.items()}
    box, bonus = str(Path(outer) / "Box"), str(Path(inner) / "Bonus")
    read = _count_reads(monkeypatch)
    for combo in ("scan+click", "click+click"):
        _swap_all_to_outer(lib)
        logs.clear()
        if combo == "scan+click":
            await asyncio.gather(scanner._run_scan([outer, inner]),
                                 scanner.refresh_subtree_under_root(outer, bonus))
        else:
            await asyncio.gather(scanner.refresh_subtree_under_root(outer, box),
                                 scanner.refresh_subtree_under_root(outer, bonus))
        assert {tid: t.get("added_at") for tid, t in lib._tracks.items()} == added0, combo
        assert _tags(lib) == want and not any(_extracted(logs).values()) and read == [], combo


@pytest.mark.asyncio
async def test_a_full_scan_of_a_root_being_removed_becomes_its_owners_scan_of_its_folder(lib, logs):
    # A full scan of the inner root in its removal window (asked for then, or
    # queued before): its folder is the outer root's now — the outer root's
    # scoped scan of it runs instead.  A change there is indexed (under the
    # outer root), nothing else is read, the inner root isn't walked.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    assert await removals.begin(inner, store=lib)
    make_wav(Path(inner) / "e.wav", 540)                    # (new in its window)
    logs.clear()
    await scanner.start_scan([inner], light=True)
    assert [(q[0], q[2]) for q in scanner._scan_queue] == [(frozenset({outer}),
                                                            {outer: frozenset({inner})})]
    await _drain()
    assert _tags(lib)["Box/CD2/e.wav"] == "outer" and _nonzero(_extracted(logs)) == {outer: 1}
    assert inner not in _discovered(logs)
    make_wav(Path(inner) / "f.wav", 560)
    logs.clear()
    assert await scanner._run_scan([inner]) is False        # (queued before its removal)
    assert inner not in _discovered(logs)
    await _drain()                                          # (the owner's scan it queued)
    assert _tags(lib)["Box/CD2/f.wav"] == "outer" and _nonzero(_extracted(logs)) == {outer: 1}


@pytest.mark.asyncio
async def test_a_removal_begun_after_the_walk_skips_that_roots_pass(lib, logs, monkeypatch):
    # The inner root's removal begins once the walk listed its files: its own
    # pass in this scan reads nothing (its folder is the outer root's now).
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    loop = asyncio.get_running_loop()
    real = scanner._find_audio_files

    def _walk_then_remove(*a, **k):
        out = real(*a, **k)
        asyncio.run_coroutine_threadsafe(removals.begin(inner, store=lib), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", _walk_then_remove)
    ids0 = set(lib._tracks)
    logs.clear()
    await scanner._run_scan([outer, inner])
    assert not _extracted(logs).get(inner) and set(lib._tracks) == ids0
    assert set(_tags(lib).values()) == {"outer"}


@pytest.mark.asyncio
async def test_rows_of_a_root_whose_removal_began_during_its_pass_go_to_the_owner(lib, monkeypatch):
    # The removal begins as the inner root's pass starts (after every earlier
    # check): what that pass reads is tagged with the outer root at the
    # commit, which covers the outer root's listing too.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    e = Path(inner) / "e.wav"
    make_wav(e, 540)                                        # (new: no old row)
    real_up = scanner.upsert_scan_dir
    began: list = []

    async def _up(root, *a, **k):
        if root == inner and not began:
            began.append(await removals.begin(inner, store=lib))
        return await real_up(root, *a, **k)
    monkeypatch.setattr(scanner, "upsert_scan_dir", _up)
    windows: list = []
    real_commit = fstree.root_commit

    class _seen(real_commit):
        def __init__(self, hashes):
            windows.append(set(hashes))
            super().__init__(hashes)
    monkeypatch.setattr(fstree, "root_commit", _seen)
    await scanner._run_scan([inner], scope={inner: frozenset({str(e)})})
    assert began and _tags(lib)["Box/CD2/e.wav"] == "outer"
    hidden, _ = removals.hidden_ids()
    assert not (hidden or set()) & set(lib._tracks)
    assert any(path_hash(outer) in w for w in windows)


@pytest.mark.asyncio
async def test_an_undo_during_the_outer_scan_keeps_an_old_tag_inside_the_inner_folder(lib, logs,
                                                                                     monkeypatch):
    # A track in the inner folder still tagged with the outer root (an older
    # scan's), the inner root's removal undone after the outer root's walk:
    # that track is not the outer root's to prune.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    c = next(t["id"] for t in lib._tracks.values() if t["path"].endswith("/c.wav"))
    lib.update_track_fields_batch([(c, {"scan_root_hash": path_hash(outer)})])
    assert await removals.begin(inner, store=lib)
    loop = asyncio.get_running_loop()
    real = scanner._find_audio_files

    async def _undo() -> None:
        assert removals.undo(inner, lib)

    def _walk_then_undo(*a, **k):
        out = real(*a, **k)
        asyncio.run_coroutine_threadsafe(_undo(), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", _walk_then_undo)
    ids0 = set(lib._tracks)
    await scanner._run_scan([outer])
    assert set(lib._tracks) == ids0                          # (c's file is still there)


@pytest.mark.asyncio
async def test_a_scoped_scan_over_a_folder_holding_the_inner_root_hands_its_old_tags_over(lib, logs):
    # Straight to the scan (no routing): the outer root's scan of ``Box``
    # hands what it holds in the inner folder over before it walks.
    outer = lib.paths["outer"]
    await scanner._run_scan([outer, lib.paths["inner"]])
    _swap_all_to_outer(lib)
    logs.clear()
    await scanner._run_scan([outer], scope={outer: frozenset({str(Path(outer) / "Box")})})
    assert _tags(lib) == _owners() and not any(_extracted(logs).values())



# ── Owners changing around scans, clicks and purges (third review) ───────────

@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before its hand-over", "during its hand-over"])
async def test_a_removal_begun_during_a_purge_keeps_what_the_undone_root_covers(lib, monkeypatch,
                                                                                 when):
    # The inner root is purged while it still holds tracks the outer root
    # covers (gained in its window); the outer root's removal begins as the
    # purge hands them over, then is undone.  A root still in its undo window
    # takes what it covers — nothing is deleted.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    for i in range(20):
        make_wav(Path(inner) / "Bonus" / f"n{i:02d}.wav", 200 + i, secs=0.2)
    await scanner._run_scan([outer, inner])
    ids0 = set(lib._tracks)
    from soniqboom.core import data, modland_catalog
    monkeypatch.setattr(modland_catalog, "_SLICE_S", 0.0)       # (a yield after every step)
    monkeypatch.setattr(data, "_HAND_OVER_STEP", 1)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    assert await removals.begin(inner, store=lib)
    ih = path_hash(inner)
    lib.update_track_fields_batch([(t["id"], {"scan_root_hash": ih}) for t in lib._tracks.values()
                                   if t["path"].startswith(inner + "/")])   # (gained meanwhile)
    real = data.hand_over_covered
    began: list = []

    async def _purge_hand_over(store, root, exclude=(), **kw):
        if root == inner and inner not in lib._scan_dirs and not began:
            began.append(1)

            async def _remove_outer() -> None:
                if when == "during its hand-over":
                    for _ in range(6):
                        await asyncio.sleep(0)
                removals.UNDO_S = 30.0                       # (its window stays open)
                await removals.begin(outer, store=lib)
            if when == "before its hand-over":
                await _remove_outer()
            else:
                asyncio.ensure_future(_remove_outer())
        return await real(store, root, exclude, **kw)
    monkeypatch.setattr(data, "hand_over_covered", _purge_hand_over)
    for _ in range(1000):
        if inner not in removals.registry(lib):
            break
        await asyncio.sleep(0.01)
    assert began and inner not in removals.registry(lib) and outer in removals.registry(lib)
    assert removals.undo(outer, lib)
    await _settle_removals(lib)
    assert set(lib._tracks) == ids0


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["purged", "removed keeping its tracks"])
async def test_a_change_queued_for_a_root_removed_before_its_scan_goes_to_the_owner(lib, logs,
                                                                                    monkeypatch,
                                                                                    how):
    # A change in the inner folder is queued for the inner root alone (both
    # watches report it); the inner root is removed before that scan runs:
    # the queued paths go to the outer root's scan (``forget_root``).
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    b = Path(inner) / "b.wav"
    make_wav(b, 360, secs=3.0)                              # (really changed)
    gate, busy = await _hold_scans(monkeypatch)
    await scanner.start_scan([], scope={outer: [str(b)], inner: [str(b)]})
    assert [q[0] for q in scanner._scan_queue] == [frozenset({inner})]
    if how == "purged":
        monkeypatch.setattr(removals, "UNDO_S", 0.0)
        assert await removals.begin(inner, store=lib)
        await _settle_removals(lib)
    else:
        from soniqboom.core.data import delete_scan_dir
        await delete_scan_dir(inner)
        scanner.forget_root(inner)
        while removals._side:
            await asyncio.gather(*list(removals._side), return_exceptions=True)
    assert inner not in lib._scan_dirs
    assert [q[0] for q in scanner._scan_queue] == [frozenset({outer})]
    logs.clear()
    await _let_scans_run(monkeypatch, gate, busy)
    assert _dur(lib, b) == 3 and _tags(lib)["Box/CD2/b.wav"] == "outer"
    assert _nonzero(_extracted(logs)) == {outer: 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("then", ["undo", "purge"])
async def test_a_removal_begun_mid_scan_still_indexes_what_changed_in_the_folder(lib, logs,
                                                                                monkeypatch, then):
    # A file changed while the server was down; the inner root's removal
    # begins once the walk ran (the outer root's walk skipped that folder):
    # the inner root's pass is skipped, and the outer root's scan of its
    # folder, queued, reads the change.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    b = Path(inner) / "b.wav"
    make_wav(b, 360, secs=3.0)
    loop = asyncio.get_running_loop()
    real = scanner._find_audio_files
    monkeypatch.setattr(removals, "UNDO_S", 0.3 if then == "purge" else 30.0)

    def _walk_then_remove(*a, **k):
        out = real(*a, **k)
        asyncio.run_coroutine_threadsafe(removals.begin(inner, store=lib), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", _walk_then_remove)
    ids0 = set(lib._tracks)
    await scanner._run_scan([outer, inner])
    monkeypatch.setattr(scanner, "_find_audio_files", real)
    await _drain()
    assert _dur(lib, b) == 3
    if then == "undo":
        assert removals.undo(inner, lib)
    await _settle_removals(lib)
    assert _dur(lib, b) == 3 and set(lib._tracks) == ids0   # (the outer root covers them all)


@pytest.mark.asyncio
async def test_a_root_removed_keeping_its_tracks_mid_scan_leaves_its_changes_to_the_owner(
        lib, logs, monkeypatch):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    b = Path(inner) / "b.wav"
    make_wav(b, 360, secs=3.0)
    loop = asyncio.get_running_loop()
    real = scanner._find_audio_files

    async def _remove() -> None:
        from soniqboom.core.data import delete_scan_dir
        await delete_scan_dir(inner)
        scanner.forget_root(inner)

    def _walk_then_remove(*a, **k):
        out = real(*a, **k)
        asyncio.run_coroutine_threadsafe(_remove(), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", _walk_then_remove)
    await scanner._run_scan([outer, inner])
    monkeypatch.setattr(scanner, "_find_audio_files", real)
    while removals._side:
        await asyncio.gather(*list(removals._side), return_exceptions=True)
    await _drain()
    assert _dur(lib, b) == 3 and _tags(lib)["Box/CD2/b.wav"] == "outer"


@pytest.mark.asyncio
async def test_a_change_read_before_its_roots_purge_is_kept_under_the_owner(lib, logs,
                                                                           monkeypatch):
    # The inner root's pass reads a change; its removal is purged before the
    # scan commits: the row goes to the outer root, not dropped with the root.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    b = Path(inner) / "b.wav"
    make_wav(b, 360, secs=3.0)
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    real = scanner.upsert_scan_dir
    state = {"done": False}

    async def _hook(path, *a, **k):
        if path == outer and not a and not k and not state["done"]:
            state["done"] = True                            # (the inner pass is done)
            assert await removals.begin(inner, store=lib)
            await _settle_removals(lib)
            assert inner not in lib._scan_dirs
        return await real(path, *a, **k)
    monkeypatch.setattr(scanner, "upsert_scan_dir", _hook)
    await scanner._run_scan([inner, outer])                 # (the inner root's pass first)
    assert state["done"] and _dur(lib, b) == 3 and _tags(lib)["Box/CD2/b.wav"] == "outer"


@pytest.mark.asyncio
async def test_an_undo_mid_scan_queues_the_restored_roots_scan(lib, logs, monkeypatch):
    # A new file in the inner folder during its window, the removal undone
    # after the outer root's walk listed it: the outer pass leaves it out
    # and the inner root's own scan, queued, indexes it under the inner root.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    assert await removals.begin(inner, store=lib)
    make_wav(Path(inner) / "new.wav", 640)
    loop = asyncio.get_running_loop()
    real = scanner._find_audio_files

    async def _undo() -> None:
        assert removals.undo(inner, lib)

    def _walk_then_undo(*a, **k):
        out = real(*a, **k)
        asyncio.run_coroutine_threadsafe(_undo(), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", _walk_then_undo)
    logs.clear()
    await scanner._run_scan([outer])
    monkeypatch.setattr(scanner, "_find_audio_files", real)
    await _drain()
    assert _tags(lib).get("Box/CD2/new.wav") == "inner"
    assert _nonzero(_extracted(logs)) == {inner: 1}         # (only the new file read)


@pytest.mark.asyncio
async def test_an_undo_mid_click_reads_nothing_again(lib, monkeypatch):
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    assert await removals.begin(inner, store=lib)
    loop = asyncio.get_running_loop()
    real = scanner._find_audio_files

    async def _undo() -> None:
        assert removals.undo(inner, lib)

    def _walk_then_undo(*a, **k):
        out = real(*a, **k)
        if a and a[0] == [str(Path(outer) / "Box")]:      # (the click's first walk)
            asyncio.run_coroutine_threadsafe(_undo(), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", _walk_then_undo)
    e = Path(inner) / "e.wav"
    make_wav(e, 540)                                        # (new in its window)
    read = _count_reads(monkeypatch)
    res = await scanner.refresh_subtree_under_root(outer, str(Path(outer) / "Box"))
    await _settle_removals(lib)
    # (only the new file is read — in the restored root's own pass, after the
    #  first: its folder's change is picked up, under it)
    assert read == [str(e)] and res["removed"] == 0
    assert _tags(lib) == {**_owners(), "Box/CD2/e.wav": "inner"}


@pytest.mark.asyncio
async def test_a_clicks_rows_after_its_roots_removal_began_go_to_the_owner(lib, monkeypatch):
    # A click on the inner folder still reading when the inner root's
    # removal begins: what it writes is the outer root's (not hidden).
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    c = Path(inner) / "Bonus" / "c.wav"
    make_wav(c, 420, secs=3.0)
    loop = asyncio.get_running_loop()
    real_x = scanner._extract_one
    monkeypatch.setattr(removals, "UNDO_S", 30.0)

    def _x(p, *a, **k):
        out = real_x(p, *a, **k)
        asyncio.run_coroutine_threadsafe(removals.begin(inner, store=lib), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_extract_one", _x)
    windows: list = []
    real_commit = fstree.root_commit

    class _seen(real_commit):
        def __init__(self, hashes):
            windows.append(set(hashes))
            super().__init__(hashes)
    monkeypatch.setattr(fstree, "root_commit", _seen)
    await scanner.refresh_subtree_under_root(inner, str(Path(inner) / "Bonus"))
    tid = next(t["id"] for t in lib._tracks.values() if t["path"] == str(c))
    hidden, _ = removals.hidden_ids()
    assert tid not in (hidden or set()) and _tags(lib)["Box/CD2/Bonus/c.wav"] == "outer"
    assert _dur(lib, c) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [True, False])
async def test_a_roots_own_folder_rerouted_at_its_scan_gets_a_full_light_scan(lib, logs, mixed):
    # The inner root's own folder in a scope that reaches the scan after the
    # owner changed (reported during its removal window, undone since): it
    # gets the inner root's full, light scan — the hand-over first (no old
    # tag read again) and its give-up records kept (a waiting file waits).
    import time as _time
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    _swap_all_to_outer(lib)
    e = Path(inner) / "e.wav"
    make_wav(e, 540)
    key = scanner._GIVEUP_KEY + path_hash(inner)
    lib.set_config(key, {str(e): [e.stat().st_mtime, e.stat().st_size, "crash", 1, _time.time()]})
    a = str(Path(outer) / "Box" / "CD1" / "a.wav")
    logs.clear()
    await scanner._run_scan([outer], scope={outer: frozenset({inner, a} if mixed else {inner})})
    await _drain()
    assert not any(_extracted(logs).values()) and _tags(lib) == _owners()
    assert "Box/CD2/e.wav" not in _tags(lib)                # (given up: still waiting)



@pytest.mark.asyncio
async def test_a_drop_gives_a_neighbour_still_in_its_undo_window_what_it_covers(lib, monkeypatch):
    # The inner root's removal drops it as the outer root's removal begins:
    # the drop's hand-over gives the outer root (still undoable) what it
    # covers — none of it is left for the purge to delete.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    ids0 = set(lib._tracks)
    from soniqboom.core import data, watcher
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    assert await removals.begin(inner, store=lib)
    ih = path_hash(inner)
    lib.update_track_fields_batch([(t["id"], {"scan_root_hash": ih}) for t in lib._tracks.values()
                                   if t["path"].startswith(inner + "/")])   # (gained meanwhile)
    real_rm = watcher.remove_root

    async def _rm(path, *a, **k):
        if path == inner:
            removals.UNDO_S = 30.0
            await removals.begin(outer, store=lib)          # (as the inner root drops)
        return await real_rm(path, *a, **k)
    monkeypatch.setattr(watcher, "remove_root", _rm)
    left: list = []
    real_purge = data.delete_tracks_by_scan_root

    async def _purge(root, *a, **k):
        left.append(len(lib.get_track_ids_for_scan_root(path_hash(root))))
        return await real_purge(root, *a, **k)
    monkeypatch.setattr(data, "delete_tracks_by_scan_root", _purge)
    for _ in range(1000):
        if inner not in removals.registry(lib):
            break
        await asyncio.sleep(0.01)
    assert left == [0] and outer in removals.registry(lib)  # (the drop gave them all)
    assert removals.undo(outer, lib)
    await _settle_removals(lib)
    assert set(lib._tracks) == ids0


@pytest.mark.asyncio
async def test_a_purge_gives_a_neighbour_still_in_its_undo_window_what_it_covers(lib, monkeypatch):
    # Tracks the inner root gains between its drop and its purge, as the outer
    # root's removal begins: the purge's hand-over gives them to the outer
    # root (still undoable) instead of deleting them.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    ids0 = set(lib._tracks)
    from soniqboom.core import data
    monkeypatch.setattr(removals, "UNDO_S", 0.0)
    assert await removals.begin(inner, store=lib)
    ih = path_hash(inner)
    real_purge = data.delete_tracks_by_scan_root

    async def _purge(root, *a, **k):
        if root == inner:
            lib.update_track_fields_batch([(t["id"], {"scan_root_hash": ih})
                                           for t in lib._tracks.values()
                                           if t["path"].startswith(inner + "/")])  # (gained)
            removals.UNDO_S = 30.0
            await removals.begin(outer, store=lib)          # (before the purge hands over)
        return await real_purge(root, *a, **k)
    monkeypatch.setattr(data, "delete_tracks_by_scan_root", _purge)
    for _ in range(1000):
        if inner not in removals.registry(lib):
            break
        await asyncio.sleep(0.01)
    assert outer in removals.registry(lib)
    assert removals.undo(outer, lib)
    await _settle_removals(lib)
    assert set(lib._tracks) == ids0


@pytest.mark.asyncio
async def test_a_root_removed_keeping_its_tracks_gives_a_neighbour_in_its_window_its_tracks(lib):
    # Settings → Remove (keep the tracks) of the inner root while the outer
    # root's removal can still be undone: its tracks go to the outer root
    # (not left with a root that's gone), and stay with it after the Undo.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    ids0 = set(lib._tracks)
    assert await removals.begin(outer, store=lib)           # (its window stays open)
    from soniqboom.core.data import delete_scan_dir
    await delete_scan_dir(inner)
    while removals._side:
        await asyncio.gather(*list(removals._side), return_exceptions=True)
    assert removals.undo(outer, lib)
    await _settle_removals(lib)
    assert set(lib._tracks) == ids0 and set(_tags(lib).values()) == {"outer"}


@pytest.mark.asyncio
async def test_no_hand_over_gives_to_a_root_past_its_undo_window(lib):
    from soniqboom.core.data import hand_over_covered
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    removals._purging.add(outer)                            # (finishing: no Undo any more)
    try:
        assert await hand_over_covered(lib, inner) == {}    # (a removal's own hand-over)
    finally:
        removals._purging.discard(outer)
    assert set(_tags(lib).values()) == {"outer", "inner"}


@pytest.mark.asyncio
async def test_an_undo_during_a_scoped_walk_leaves_the_change_to_the_restored_root(lib, logs,
                                                                                   monkeypatch):
    # A change in the inner folder routed to the outer root in its window;
    # the Undo comes during the outer root's walk: that file is left out of
    # its pass, and the inner root's own scan of it, queued, reads it.
    outer, inner = lib.paths["outer"], lib.paths["inner"]
    await scanner._run_scan([outer, inner])
    assert await removals.begin(inner, store=lib)
    c = Path(inner) / "Bonus" / "c.wav"
    make_wav(c, 420, secs=3.0)                              # (really changed)
    loop = asyncio.get_running_loop()
    real = scanner._find_audio_files

    async def _undo() -> None:
        assert removals.undo(inner, lib)

    def _walk_then_undo(*a, **k):
        out = real(*a, **k)
        if removals.registry(lib):
            asyncio.run_coroutine_threadsafe(_undo(), loop).result(10)
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", _walk_then_undo)
    logs.clear()
    await scanner._run_scan([inner], scope={inner: frozenset({str(Path(inner) / "Bonus")})})
    monkeypatch.setattr(scanner, "_find_audio_files", real)
    await _drain()
    assert _dur(lib, c) == 3 and _tags(lib) == _owners()
    assert _nonzero(_extracted(logs)) == {inner: 1}
