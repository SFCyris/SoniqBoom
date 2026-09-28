# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Folder watcher (GitHub #16): reads never trigger a rescan; real changes
trigger a SCOPED scan of just the changed paths; a scan that changed nothing
skips the library-wide post-scan passes; a small change is committed and
duplicate-grouped incrementally.

Fixtures are generated (WAV / ProTracker MOD / PSID) — no private test data."""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import random
import shutil
import stat
import struct
import sys
import threading
import time
import wave
import zipfile
from pathlib import Path

import pytest

from soniqboom.core import diskimage, scanner, watcher
from soniqboom.core.data import path_hash

_REAL_FULL_DUP = scanner._run_duplicate_detection_async    # (fixtures replace it)

pytest.importorskip("watchdog")
from watchdog.events import (  # noqa: E402
    DirDeletedEvent, DirModifiedEvent, FileClosedEvent, FileClosedNoWriteEvent,
    FileCreatedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent,
    FileOpenedEvent,
)


# ── Generated fixtures ──────────────────────────────────────────────────────

def make_wav(path: Path, secs: float = 2.0, freq: float = 440.0, rate: int = 8000) -> Path:
    n = int(secs * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)))
    return path


def make_mod(path: Path, title: bytes = b"synthetic") -> Path:
    """A minimal 4-channel ProTracker module (one pattern, one sample)."""
    samples = b"square".ljust(22, b"\0") + struct.pack(">HBBHH", 512, 0, 64, 0, 512)
    samples += (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
    pat = bytearray(1024)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, 0x00])            # C-3, sample 1
    path.write_bytes(title[:20].ljust(20, b"\0") + samples + bytes([1, 127]) + bytes(128)
                     + b"M.K." + bytes(pat) + bytes(([100] * 16 + [156] * 16) * 32))
    return path


def make_psid(path: Path, name: bytes = b"Tune", songs: int = 1) -> Path:
    """A PSID v2 header + a one-byte player (RTS)."""
    hdr = (b"PSID" + struct.pack(">HHHHHHHI", 2, 0x7C, 0, 0x1000, 0x1003, songs, 1, 0)
           + name.ljust(32, b"\0") + b"Someone".ljust(32, b"\0") + b"1989".ljust(32, b"\0")
           + struct.pack(">HBBBB", 0, 0, 0, 0, 0))
    path.write_bytes(hdr + struct.pack("<H", 0x1000) + b"\x60" * 8)
    return path


def _case_insensitive_fs(d: Path) -> bool:
    probe = d / "CaseProbe"
    probe.write_text("x")
    try:
        return (d / "caseprobe").exists()
    finally:
        probe.unlink()


# ── Watcher: which events count, and what they collect ──────────────────────

@pytest.fixture
def captured(monkeypatch):
    got: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(watcher, "_mark_dirty", lambda root, paths=None: got.append((root, paths)))
    return got


def test_reads_are_ignored_writes_count(captured, tmp_path):
    h = watcher._Handler(str(tmp_path))
    f = str(tmp_path / "a" / "tune.mod")
    h.on_any_event(FileOpenedEvent(f))
    h.on_any_event(FileClosedNoWriteEvent(f))
    h.on_any_event(DirModifiedEvent(str(tmp_path / "a")))     # a child changed: its own event counts
    assert captured == [], "a plain read (open / close-without-write) triggered a rescan"
    for ev in (FileClosedEvent(f), FileCreatedEvent(f), FileModifiedEvent(f), FileDeletedEvent(f)):
        h.on_any_event(ev)
    assert [p for _, p in captured] == [[f]] * 4


def test_moves_folders_non_music_and_temp_names(captured, tmp_path):
    h = watcher._Handler(str(tmp_path))
    h.on_any_event(FileMovedEvent(str(tmp_path / "x.mod"), str(tmp_path / "sub" / "x.mod")))
    h.on_any_event(DirDeletedEvent(str(tmp_path / "Album")))
    h.on_any_event(FileCreatedEvent(str(tmp_path / "notes.txt")))
    h.on_any_event(FileCreatedEvent(str(tmp_path / "mdat.song")))          # Amiga prefix form
    h.on_any_event(FileCreatedEvent(str(tmp_path / "pack.zip")))           # container
    # rsync / a download client: temp name renamed to the final name
    h.on_any_event(FileMovedEvent(str(tmp_path / "A" / ".01.flac.Xy12"), str(tmp_path / "A" / "01.flac")))
    assert captured[0][1] == [str(tmp_path / "x.mod"), str(tmp_path / "sub" / "x.mod")]
    assert captured[1][1] == [str(tmp_path / "Album")]
    assert [p for _, p in captured[2:]] == [[str(tmp_path / "mdat.song")],
                                            [str(tmp_path / "pack.zip")],
                                            [str(tmp_path / "A" / "01.flac")]]


def test_events_in_the_disk_spelling_map_to_the_registered_root(captured):
    h = watcher._Handler("/Vol/music", "/Vol/Music")
    h.on_any_event(FileCreatedEvent("/Vol/Music/A/x.mod"))
    h.on_any_event(FileCreatedEvent("/Vol/Musicals/y.mod"))               # a sibling, not the root
    assert [p for _, p in captured] == [["/Vol/music/A/x.mod"], ["/Vol/Musicals/y.mod"]]


def test_canonical_path_finds_the_disk_spelling(tmp_path):
    (tmp_path / "Music").mkdir()
    real = str(tmp_path.resolve() / "Music")
    if not _case_insensitive_fs(tmp_path):
        assert watcher._canonical_path(real) == real
        return
    assert watcher._canonical_path(str(tmp_path.resolve() / "music")) == real


def test_collapse_folds_into_the_outermost_folder_and_keeps_the_latest_time():
    r = "/r"
    got = watcher._collapse({f"{r}/Album": 1.0, f"{r}/Album (Live)/x.mp3": 2.0,
                             f"{r}/Album/01.mp3": 3.0, f"{r}/Album/CD2/02.mp3": 4.0})
    assert got == {f"{r}/Album": 4.0, f"{r}/Album (Live)/x.mp3": 2.0}


def test_widen_goes_up_to_parent_folders_before_giving_up(monkeypatch):
    monkeypatch.setattr(watcher, "_SCOPE_MAX", 3)
    r = "/r"
    many = {f"{r}/A/{i}.mod": float(i) for i in range(5)} | {f"{r}/B/{i}.mod": 9.0 for i in range(5)}
    assert watcher._widen(dict(many), r) == {f"{r}/A": 4.0, f"{r}/B": 9.0}
    spread = {f"{r}/d{i}/x.mod": 1.0 for i in range(5)}                 # 5 top-level folders
    assert watcher._widen(spread, r) is None


# ── Watcher: debounce / hold / cap, driven on a real loop ───────────────────

@pytest.fixture
def armed(monkeypatch, tmp_path):
    """Watcher state with ``tmp_path`` registered as a watched root and a
    recording ``start_scan``."""
    root = str(tmp_path)
    calls: list = []

    async def fake_start_scan(dirs, on_progress=None, *, scope=None, rescan_if_running=False, light=False):
        calls.append((sorted(dirs),
                      {k: sorted(v) for k, v in scope.items()} if scope else None,
                      rescan_if_running))
    monkeypatch.setattr(scanner, "start_scan", fake_start_scan)
    monkeypatch.setattr(watcher, "_QUIET_SEC", 0.15)
    monkeypatch.setattr(watcher, "_MAX_WAIT_SEC", 5.0)
    monkeypatch.setattr(watcher._state, "pending", {})
    monkeypatch.setattr(watcher._state, "watches", {root: object()})
    monkeypatch.setattr(watcher._state, "debounce_task", None)
    return root, calls


@pytest.mark.asyncio
async def test_debounce_waits_for_quiet_then_queues_one_scoped_scan(armed):
    root, calls = armed
    a, b = f"{root}/Album/1.mod", f"{root}/Album/2.mod"
    watcher._note_change(root, [a])
    await asyncio.sleep(0.08)
    watcher._note_change(root, [b])                       # keeps the burst open
    watcher._note_change(root, [f"{root}/Album"])         # folder covers its files
    await asyncio.sleep(0.08)
    assert calls == [], "rescan fired before the burst was quiet"
    await asyncio.sleep(0.3)
    assert calls == [([root], {root: [f"{root}/Album"]}, False)]


@pytest.mark.asyncio
async def test_cap_scans_quiet_paths_and_holds_one_still_being_written(armed, monkeypatch):
    root, calls = armed
    monkeypatch.setattr(watcher, "_MAX_WAIT_SEC", 0.5)
    cold, hot = f"{root}/B/done.mod", f"{root}/A/downloading.flac"
    watcher._note_change(root, [cold])
    t_end = time.monotonic() + 0.9
    while time.monotonic() < t_end:                        # a writer: an event every 50 ms
        watcher._note_change(root, [hot])
        await asyncio.sleep(0.05)
    assert calls == [([root], {root: [cold]}, False)], "the cap must scan the quiet path only"
    await asyncio.sleep(0.4)                               # writer stopped → quiet → scanned
    assert calls[1:] == [([root], {root: [hot]}, False)]


@pytest.mark.asyncio
async def test_a_quiet_file_with_a_fresh_mtime_is_held_until_it_settles(armed, monkeypatch, tmp_path):
    root, calls = armed
    monkeypatch.setattr(watcher, "_QUIET_SEC", 0.3)
    f = tmp_path / "growing.flac"
    f.write_bytes(b"x")
    watcher._note_change(root, [str(f)])
    t_end = time.monotonic() + 0.8
    while time.monotonic() < t_end:                       # written without any events
        with open(f, "ab") as fh:
            fh.write(b"x")
        await asyncio.sleep(0.05)
    assert calls == [], "a file still being written was scanned"
    await asyncio.sleep(1.0)
    assert calls == [([root], {root: [str(f)]}, False)]


@pytest.mark.asyncio
async def test_overflow_or_a_root_level_change_falls_back_to_a_full_rescan(armed, monkeypatch):
    root, calls = armed
    monkeypatch.setattr(watcher, "_SCOPE_MAX", 3)
    for i in range(5):
        watcher._note_change(root, [f"{root}/d{i}/x.mod"])
    await asyncio.sleep(0.4)
    assert calls == [([root], None, True)], "the full fallback must queue behind a running scan"
    calls.clear()
    watcher._note_change(root, [root])
    await asyncio.sleep(0.4)
    assert calls == [([root], None, True)]


@pytest.mark.asyncio
async def test_many_files_in_a_few_folders_scan_those_folders_not_the_library(armed, monkeypatch):
    root, calls = armed
    monkeypatch.setattr(watcher, "_SCOPE_MAX", 3)
    watcher._note_change(root, [f"{root}/Album/{i:02}.flac" for i in range(40)])
    await asyncio.sleep(0.4)
    assert calls == [([root], {root: [f"{root}/Album"]}, False)]


@pytest.mark.asyncio
async def test_events_arriving_while_a_batch_is_queued_are_picked_up(armed, monkeypatch):
    root, calls = armed
    real = scanner.start_scan

    async def slow(dirs, on_progress=None, *, scope=None, rescan_if_running=False, light=False):
        await real(dirs, on_progress, scope=scope, rescan_if_running=rescan_if_running, light=light)
        if len(calls) == 1:
            watcher._note_change(root, [f"{root}/late.mod"])
            await asyncio.sleep(0.05)
    monkeypatch.setattr(scanner, "start_scan", slow)
    watcher._note_change(root, [f"{root}/first.mod"])
    await asyncio.sleep(0.6)
    assert [c[1] for c in calls] == [{root: [f"{root}/first.mod"]}, {root: [f"{root}/late.mod"]}]


@pytest.mark.asyncio
async def test_a_removed_root_is_never_rescanned(armed, monkeypatch):
    root, calls = armed
    monkeypatch.setattr(watcher._state, "enabled", True)

    class _Obs:
        def unschedule(self, w):
            pass
    monkeypatch.setattr(watcher._state, "observer", _Obs())
    watcher._note_change(root, [f"{root}/x.mod"])
    await watcher.remove_root(root)
    assert watcher._state.pending == {}, "remove_root left the root's changes pending"
    watcher._note_change(root, [f"{root}/y.mod"])          # a late event from the thread
    assert watcher._state.pending == {}, "an event for an unwatched root was buffered"
    await asyncio.sleep(0.4)
    assert calls == []


# ── Scanner: scoped scans and the no-change short-circuit ───────────────────

@pytest.fixture
def library(tmp_path, monkeypatch, tmp_data_dir):
    """A fresh store and a small library: A/one.wav A/two.wav B/three.mod.
    ``stats`` counts the duplicate passes (the full one is faked — it runs a
    subprocess over the whole store) and aggregation-cache invalidations."""
    from soniqboom.core import store as store_mod
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    # A ``_run_scan`` called directly that raises or is cancelled mid-way
    # never reaches its bookkeeping (``_drain_scan_queue`` cleans up after
    # the real scan task): its scan count and running flag would outlive the
    # test (every later scan counted as concurrent, getScanStatus reporting
    # a scan forever).
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())
    stats = {"full": 0, "incr": 0, "agg": 0}

    async def fake_full():
        stats["full"] += 1
    real_incr = scanner._run_duplicate_detection_incremental

    async def spy_incr(delta):
        stats["incr"] += 1
        return await real_incr(delta)
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", fake_full)
    monkeypatch.setattr(scanner, "_run_duplicate_detection_incremental", spy_incr)
    from soniqboom.api import library as lib_api
    real_inv = lib_api.invalidate_agg_cache

    def spy_inv():
        stats["agg"] += 1
        return real_inv()
    monkeypatch.setattr(lib_api, "invalidate_agg_cache", spy_inv)
    root = tmp_path / "lib"
    for d in ("A", "B"):
        (root / d).mkdir(parents=True)
    make_wav(root / "A" / "one.wav", secs=2.0)
    make_wav(root / "A" / "two.wav", secs=3.0, freq=330)
    make_mod(root / "B" / "three.mod")
    root = root.resolve()
    store_mod._store._lib_root = root
    return root, store_mod._store, stats


def _paths(store):
    return sorted(os.path.relpath(t["path"], start=str(store._lib_root)) for t in store._tracks.values())


def _scope(root, *rel):
    return {str(root): frozenset(str(root / r) for r in rel)}


@pytest.mark.asyncio
async def test_scoped_scan_adds_updates_and_prunes_only_what_changed(library):
    root, store, stats = library
    assert await scanner._run_scan([str(root)]) is True
    assert _paths(store) == ["A/one.wav", "A/two.wav", "B/three.mod"]
    stats.update(full=0, incr=0, agg=0)

    # An event for an unchanged file → nothing changes → no library-wide work.
    assert await scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav")) is False
    assert stats == {"full": 0, "incr": 0, "agg": 0}

    # New file + deleted file in one scoped scan; a deletion OUTSIDE the scope
    # is not pruned (that is what distinguishes it from a full scan).
    make_mod(root / "A" / "four.mod", title=b"four")
    (root / "A" / "two.wav").unlink()
    (root / "B" / "three.mod").unlink()
    assert await scanner._run_scan([str(root)], scope=_scope(root, "A/four.mod", "A/two.wav")) is True
    assert _paths(store) == ["A/four.mod", "A/one.wav", "B/three.mod"]
    assert stats["incr"] == 1 and stats["full"] == 0 and stats["agg"] >= 1
    assert store._scan_dirs[str(root)]["track_count"] == 3     # stored count, recounted

    # A whole deleted folder
    shutil.rmtree(root / "A")
    assert await scanner._run_scan([str(root)], scope=_scope(root, "A")) is True
    assert _paths(store) == ["B/three.mod"]
    assert store._scan_dirs[str(root)]["track_count"] == 1


@pytest.mark.asyncio
async def test_touched_file_only_refreshes_mtime_and_a_changed_one_keeps_added_at(library):
    root, store, stats = library
    await scanner._run_scan([str(root)])
    tid = next(t["id"] for t in store._tracks.values() if t["path"].endswith("one.wav"))
    added0 = store._tracks[tid]["added_at"] = 1_000_000          # "added long ago"
    stats.update(full=0, incr=0, agg=0)
    seq0 = store._catalog_seq

    later = time.time() + 30
    os.utime(root / "A" / "one.wav", (later, later))           # touched, same content
    assert await scanner._run_scan([str(root)]) is False
    t = store._tracks[tid]
    assert abs(t["mtime"] - later) < 1 and t["added_at"] == added0
    assert stats == {"full": 0, "incr": 0, "agg": 0} and store._catalog_seq == seq0

    make_wav(root / "A" / "one.wav", secs=4.0)                  # really changed
    assert await scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav")) is True
    t = store._tracks[tid]
    assert round(t["duration"]) == 4 and t["added_at"] == added0


@pytest.mark.asyncio
async def test_a_small_commit_merges_into_the_sorted_indexes_without_a_rebuild(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    rebuilds = []
    real_exit = scanner._async_exit_batch_mode

    async def spy_exit(st):
        rebuilds.append(1)
        return await real_exit(st)
    monkeypatch.setattr(scanner, "_async_exit_batch_mode", spy_exit)
    make_mod(root / "B" / "aardvark.mod", title=b"Aardvark")
    assert await scanner._run_scan([str(root)], scope=_scope(root, "B/aardvark.mod")) is True
    assert rebuilds == [], "a one-file commit re-sorted every index of the library"
    titles = [k for k, _tid in store._sorted_title]
    assert titles == sorted(titles) and "aardvark" in titles
    from soniqboom.core import data
    report = await data.rebuild_indexes()
    assert report.get("index_ok", True) and not report.get("mismatches"), report


@pytest.mark.asyncio
async def test_scoped_scan_never_prunes_when_the_root_reads_empty(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    monkeypatch.setattr(scanner, "_root_is_live", lambda r: False)   # a dropped mount
    (root / "A" / "one.wav").unlink()
    await scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav"))
    assert "A/one.wav" in _paths(store)


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions as a non-root user")
@pytest.mark.asyncio
async def test_an_unreadable_folder_is_not_a_deletion(library):
    root, store, _ = library
    (root / "A" / "sub").mkdir()
    make_mod(root / "A" / "sub" / "x.mod")
    await scanner._run_scan([str(root)])
    assert "A/sub/x.mod" in _paths(store)
    os.chmod(root / "A", 0)
    try:
        await scanner._run_scan([str(root)], scope=_scope(root, "A/sub/x.mod"))
    finally:
        os.chmod(root / "A", 0o755)
    assert "A/sub/x.mod" in _paths(store)


@pytest.mark.asyncio
async def test_case_only_rename_does_not_duplicate(library):
    root, store, _ = library
    if not _case_insensitive_fs(root):
        pytest.skip("case-sensitive filesystem")
    await scanner._run_scan([str(root)])
    (root / "A" / "one.wav").rename(root / "A" / "One.wav")
    await scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav", "A/One.wav"))
    assert _paths(store) == ["A/One.wav", "A/two.wav", "B/three.mod"]
    (root / "A").rename(root / "tmp")
    (root / "tmp").rename(root / "a")
    await scanner._run_scan([str(root)], scope=_scope(root, "A", "a"))
    assert _paths(store) == ["B/three.mod", "a/One.wav", "a/two.wav"]


@pytest.mark.asyncio
async def test_folder_delete_finds_subfolders_without_the_hash_lookup_table(library):
    """``_hash_lookups`` is persisted only by the clean-shutdown snapshot —
    after a crash it can miss folders; the scoped prune must not need it."""
    root, store, _ = library
    for cd in ("CD1", "CD2"):
        (root / "A" / cd).mkdir()
        make_mod(root / "A" / cd / f"{cd}.mod")
    await scanner._run_scan([str(root)])
    store._hash_lookups.clear()
    shutil.rmtree(root / "A")
    await scanner._run_scan([str(root)], scope=_scope(root, "A"))
    assert _paths(store) == ["B/three.mod"]


@pytest.mark.asyncio
async def test_scope_walk_matches_the_full_walk_for_symlinks(library):
    root, store, _ = library
    ext = root.parent / "outside"
    ext.mkdir()
    make_mod(ext / "linked.mod")
    (root / "B" / "linkdir").symlink_to(ext, target_is_directory=True)
    (root / "B" / "link.mod").symlink_to(ext / "linked.mod")
    (root / "B" / "dangling.mod").symlink_to(ext / "nope.mod")
    await scanner._run_scan([str(root)])
    full = _paths(store)
    await scanner._run_scan([str(root)], scope=_scope(root, "B/linkdir", "B/link.mod",
                                                      "B/dangling.mod", "B/linkdir/linked.mod"))
    assert _paths(store) == full and "B/link.mod" in full and "B/linkdir/linked.mod" not in full


@pytest.mark.asyncio
async def test_hvsc_configured_by_a_no_change_scan_is_still_applied(library, monkeypatch):
    root, store, _ = library
    make_psid(root / "B" / "tune.sid")
    await scanner._run_scan([str(root)])
    scanner._hvsc_probed_roots.discard(str(root))
    applied, calls = [], []
    monkeypatch.setattr(scanner, "_detect_hvsc_docs_local", lambda dirs: "/hvsc/DOCUMENTS")

    async def fake_autoconf(docs):
        applied.append(docs)
        return True
    monkeypatch.setattr(scanner, "_apply_hvsc_autoconfig", fake_autoconf)
    from soniqboom.core import hvsc, hvsc_apply

    class _H:
        def is_configured(self):
            return bool(applied)
    monkeypatch.setattr(hvsc, "get_hvsc", lambda: _H())

    async def fake_apply(**kw):
        calls.append(kw)
        return {}
    monkeypatch.setattr(hvsc_apply, "apply_hvsc_to_library", fake_apply)
    assert await scanner._run_scan([str(root)]) is False
    assert applied and calls == [{"reload": False, "ids": None}]


@pytest.mark.asyncio
async def test_drain_spawns_enrichment_only_when_a_scan_changed_something(library, monkeypatch):
    root, store, _ = library
    spawned = []
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda: spawned.append(1))
    monkeypatch.setattr(scanner, "_scan_queue", [])
    monkeypatch.setattr(scanner, "_scan_task", None)
    await (await scanner.start_scan([str(root)]))
    assert spawned == [1]
    await (await scanner.start_scan([str(root)], scope=_scope(root, "A/one.wav")))
    assert spawned == [1], "a no-change watcher scan re-ran the enrichment runner"


# ── Scan queue ──────────────────────────────────────────────────────────────

class _Busy:
    def done(self):
        return False


@pytest.mark.asyncio
async def test_queue_merges_scoped_scans_and_a_queued_full_scan_absorbs_them(monkeypatch, tmp_path):
    root = str(tmp_path.resolve())
    monkeypatch.setattr(scanner, "_scan_queue", [])
    monkeypatch.setattr(scanner, "_scan_task", _Busy())      # a scan is running
    await scanner.start_scan([root], scope={root: [f"{root}/a.mod"]})
    await scanner.start_scan([root], scope={root: [f"{root}/b.mod"]})
    assert len(scanner._scan_queue) == 1
    assert scanner._scan_queue[0][2] == {root: frozenset({f"{root}/a.mod", f"{root}/b.mod"})}
    monkeypatch.setattr(scanner, "_SCOPED_MERGE_MAX", 2)
    await scanner.start_scan([root], scope={root: [f"{root}/c.mod"]})
    assert scanner._scan_queue == [(frozenset({root}), None, None, True)], "an oversized merge → full"
    await scanner.start_scan([root], scope={root: [f"{root}/d.mod"]})
    assert scanner._scan_queue == [(frozenset({root}), None, None, True)]


@pytest.mark.asyncio
async def test_a_running_scoped_scan_never_swallows_a_full_request(monkeypatch, tmp_path):
    root = str(tmp_path.resolve())
    monkeypatch.setattr(scanner, "_scan_queue", [])
    monkeypatch.setattr(scanner, "_scan_task", _Busy())
    monkeypatch.setattr(scanner, "_current_scan_dirs", frozenset({root}))
    monkeypatch.setattr(scanner, "_current_scan_scoped", True)
    assert not scanner.full_scan_active()
    await scanner.start_scan([root])
    assert scanner._scan_queue == [(frozenset({root}), None, None, False)]
    assert scanner.full_scan_active()
    # a running FULL scan dedupes a manual request, but not the watcher's
    scanner._scan_queue.clear()
    monkeypatch.setattr(scanner, "_current_scan_scoped", False)
    await scanner.start_scan([root])
    assert scanner._scan_queue == []
    await scanner.start_scan([root], rescan_if_running=True)
    assert scanner._scan_queue == [(frozenset({root}), None, None, False)]


@pytest.mark.asyncio
async def test_scope_paths_are_not_resolved_through_symlinks(monkeypatch, tmp_path):
    """A symlinked file is indexed under its in-library path; resolving it
    would scan the target, which can live outside the root."""
    lib, outside = tmp_path / "lib", tmp_path / "elsewhere"
    lib.mkdir(), outside.mkdir()
    (outside / "real.mod").write_bytes(b"x")
    (lib / "link.mod").symlink_to(outside / "real.mod")
    root = str(lib.resolve())
    monkeypatch.setattr(scanner, "_scan_queue", [])
    monkeypatch.setattr(scanner, "_scan_task", _Busy())
    await scanner.start_scan([root], scope={root: [f"{root}/link.mod", str(outside / "stray.mod")]})
    assert scanner._scan_queue[0][2] == {root: frozenset({f"{root}/link.mod"})}


# ── Incremental duplicate grouping == the full pass ─────────────────────────

@pytest.mark.asyncio
async def test_incremental_duplicate_regrouping_matches_the_full_pass(monkeypatch, tmp_data_dir):
    from soniqboom.core import store as store_mod
    from soniqboom.core.duplicates import compute_duplicate_groups
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    monkeypatch.setattr(store_mod, "_store", st)
    rng = random.Random(16)
    titles = ["Yellow", "Clocks", "Fix You", "Speed of Sound", ""]
    artists = ["Coldplay", "COLDPLAY!", "Keane", ""]
    fmts = [("FLAC", None), ("MP3", 320_000), ("MP3", 128_000), ("Ogg Vorbis", None), ("ProTracker", None)]

    def mk(i):
        f, br = rng.choice(fmts)
        return {"id": f"t{i}", "path": f"/m/{i}.x", "title": rng.choice(titles),
                "artist": rng.choice(artists), "album_artist": "",
                "duration": rng.choice([0, 0, 3.2, 180.0, 181.9, 184.9, 185.1, 240.0]),
                "format": f, "bitrate": br, "added_at": 1000 + i}
    st.upsert_tracks_batch([mk(i) for i in range(400)])
    ann = compute_duplicate_groups(list(st._tracks.values()))
    st.update_track_fields_batch([(tid, dict(a)) for tid, a in ann.items()])
    for rnd in range(6):
        delta = {}
        for tid in rng.sample(sorted(st._tracks), 15):             # change
            old = st._tracks[tid]
            new = {**mk(int(tid[1:])), "id": tid,
                   "duplicate_group_id": old.get("duplicate_group_id"),
                   "format_score": old.get("format_score"),
                   "is_duplicate_primary": old.get("is_duplicate_primary")}
            delta[tid] = old
            st.upsert_tracks_batch([new])
        for tid in rng.sample(sorted(set(st._tracks) - set(delta)), 5):   # delete
            delta[tid] = st._tracks[tid]
            st.delete_track_ids([tid])
        for i in range(3):                                          # add
            t = mk(10_000 + rnd * 10 + i)
            delta[t["id"]] = None
            st.upsert_tracks_batch([t])
        assert await scanner._run_duplicate_detection_incremental(delta) is True
        expect = compute_duplicate_groups(list(st._tracks.values()))
        got = {tid: {k: t.get(k) for k in ("duplicate_group_id", "format_score", "is_duplicate_primary")}
               for tid, t in st._tracks.items()}
        assert got == expect, f"round {rnd}: incremental grouping diverged from the full pass"


# ── Real observer end to end ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_real_observer_reads_never_scan_and_a_new_file_scans_scoped(library, monkeypatch):
    """The real watchdog backend (inotify on Linux — where reads emit
    ``opened`` / ``closed_no_write`` — FSEvents on macOS) end to end."""
    root, store, _ = library
    await scanner._run_scan([str(root)])
    calls: list = []
    real_start = scanner.start_scan

    async def spy(dirs, on_progress=None, *, scope=None, rescan_if_running=False, light=False):
        calls.append((sorted(dirs), scope))
        return await real_start(dirs, on_progress, scope=scope, rescan_if_running=rescan_if_running, light=light)
    monkeypatch.setattr(scanner, "start_scan", spy)
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda *a, **k: None)
    monkeypatch.setattr(watcher, "_QUIET_SEC", 0.4)
    for attr, val in (("enabled", False), ("observer", None), ("watches", {}),
                      ("pending", {}), ("debounce_task", None), ("loop", None)):
        monkeypatch.setattr(watcher._state, attr, val)
    await watcher.start([str(root)])
    try:
        await asyncio.sleep(0.5)
        for f in sorted(root.rglob("*.*")):          # what playback does
            with open(f, "rb") as fh:
                fh.read()
        await asyncio.sleep(2.0)
        assert calls == [], f"reading files triggered a rescan: {calls}"

        make_mod(root / "B" / "new.mod")
        for _ in range(100):
            await asyncio.sleep(0.1)
            if calls and (scanner._scan_task is None or scanner._scan_task.done()):
                break
        assert calls and calls[0][1] is not None, f"expected a scoped scan, got {calls}"
        assert "B/new.mod" in _paths(store)
    finally:
        await watcher.stop()


# ── Extractor: a uade prefix token never overrides an owned extension ───────

@pytest.mark.parametrize("name", ["one.wav", "Two.wav", "P10.wav"])
def test_prefix_named_plain_audio_is_extracted_by_its_own_extension(tmp_path, name):
    """``one.`` / ``two.`` / ``p10.`` are Amiga prefix tokens; before the fix
    these files were probed with uade, rejected, and never indexed."""
    from soniqboom.core import metadata
    f = make_wav(tmp_path / name, secs=2.0)
    meta = metadata.extract(f, "t")
    assert meta.format == "WAV" and meta.duration > 1.5


def test_a_sid_is_decided_by_its_content_whatever_its_name(tmp_path):
    from soniqboom.core import metadata
    c64 = make_psid(tmp_path / "Fred.sid")
    assert metadata.extract(c64, "t").format == "SID"
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "Fred.sid").write_bytes(bytes(random.Random(1).getrandbits(8) for _ in range(4096)))
    if shutil.which("uade123") is None:
        pytest.skip("uade123 not installed")
    with pytest.raises(ValueError):
        metadata.extract(junk / "Fred.sid", "t")


def test_every_other_engines_extension_is_owned_elsewhere():
    """A prefix-named file with ANY extension another engine handles must not
    be claimed by uade (the PSF family was missing: ``one.ssf`` was rejected)."""
    from soniqboom.core import metadata, uade_formats
    sup = {e.lower().lstrip(".") for e in metadata.SUPPORTED_EXTENSIONS}
    uade = {e.lower().lstrip(".") for e in metadata._UADE_SUFFIX_EXTS}
    assert sorted(e for e in sup - uade if not uade_formats.ext_owned_elsewhere(e)) == []


# ── Round 2: groups follow every change; queue / root / spelling edge cases ─

def _dup(store, tid):
    t = store._tracks[tid]
    return t.get("duplicate_group_id"), t.get("is_duplicate_primary")


@pytest.mark.asyncio
async def test_a_tag_edit_outside_a_scan_regroups_duplicates(library, monkeypatch):
    """Two copies of "Song" (one group, the better one primary); retitling the
    primary outside any scan must make the other copy visible again."""
    root, store, _ = library
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", scanner._run_duplicate_detection_async)
    make_wav(root / "A" / "x.wav", secs=6.0)
    make_wav(root / "A" / "y.wav", secs=6.0)
    await scanner._run_scan([str(root)])
    x, y = (next(t["id"] for t in store._tracks.values() if t["path"].endswith(n)) for n in ("x.wav", "y.wav"))
    store.update_track_fields_batch([(x, {"title": "Song", "artist": "Band"}),
                                     (y, {"title": "Song", "artist": "Band", "bitrate": 1})])
    await scanner._regroup_duplicates_now()
    assert _dup(store, x)[0] is not None and _dup(store, x)[0] == _dup(store, y)[0]
    assert {_dup(store, x)[1], _dup(store, y)[1]} == {True, False}

    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 0.05)
    scanner.install_dup_regroup()
    try:
        store.update_track_fields(x, {"title": "Another Song"})     # e.g. /api/tracks tag edit
        for _ in range(60):
            await asyncio.sleep(0.05)
            if _dup(store, y) == (None, True):
                break
        assert _dup(store, y) == (None, True) and _dup(store, x) == (None, True)
    finally:
        store._on_dup_dirty = None


@pytest.mark.asyncio
async def test_deleting_a_group_member_regroups_the_survivor_in_a_scan(library, monkeypatch):
    root, store, _ = library
    make_wav(root / "A" / "x.wav", secs=6.0)
    make_wav(root / "B" / "x.wav", secs=6.0)
    await scanner._run_scan([str(root)])
    a, b = (next(t["id"] for t in store._tracks.values() if t["path"].endswith(n))
            for n in ("A/x.wav", "B/x.wav"))
    assert _dup(store, a)[0] is not None and _dup(store, a)[0] == _dup(store, b)[0]
    (root / "A" / "x.wav").unlink()
    assert await scanner._run_scan([str(root)], scope=_scope(root, "A/x.wav")) is True
    assert _dup(store, b) == (None, True)


@pytest.mark.asyncio
async def test_tied_duplicates_pick_the_same_primary_incrementally_and_in_full(monkeypatch, tmp_data_dir):
    from soniqboom.core import store as store_mod
    from soniqboom.core.duplicates import compute_duplicate_groups
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    monkeypatch.setattr(store_mod, "_store", st)
    ids = [f"t{i:02}" for i in range(12)]
    rng = random.Random(7)
    rng.shuffle(ids)                                   # insertion order ≠ id order
    st.upsert_tracks_batch([{"id": tid, "path": f"/m/{tid}.mod", "title": "Intro", "artist": "",
                             "duration": 120.0, "format": "ProTracker", "added_at": 5}
                            for tid in ids])
    delta = {tid: None for tid in ids}
    assert await scanner._run_duplicate_detection_incremental(delta) is True
    expect = compute_duplicate_groups(list(reversed(list(st._tracks.values()))))
    assert {tid: st._tracks[tid]["is_duplicate_primary"] for tid in ids} == \
           {tid: expect[tid]["is_duplicate_primary"] for tid in ids}
    assert st._tracks["t00"]["is_duplicate_primary"] is True


@pytest.mark.asyncio
async def test_a_touch_after_later_passes_wrote_fields_is_still_unchanged(library):
    root, store, stats = library
    await scanner._run_scan([str(root)])
    tid = next(t["id"] for t in store._tracks.values() if t["path"].endswith("three.mod"))
    store.update_track_fields(tid, {"duration": 123.45, "cover_art": f"/api/art/{tid}",
                                    "stil": "Composed on a Tuesday"})
    stats.update(full=0, incr=0, agg=0)
    seq0 = store._mutation_seq
    later = time.time() + 30
    os.utime(root / "B" / "three.mod", (later, later))
    assert await scanner._run_scan([str(root)]) is False
    t = store._tracks[tid]
    assert (t["duration"], t["stil"]) == (123.45, "Composed on a Tuesday")
    assert stats == {"full": 0, "incr": 0, "agg": 0}
    assert store._mutation_seq == seq0, "an mtime refresh re-cooled every seq-keyed cache"
    # …but a field only the stored track has, that no later pass writes, is a change
    store._tracks[tid]["mystery"] = 1
    os.utime(root / "B" / "three.mod", (later + 5, later + 5))
    assert await scanner._run_scan([str(root)]) is True


@pytest.mark.asyncio
async def test_a_queued_watcher_scan_never_brings_a_removed_root_back(library):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    store._scan_dirs.pop(str(root))                     # the user removed the folder
    for tid in list(store._tracks):
        store.delete_track_ids([tid])
    make_mod(root / "B" / "late.mod")
    await scanner._run_scan([str(root)], scope=_scope(root, "B/late.mod"))
    assert str(root) not in store._scan_dirs and _paths(store) == []


@pytest.mark.asyncio
async def test_a_scoped_scan_keeps_the_last_scan_summary(library):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    scanner._progress.last_plan = {"skipped": 16027, "refreshed": 0}
    await scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav"))
    assert scanner.get_progress().last_plan == {"skipped": 16027, "refreshed": 0}


@pytest.mark.asyncio
async def test_the_root_itself_in_a_scope_queues_a_full_scan(monkeypatch, tmp_path):
    root = str(tmp_path.resolve())
    monkeypatch.setattr(scanner, "_scan_queue", [])
    monkeypatch.setattr(scanner, "_scan_task", _Busy())
    await scanner.start_scan([root], scope={root: [root, f"{root}/x.mod"]})
    assert (frozenset({root}), None, None, True) in scanner._scan_queue


def test_a_huge_raw_burst_folds_into_its_folders(monkeypatch, tmp_path):
    root = str(tmp_path)
    monkeypatch.setattr(watcher, "_SCOPE_HARD_MAX", 50)
    monkeypatch.setattr(watcher, "_SCOPE_MAX", 10)
    monkeypatch.setattr(watcher._state, "pending", {})
    monkeypatch.setattr(watcher._state, "watches", {root: object()})
    monkeypatch.setattr(watcher, "_schedule_debounce", lambda: None)
    watcher._note_change(root, [f"{root}/Big/{i:04}.flac" for i in range(200)])
    assert set(watcher._state.pending[root]) == {f"{root}/Big"}


def test_widen_keeps_top_level_entries_and_widens_the_rest(monkeypatch):
    monkeypatch.setattr(watcher, "_SCOPE_MAX", 3)
    r = "/r"
    got = watcher._widen({f"{r}/top.mod": 1.0, **{f"{r}/A/B/{i}.mod": 2.0 for i in range(5)}}, r)
    assert got == {f"{r}/top.mod": 1.0, f"{r}/A/B": 2.0}


def test_canonical_path_matches_unicode_forms(tmp_path):
    import unicodedata
    nfd = unicodedata.normalize("NFD", "Müsic")
    (tmp_path / nfd).mkdir()
    base = str(tmp_path.resolve())
    on_disk = next(n for n in os.listdir(base) if unicodedata.normalize("NFC", n) == "Müsic")
    got = watcher._canonical_path(os.path.join(base, unicodedata.normalize("NFC", "Müsic")))
    assert os.path.basename(got) == on_disk and os.path.isdir(got)


def test_appledouble_and_junk_names_never_count(captured, tmp_path):
    h = watcher._Handler(str(tmp_path))
    h.on_any_event(FileCreatedEvent(str(tmp_path / "A" / "._01.flac")))
    assert captured == []


@pytest.mark.asyncio
async def test_startup_reconcile_rescans_local_roots_unless_switched_off(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    calls = []

    async def fake_start(dirs, *a, **k):
        calls.append(sorted(dirs))
    monkeypatch.setattr(scanner, "start_scan", fake_start)
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "startup_reconcile_scan", False)
    scanner.schedule_startup_reconcile()
    await asyncio.sleep(0.05)
    assert calls == []
    monkeypatch.setattr(settings, "startup_reconcile_scan", True)
    scanner.schedule_startup_reconcile()
    await asyncio.sleep(0.05)
    assert calls == [[str(root)]]


@pytest.mark.asyncio
async def test_watcher_runs_without_roots_so_a_root_added_later_is_armed(monkeypatch, tmp_path):
    for attr, val in (("enabled", False), ("observer", None), ("watches", {}),
                      ("pending", {}), ("debounce_task", None), ("loop", None)):
        monkeypatch.setattr(watcher._state, attr, val)
    await watcher.start([])
    try:
        await watcher.add_root(str(tmp_path))
        assert str(tmp_path.resolve()) in watcher._state.watches
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_inotify_watches_a_folder_moved_in_from_outside(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    calls: list = []

    async def spy(dirs, on_progress=None, *, scope=None, rescan_if_running=False, light=False):
        calls.append({k: sorted(v) for k, v in (scope or {}).items()})
    monkeypatch.setattr(scanner, "start_scan", spy)
    monkeypatch.setattr(watcher, "_QUIET_SEC", 0.3)
    for attr, val in (("enabled", False), ("observer", None), ("watches", {}),
                      ("pending", {}), ("debounce_task", None), ("loop", None)):
        monkeypatch.setattr(watcher._state, attr, val)
    await watcher.start([str(root)])
    try:
        if type(watcher._state.observer).__name__ != "InotifyObserver":
            pytest.skip("inotify only (FSEvents watches the whole tree)")
        outside = root.parent / "incoming" / "NewAlbum"
        (outside / "CD1").mkdir(parents=True)
        await asyncio.sleep(0.3)
        shutil.move(str(outside), str(root / "NewAlbum"))
        await asyncio.sleep(1.2)
        calls.clear()
        make_mod(root / "NewAlbum" / "CD1" / "later.mod")
        await asyncio.sleep(1.2)
        assert calls and any(str(root / "NewAlbum" / "CD1" / "later.mod") in v
                             for c in calls for v in c.values()), calls
    finally:
        await watcher.stop()


def test_archive_members_are_fresh_when_the_archive_is_unchanged(tmp_path):
    """A member stores its own size; the stat is the archive's.  Comparing the
    two made every member look changed on every full scan."""
    import uuid
    z = tmp_path / "pack.zip"
    z.write_bytes(b"x" * 5000)
    st = os.stat(z)
    member = f"{z}::tune.mod"
    tid = str(uuid.uuid5(uuid.NAMESPACE_URL, member))
    fresh, _ = scanner._compute_incremental([member], {tid: (st.st_mtime, 1234)})
    assert member in fresh
    fresh, _ = scanner._compute_incremental([member], {tid: (st.st_mtime - 60, 1234)})
    assert member not in fresh, "a rewritten archive (new mtime) must be re-read"
    plain = str(tmp_path / "x.mod")
    Path(plain).write_bytes(b"y" * 10)
    pst = os.stat(plain)
    ptid = str(uuid.uuid5(uuid.NAMESPACE_URL, plain))
    assert plain not in scanner._compute_incremental([plain], {ptid: (pst.st_mtime, 11)})[0]


@pytest.mark.asyncio
async def test_a_second_full_scan_reads_nothing_inside_an_unchanged_archive(library, caplog):
    import logging
    import zipfile
    root, store, _ = library
    make_mod(root / "B" / "inner.mod", title=b"inner")
    with zipfile.ZipFile(root / "B" / "pack.zip", "w") as zf:
        zf.write(root / "B" / "inner.mod", "pack/inner.mod")
    (root / "B" / "inner.mod").unlink()
    await scanner._run_scan([str(root)])
    assert any("::" in t["path"] for t in store._tracks.values())
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="soniqboom.core.scanner"):
        assert await scanner._run_scan([str(root)]) is False
    assert any("0 files to scan" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records if "to scan" in r.getMessage()]



# ── Round 3 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_same_size_sid_rewrite_is_a_change(library):
    root, store, _ = library
    make_psid(root / "B" / "tune.sid", name=b"Same", songs=1)
    await scanner._run_scan([str(root)])
    tid = next(t["id"] for t in store._tracks.values() if t["path"].endswith("tune.sid"))
    md5_0 = store._tracks[tid].get("sid_md5")
    size0 = os.path.getsize(root / "B" / "tune.sid")
    # same size, same tags — only the tune count (a post-scan-owned field) and
    # therefore the SID's own hash differ
    make_psid(root / "B" / "tune.sid", name=b"Same", songs=3)
    assert os.path.getsize(root / "B" / "tune.sid") == size0
    later = time.time() + 30
    os.utime(root / "B" / "tune.sid", (later, later))
    assert await scanner._run_scan([str(root)], scope=_scope(root, "B/tune.sid")) is True
    t = store._tracks[tid]
    assert t.get("sid_md5") and t.get("sid_md5") != md5_0 and t.get("subsongs") == 3


@pytest.mark.asyncio
async def test_a_defect_cleared_by_re_extraction_is_a_change(library):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    tid = next(t["id"] for t in store._tracks.values() if t["path"].endswith("three.mod"))
    store.update_track_fields(tid, {"defect": "partial", "defect_detail": "1 instrument substituted"})
    later = time.time() + 30
    os.utime(root / "B" / "three.mod", (later, later))
    assert await scanner._run_scan([str(root)]) is True
    assert not store._tracks[tid].get("defect")


@pytest.mark.asyncio
async def test_pending_regroups_survive_a_restart(library, monkeypatch):
    root, store, stats = library
    await scanner._run_scan([str(root)])
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 30.0)      # "shut down" before it runs
    scanner.install_dup_regroup()
    try:
        tid = next(iter(store._tracks))
        store.update_track_fields(tid, {"title": "Renamed"})
        assert store.get_config(scanner._DUP_PENDING_KEY) is True
    finally:
        # a crash: the runner dies without the clean-shutdown save
        if scanner._dup_runner_task is not None:
            scanner._dup_runner_task.cancel()
        store._on_dup_dirty = None
    assert not store.get_config(scanner._DUP_PENDING_DELTA_KEY)
    # next start: the flag alone forces one full pass
    store.take_dup_dirty()
    stats.update(full=0, incr=0, agg=0)
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 0.01)
    monkeypatch.setattr(scanner, "_dup_full_last", float("-inf"))   # a fresh process
    scanner.install_dup_regroup()
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if stats["full"]:
                break
        assert stats["full"] == 1
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not store.get_config(scanner._DUP_PENDING_KEY):
                break
        assert store.get_config(scanner._DUP_PENDING_KEY) is False
    finally:
        scanner.cancel_background_tasks()
        store._on_dup_dirty = None


@pytest.mark.asyncio
async def test_a_failed_regroup_puts_the_work_back(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])

    async def boom():
        raise RuntimeError("worker died")
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", boom)
    store._dup_dirty_overflow = True
    with pytest.raises(RuntimeError):
        await scanner._regroup_duplicates_now()
    assert store._dup_dirty_overflow is True


@pytest.mark.asyncio
async def test_regrouping_waits_while_a_batch_holds_the_sorted_indexes(library, monkeypatch):
    root, store, stats = library
    await scanner._run_scan([str(root)])
    tid = next(iter(store._tracks))
    store.update_track_fields(tid, {"title": "Held"})
    monkeypatch.setattr(scanner, "_ensure_dup_runner", lambda: None)
    store._batch_mode = True
    try:
        await scanner._regroup_duplicates_now()
        assert tid in store._dup_dirty, "the pending change was consumed during a batch"
    finally:
        store._batch_mode = False


@pytest.mark.asyncio
async def test_losing_the_annotation_on_upsert_marks_the_group_dirty(monkeypatch, tmp_data_dir):
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    t = {"id": "a", "path": "/m/a.mod", "title": "X", "duration": 100.0, "format": "MOD",
         "duplicate_group_id": "g1", "is_duplicate_primary": False, "format_score": 50}
    st.upsert_tracks_batch([dict(t)])
    st.take_dup_dirty()
    st.upsert_tracks_batch([{k: v for k, v in t.items() if k not in ("duplicate_group_id",
                                                                      "is_duplicate_primary",
                                                                      "format_score")}])
    assert "a" in st._dup_dirty


def test_nan_durations_stay_out_of_the_sorted_duration_index(tmp_data_dir):
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    st.upsert_tracks_batch([{"id": "n", "path": "/m/n.mod", "title": "N", "duration": float("nan")},
                            {"id": "k", "path": "/m/k.mod", "title": "K", "duration": 120.0}])
    assert [tid for _d, tid in st._sorted_duration] == ["k"]
    st.delete_track_ids(["n", "k"])
    assert st._sorted_duration == []


@pytest.mark.asyncio
async def test_startup_reconcile_is_not_skipped_for_a_remote_scan(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    calls = []

    async def fake_start(dirs, *a, **k):
        calls.append(sorted(dirs))
    monkeypatch.setattr(scanner, "start_scan", fake_start)
    monkeypatch.setattr(scanner, "_current_remote_dirs", {"smb://nas/music"})
    scanner.schedule_startup_reconcile()
    await asyncio.sleep(0.1)
    assert calls == [[str(root)]]


@pytest.mark.asyncio
async def test_a_removed_root_leaves_the_scan_queue(monkeypatch, tmp_path):
    a, b = str((tmp_path / "a").resolve()), str((tmp_path / "b").resolve())
    monkeypatch.setattr(scanner, "_scan_queue", [(frozenset({a, b}), None, None, True),
                                                 (frozenset({a}), None, {a: frozenset({a + "/x"})}, False)])
    scanner.forget_root(a)
    assert scanner._scan_queue == [(frozenset({b}), None, None, True)]


@pytest.mark.asyncio
async def test_folder_browse_rows_follow_an_in_place_retag(library):
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    _paths_c, dicts = fstree._get_or_build_scan_root_sorted(store, path_hash(str(root)))
    assert any(d.get("title") == "synthetic" for d in dicts)
    make_mod(root / "B" / "three.mod", title=b"retagged!")        # same size, new title
    later = time.time() + 30
    os.utime(root / "B" / "three.mod", (later, later))
    assert await scanner._run_scan([str(root)], scope=_scope(root, "B/three.mod")) is True
    _paths_c, dicts = fstree._get_or_build_scan_root_sorted(store, path_hash(str(root)))
    assert any(d.get("title") == "retagged!" for d in dicts), "the folder view kept the old title"


@pytest.mark.asyncio
async def test_deleting_a_folder_that_only_holds_folders_prunes_them(library):
    root, store, _ = library
    (root / "Artist" / "Album").mkdir(parents=True)
    make_mod(root / "Artist" / "Album" / "x.mod")
    await scanner._run_scan([str(root)])
    assert "Artist/Album/x.mod" in _paths(store)
    shutil.rmtree(root / "Artist")
    await scanner._run_scan([str(root)], scope=_scope(root, "Artist"))
    assert "Artist/Album/x.mod" not in _paths(store)


def test_root_is_live_only_for_a_readable_non_empty_folder(tmp_path):
    assert scanner._root_is_live(str(tmp_path)) is False
    (tmp_path / "x").write_text("1")
    assert scanner._root_is_live(str(tmp_path)) is True
    assert scanner._root_is_live(str(tmp_path / "missing")) is False


def test_a_path_outside_the_root_spelling_asks_for_a_full_rescan(monkeypatch, tmp_path):
    root = str(tmp_path)
    monkeypatch.setattr(watcher._state, "pending", {})
    monkeypatch.setattr(watcher._state, "watches", {root: object()})
    monkeypatch.setattr(watcher, "_schedule_debounce", lambda: None)
    watcher._note_change(root, [root.upper() + "/X/y.mod"])
    assert watcher._state.pending == {root: None}


def test_cover_subtree_stops_at_the_inotify_watch_limit(tmp_path, caplog, monkeypatch):
    import errno
    import logging
    (tmp_path / "A" / "B").mkdir(parents=True)
    added = []

    class _Ino:
        def add_watch(self, p):
            added.append(p)
            raise OSError(errno.ENOSPC, "No space left on device")
    h = watcher._Handler(str(tmp_path))
    monkeypatch.setattr(watcher._Handler, "_inotify", property(lambda self: _Ino()))
    with caplog.at_level(logging.WARNING, logger="soniqboom.core.watcher"):
        h._cover_subtree(str(tmp_path / "A"))
    assert len(added) == 1 and any("watch limit" in r.getMessage() for r in caplog.records)



# ── Round 3 (performance) ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_archive_named_like_an_amiga_prefix_is_listed_as_an_archive(library):
    """``ST.zip`` / ``MA.zip`` match uade prefix tokens (``st.``, ``ma.``); they
    were indexed as one broken module and their members never listed."""
    import zipfile
    root, store, _ = library
    make_mod(root / "B" / "inner.mod", title=b"inner")
    for name in ("ST.zip", "MA.zip"):
        with zipfile.ZipFile(root / "B" / name, "w") as zf:
            zf.write(root / "B" / "inner.mod", "ST-01.mod")
            with zipfile.ZipFile(root / "B" / "n.zip", "w") as nz:
                nz.write(root / "B" / "inner.mod", "deep.mod")
            zf.write(root / "B" / "n.zip", "PT.zip")          # a nested prefix-named zip
    (root / "B" / "inner.mod").unlink()
    (root / "B" / "n.zip").unlink()
    await scanner._run_scan([str(root)])
    got = _paths(store)
    assert "B/ST.zip::ST-01.mod" in got and "B/MA.zip::PT.zip::deep.mod" in got
    assert "B/ST.zip" not in got


@pytest.mark.asyncio
async def test_an_automatic_rescan_lists_unchanged_archives_without_opening_them(library, monkeypatch):
    import zipfile
    root, store, _ = library
    make_mod(root / "B" / "inner.mod", title=b"inner")
    with zipfile.ZipFile(root / "B" / "pack.zip", "w") as zf:
        zf.write(root / "B" / "inner.mod", "a.mod")
    (root / "B" / "inner.mod").unlink()
    await scanner._run_scan([str(root)])
    opened = []
    real_zip = zipfile.ZipFile

    class _Spy(real_zip):
        def __init__(self, f, *a, **k):
            opened.append(f)
            super().__init__(f, *a, **k)
    monkeypatch.setattr(zipfile, "ZipFile", _Spy)
    assert await scanner._run_scan([str(root)], light=True) is False
    assert opened == [] and "B/pack.zip::a.mod" in _paths(store)
    # a changed archive is opened again
    later = time.time() + 60
    os.utime(root / "B" / "pack.zip", (later, later))
    await scanner._run_scan([str(root)], light=True)
    assert opened, "a changed archive was not re-listed"
    # new discovery rules (an upgrade learned a format) → one full enumeration
    opened.clear()
    monkeypatch.setattr(scanner, "_ARCHIVE_LISTING_VERSION", 99)
    await scanner._run_scan([str(root)], light=True)
    assert opened, "light mode listed archives under outdated discovery rules"
    opened.clear()
    await scanner._run_scan([str(root)], light=True)
    assert opened == [], "the fingerprint was not recorded after the full enumeration"


@pytest.mark.asyncio
async def test_a_manual_scan_request_wins_over_an_automatic_one(monkeypatch, tmp_path):
    root = str(tmp_path.resolve())
    monkeypatch.setattr(scanner, "_scan_queue", [])
    monkeypatch.setattr(scanner, "_scan_task", _Busy())
    await scanner.start_scan([root], light=True)
    await scanner.start_scan([root])                        # admin "Rebuild"
    assert scanner._scan_queue == [(frozenset({root}), None, None, False)]
    await scanner.start_scan([root], light=True)            # a later automatic one
    assert scanner._scan_queue == [(frozenset({root}), None, None, False)]
    # a manual request is not deduped against a RUNNING automatic scan
    scanner._scan_queue.clear()
    monkeypatch.setattr(scanner, "_current_scan_dirs", frozenset({root}))
    monkeypatch.setattr(scanner, "_current_scan_light", True)
    assert not scanner.full_scan_active()
    await scanner.start_scan([root])
    assert scanner._scan_queue == [(frozenset({root}), None, None, False)]


@pytest.mark.asyncio
async def test_sort_yielding_equals_sorted():
    rng = random.Random(3)
    data = [(rng.random() * 300, f"t{i}") for i in range(70_000)]
    assert await scanner._sort_yielding(list(data), run=8_000) == sorted(data)
    mixed = [(1, "a"), ("x", "b")] * 20_000
    with pytest.raises(TypeError):
        await scanner._sort_yielding(list(mixed), run=8_000)


def test_bucket0_is_a_numeric_test():
    for d, want in ((0, True), (None, True), (4.99, True), (5.0, False), (180.0, False),
                    (float("nan"), True), (float("inf"), True), ("abc", True), (-3, True)):
        assert scanner._in_bucket0({"duration": d}) is want, d



# ── Round 4 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_moved_file_leaves_no_ghost_in_folder_listings(library):
    """A move is one delete + one add: the root's COUNT is unchanged, so the
    count-validated per-folder listings kept the old rows."""
    from soniqboom.api import fstree
    root, store, _ = library
    await scanner._run_scan([str(root)])
    before_a = [d["path"].rsplit("/", 1)[1] for d in fstree._store_recursive_tracks_under(store, root / "A")]
    before_b = [d["path"].rsplit("/", 1)[1] for d in fstree._store_recursive_tracks_under(store, root / "B")]
    assert "one.wav" in before_a and "one.wav" not in before_b
    (root / "A" / "one.wav").rename(root / "B" / "one.wav")
    await scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav", "B/one.wav"))
    after_a = [d["path"].rsplit("/", 1)[1] for d in fstree._store_recursive_tracks_under(store, root / "A")]
    after_b = [d["path"].rsplit("/", 1)[1] for d in fstree._store_recursive_tracks_under(store, root / "B")]
    assert "one.wav" not in after_a and "one.wav" in after_b


@pytest.mark.asyncio
async def test_listings_follow_a_retag_after_the_root_cache_was_dropped(library):
    from soniqboom.api import fstree
    root, store, _ = library
    await scanner._run_scan([str(root)])
    fstree._store_recursive_tracks_under(store, root / "B")            # listing built
    (root / "A" / "one.wav").rename(root / "B" / "moved.wav")           # drops the root cache
    await scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav", "B/moved.wav"))
    fstree._store_recursive_tracks_under(store, root / "B")
    make_mod(root / "B" / "three.mod", title=b"retitled")               # then an in-place retag
    later = time.time() + 30
    os.utime(root / "B" / "three.mod", (later, later))
    await scanner._run_scan([str(root)], scope=_scope(root, "B/three.mod"))
    titles = [d.get("title") for d in fstree._store_recursive_tracks_under(store, root / "B")]
    assert "retitled" in titles, titles


@pytest.mark.asyncio
async def test_prefix_named_lha_and_disk_images_are_archives(library, monkeypatch):
    from soniqboom.core import archive, diskimage
    root, store, _ = library
    for n in ("ST.lha", "MA.adf", "game.d64"):
        (root / "B" / n).write_bytes(b"\0" * 64)
    listed = []
    monkeypatch.setattr(archive, "list_members",
                        lambda full, **k: (listed.append(os.path.basename(full)), ["ST-01.mod"])[1])
    monkeypatch.setattr(diskimage, "list_members",
                        lambda full, **k: (listed.append(os.path.basename(full)), ["tune.sid"])[1])
    files, _errs = scanner._find_audio_files([str(root)])
    names = {str(p)[len(str(root)) + 1:] for p in files[str(root)]}
    assert sorted(listed) == ["MA.adf", "ST.lha", "game.d64"]
    assert "B/ST.lha::ST-01.mod" in names and "B/MA.adf::tune.sid" in names
    assert "B/ST.lha" not in names and "B/MA.adf" not in names


@pytest.mark.asyncio
async def test_a_corrupt_nested_zip_neither_aborts_the_scan_nor_prunes(library, monkeypatch):
    import zipfile
    root, store, _ = library
    make_mod(root / "B" / "inner.mod", title=b"inner")
    with zipfile.ZipFile(root / "B" / "n.zip", "w") as nz:
        nz.write(root / "B" / "inner.mod", "deep.mod")
    with zipfile.ZipFile(root / "B" / "pack.zip", "w") as zf:
        zf.write(root / "B" / "n.zip", "nested.zip")
        zf.write(root / "B" / "inner.mod", "top.mod")
    for f in ("inner.mod", "n.zip"):
        (root / "B" / f).unlink()
    await scanner._run_scan([str(root)])
    assert "B/pack.zip::nested.zip::deep.mod" in _paths(store)
    real_read = zipfile.ZipFile.read

    def bad_read(self, name, *a, **k):
        if name == "nested.zip":
            import zlib
            raise zlib.error("Error -3 while decompressing data")
        return real_read(self, name, *a, **k)
    monkeypatch.setattr(zipfile.ZipFile, "read", bad_read)
    listed, _errs = scanner._find_audio_files([str(root)])   # the archive's other members still list
    assert str(root / "B" / "pack.zip") + "::top.mod" in {str(p) for p in listed[str(root)]}
    await scanner._run_scan([str(root)])                    # must not raise
    got = _paths(store)
    assert "A/one.wav" in got and "B/pack.zip::nested.zip::deep.mod" in got


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions as a non-root user")
@pytest.mark.asyncio
async def test_an_unreadable_archive_keeps_its_members(library):
    import zipfile
    root, store, _ = library
    make_mod(root / "B" / "inner.mod", title=b"inner")
    with zipfile.ZipFile(root / "B" / "pack.zip", "w") as zf:
        zf.write(root / "B" / "inner.mod", "a.mod")
    (root / "B" / "inner.mod").unlink()
    await scanner._run_scan([str(root)])
    os.chmod(root / "B" / "pack.zip", 0)
    try:
        await scanner._run_scan([str(root)])
    finally:
        os.chmod(root / "B" / "pack.zip", 0o644)
    assert "B/pack.zip::a.mod" in _paths(store)


@pytest.mark.asyncio
async def test_a_root_removed_during_its_scan_is_not_brought_back(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    make_mod(root / "B" / "late.mod")
    real = scanner._find_audio_files

    def remove_meanwhile(*a, **k):
        out = real(*a, **k)
        store._scan_dirs.pop(str(root), None)               # the user removed the folder
        return out
    monkeypatch.setattr(scanner, "_find_audio_files", remove_meanwhile)
    await scanner._run_scan([str(root)])
    assert str(root) not in store._scan_dirs
    assert "B/late.mod" not in _paths(store)


@pytest.mark.asyncio
async def test_known_archive_members_drop_archives_with_mixed_member_mtimes(tmp_data_dir):
    from soniqboom.core.data import path_hash
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    root = "/lib"
    h = path_hash(root)
    st.upsert_tracks_batch([
        {"id": "a", "path": "/lib/x.zip::a.mod", "mtime": 100.0, "scan_root_hash": h, "title": "a"},
        {"id": "b", "path": "/lib/x.zip::b.mod", "mtime": 100.2, "scan_root_hash": h, "title": "b"},
        {"id": "c", "path": "/lib/y.zip::c.mod", "mtime": 100.0, "scan_root_hash": h, "title": "c"},
        {"id": "d", "path": "/lib/y.zip::d.mod", "mtime": 300.0, "scan_root_hash": h, "title": "d"},
    ])
    known = await scanner._known_archive_members(st, [root])
    assert abs(known["/lib/x.zip"][0] - 100.0) < 1 and sorted(known["/lib/x.zip"][1]) == [
        "/lib/x.zip::a.mod", "/lib/x.zip::b.mod"]
    assert known["/lib/y.zip"][0] is None


@pytest.mark.asyncio
async def test_duplicate_annotation_writes_refresh_folder_rows(library):
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    tid = next(t["id"] for t in store._tracks.values() if t["path"].endswith("three.mod"))
    fstree._get_or_build_scan_root_sorted(store, path_hash(str(root)))
    await scanner._apply_duplicate_annotations(
        {tid: {"duplicate_group_id": "g9", "format_score": 50, "is_duplicate_primary": False}})
    _p, dicts = fstree._get_or_build_scan_root_sorted(store, path_hash(str(root)))
    row = next(d for d in dicts if d["id"] == tid)
    assert row.get("duplicate_group_id") == "g9" and row.get("is_duplicate_primary") is False


@pytest.mark.asyncio
async def test_cancel_background_tasks_stops_the_regroup_runner(library, monkeypatch):
    root, store, _ = library
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 30.0)
    store._dup_dirty_overflow = True
    scanner._ensure_dup_runner()
    task = scanner._dup_runner_task
    assert task is not None and not task.done()
    scanner.cancel_background_tasks()
    await asyncio.sleep(0.01)
    assert task.cancelled() or task.done()
    store._dup_dirty_overflow = False


@pytest.mark.asyncio
async def test_regroups_are_serialized(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    active, peak = [0], [0]

    async def slow_full():
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.05)
        active[0] -= 1
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", slow_full)

    async def one():
        store._dup_dirty_overflow = True
        await scanner._regroup_duplicates_now()
    await asyncio.gather(one(), one(), one())
    assert peak[0] == 1


@pytest.mark.asyncio
async def test_background_regroup_waits_for_a_running_scan(library, monkeypatch):
    root, store, stats = library
    await scanner._run_scan([str(root)])
    stats.update(full=0, incr=0, agg=0)
    fut = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(scanner, "_scan_task", fut)          # "a scan is running"
    monkeypatch.setattr(scanner, "_dup_full_last", float("-inf"))
    store._dup_dirty_overflow = True
    job = asyncio.create_task(scanner._regroup_duplicates_now(background=True))
    await asyncio.sleep(0.1)
    assert stats["full"] == 0, "re-grouped while a scan was running"
    fut.set_result(None)
    await asyncio.wait_for(job, 5)
    assert stats["full"] == 1


@pytest.mark.asyncio
async def test_the_batch_exit_rebuild_keeps_nan_out_of_the_duration_index(tmp_data_dir):
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    st.enter_batch_mode()
    st.upsert_tracks_batch([{"id": "n", "path": "/m/n.mod", "title": "N", "duration": float("nan")},
                            {"id": "k", "path": "/m/k.mod", "title": "K", "duration": 120.0}])
    await scanner._async_exit_batch_mode(st)
    assert [tid for _d, tid in st._sorted_duration] == ["k"]


def test_remote_walker_treats_archive_names_as_archives():
    """The remote walker's name test: an archive name never takes the audio branch."""
    import inspect
    src = inspect.getsource(scanner)
    assert ("if (is_supported_music_name(fe.name)\n"
            "                        and not (scan_zips and fe.name.lower().endswith((\".zip\", \".lha\", \".lzh\")))):"
            ) in src



# ── Round 4 (performance) ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_move_patches_the_root_browse_cache_in_place(library):
    """The next folder click must not rebuild the whole root (seconds on a big
    one): the cached rows are patched, and equal a fresh build."""
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    h = path_hash(str(root))
    fstree._get_or_build_scan_root_sorted(store, h)
    (root / "A" / "one.wav").rename(root / "B" / "one.wav")
    make_mod(root / "B" / "new.mod")
    await scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav", "B/one.wav", "B/new.mod"))
    assert h in fstree._SCAN_ROOT_FULL_CACHE, "the root cache was dropped, not patched"
    patched = fstree._SCAN_ROOT_FULL_CACHE[h]
    fstree._SCAN_ROOT_FULL_CACHE.pop(h)
    paths, dicts = fstree._get_or_build_scan_root_sorted(store, h)
    assert patched["paths"] == paths and patched["dicts"] == dicts
    assert patched["size"] == len(store._tag_scan_root_hash[h])


@pytest.mark.asyncio
async def test_patch_scan_root_rows_small_and_merge_paths_agree(tmp_data_dir):
    from soniqboom.api import fstree
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    tracks = [{"id": f"t{i}", "path": f"/r/{i:03}.mod", "title": f"T{i}", "scan_root_hash": "h"}
              for i in range(300)]
    st.upsert_tracks_batch([dict(t) for t in tracks])
    for lo, n_out, n_in in ((0, 3, 2), (10, 120, 90)):       # bisect path / merge path
        fstree._SCAN_ROOT_FULL_CACHE.pop("h", None)
        fstree._get_or_build_scan_root_sorted(st, "h")
        gone = [f"t{i}" for i in range(lo, lo + n_out)]
        removed = {st._tracks[t]["path"] for t in gone}
        st.delete_track_ids(gone)
        new = [{"id": f"n{n_out}-{i}", "path": f"/r/{i:03}b{n_out}.mod", "title": "N",
                "scan_root_hash": "h"} for i in range(n_in)]
        st.upsert_tracks_batch([dict(t) for t in new])
        assert await fstree.patch_scan_root_rows("h", removed, [st._tracks[t["id"]] for t in new],
                                                 len(st._tag_scan_root_hash["h"]))
        patched = fstree._SCAN_ROOT_FULL_CACHE["h"]
        fstree._SCAN_ROOT_FULL_CACHE.pop("h")
        paths, dicts = fstree._get_or_build_scan_root_sorted(st, "h")
        assert (patched["paths"], patched["dicts"]) == (paths, dicts), (n_out, n_in)


@pytest.mark.asyncio
async def test_a_scans_regroup_never_waits_behind_a_background_pass(library, monkeypatch):
    root, store, stats = library
    await scanner._run_scan([str(root)])
    kicked = []
    monkeypatch.setattr(scanner, "_ensure_dup_runner", lambda: kicked.append(1))
    tid = next(iter(store._tracks))
    store.update_track_fields(tid, {"title": "Pending"})
    lock = scanner._dup_lock()
    await lock.acquire()                                   # "a background full pass"
    try:
        await asyncio.wait_for(scanner._regroup_duplicates_now(), 0.5)
    finally:
        lock.release()
    assert kicked and tid in store._dup_dirty


@pytest.mark.asyncio
async def test_a_clean_shutdown_keeps_the_pending_set_for_an_incremental_start(library, monkeypatch):
    root, store, stats = library
    await scanner._run_scan([str(root)])
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 30.0)
    scanner.install_dup_regroup()
    try:
        tid = next(iter(store._tracks))
        store.update_track_fields(tid, {"title": "Renamed again"})
        scanner.cancel_background_tasks()                     # clean shutdown
    finally:
        store._on_dup_dirty = None
    saved = store.get_config(scanner._DUP_PENDING_DELTA_KEY)
    assert isinstance(saved, dict) and tid in saved
    store.take_dup_dirty()                                   # a new process
    stats.update(full=0, incr=0, agg=0)
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 0.01)
    scanner.install_dup_regroup()
    try:
        assert tid in store._dup_dirty and not store._dup_dirty_overflow
        for _ in range(100):
            await asyncio.sleep(0.02)
            if stats["incr"]:
                break
        assert stats == {"full": 0, "incr": 1, "agg": stats["agg"]}
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not store.get_config(scanner._DUP_PENDING_KEY):
                break
        assert not store.get_config(scanner._DUP_PENDING_KEY)
        assert not store.get_config(scanner._DUP_PENDING_DELTA_KEY)
    finally:
        scanner.cancel_background_tasks()
        store._on_dup_dirty = None


# ── Round 5 (QA repro tests, kept as regression tests) ─────────────────────

@pytest.mark.asyncio
async def test_unreadable_lha_keeps_members(library):
    root, store, _ = library
    lha = Path(__file__).resolve().parent.parent / "internal/testdata/archives/AHXSONGS.LHA"
    if not lha.exists() or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("needs the local LHA test archive and a non-root user")
    shutil.copy(lha, root / "B" / "songs.lha")
    await scanner._run_scan([str(root)])
    before = [p for p in _paths(store) if "songs.lha::" in p]
    assert before
    os.chmod(root / "B" / "songs.lha", 0)
    from soniqboom.core import archive as _arc
    with _arc._CACHE_LOCK:
        _arc._MAP_CACHE.clear(); _arc._OPEN_CACHE.clear()   # a fresh process (restart)
    try:
        fa = set()
        files, errs = scanner._find_audio_files([str(root)], failed_archives=fa)
        await scanner._run_scan([str(root)])
    finally:
        os.chmod(root / "B" / "songs.lha", 0o644)
    after = [p for p in _paths(store) if "songs.lha::" in p]
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_unreadable_d64_keeps_members(library, monkeypatch):
    from soniqboom.core import diskimage
    root, store, _ = library
    (root / "B" / "game.d64").write_bytes(b"\0" * 64)
    good = lambda p: {"tune.sid": b"x"}
    monkeypatch.setattr(diskimage, "_enumerate", good)
    fa = set()
    files, _ = scanner._find_audio_files([str(root)], failed_archives=fa)
    def perm(p):
        raise PermissionError(13, "Permission denied", p)
    monkeypatch.setattr(diskimage, "_enumerate", perm)
    fa = set()
    files, _ = scanner._find_audio_files([str(root)], failed_archives=fa)
    assert fa, "unreadable disk image not reported as failed"


def _fresh_b(store, h):
    from soniqboom.api import fstree
    saved = fstree._SCAN_ROOT_FULL_CACHE.pop(h, None)
    paths, dicts = fstree._get_or_build_scan_root_sorted(store, h)
    if saved is not None:
        fstree._SCAN_ROOT_FULL_CACHE[h] = saved
    return paths, dicts


@pytest.mark.asyncio
async def test_ghost_after_api_delete_then_scan_add(library):
    """DELETE /api/tracks/{id} (data.delete_track) doesn't touch fstree; the old
    size check rebuilt on the next count change, the patch blesses the stale rows."""
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash, delete_track
    root, store, _ = library
    await scanner._run_scan([str(root)])
    h = path_hash(str(root))
    fstree._store_recursive_tracks_under(store, root)           # warm (a folder click)
    victim = next(t for t in store._tracks.values() if t["path"].endswith("two.wav"))
    await delete_track(victim["id"])                             # the tracks API delete path
    make_mod(root / "B" / "n1.mod", title=b"n1")
    make_mod(root / "B" / "n2.mod", title=b"n2")
    await scanner._run_scan([str(root)], scope=_scope(root, "B/n1.mod", "B/n2.mod"))
    rows = fstree._store_recursive_tracks_under(store, root)
    ghost = [r["path"] for r in rows if r["id"] not in store._tracks]
    e = fstree._SCAN_ROOT_FULL_CACHE[h]
    assert not ghost


@pytest.mark.asyncio
async def test_rebuild_mid_commit_duplicates_rows(library, monkeypatch):
    """A folder click between the commit's upsert chunks rebuilds the root cache
    from the half-committed store; the patch then inserts the same rows again."""
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    h = path_hash(str(root))
    fstree._get_or_build_scan_root_sorted(store, h)
    for i in range(30):
        make_mod(root / "B" / f"x{i:02}.mod", title=f"x{i}".encode())
    real = store.upsert_tracks_batch
    calls = {"n": 0}

    def spy(batch, *a, **k):
        r = real(batch, *a, **k)
        calls["n"] += 1
        if calls["n"] == 1:
            fstree._store_recursive_tracks_under(store, root)   # a browse request between chunks
        return r
    monkeypatch.setattr(store, "upsert_tracks_batch", spy)
    await scanner._run_scan([str(root)], scope=_scope(root, "B"))
    # the out-of-step entry is dropped (not patched): whatever serves the next
    # click equals a fresh build, with no duplicated rows
    fstree._store_recursive_tracks_under(store, root)
    e = fstree._SCAN_ROOT_FULL_CACHE[h]
    paths, dicts = _fresh_b(store, h)
    assert e["paths"] == paths and len(e["paths"]) == len(set(e["paths"]))




@pytest.mark.asyncio
async def test_remove_purge_readd_shows_old_rows(library, tmp_path):
    """Remove a folder with purge (no fstree invalidation), retag a file, re-add:
    the patch merges the new rows into the stale entry and the listing keeps
    the pre-removal row (first by id)."""
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash, delete_scan_dir, delete_tracks_by_scan_root
    root, store, _ = library
    other = (tmp_path / "other").resolve()
    other.mkdir()
    make_wav(other / "o.wav", secs=1.5)
    await scanner._run_scan([str(other)])
    await scanner._run_scan([str(root)])
    h = path_hash(str(root))
    fstree._store_recursive_tracks_under(store, root)            # browsed once
    await delete_scan_dir(str(root))                              # DELETE /api/admin/dirs purge=true
    await delete_tracks_by_scan_root(str(root))
    make_mod(root / "B" / "three.mod", title=b"RETAGGED")         # changed while removed
    (root / "A" / "two.wav").unlink()                             # deleted while removed
    await scanner._run_scan([str(root)])                          # re-added
    rows = fstree._store_recursive_tracks_under(store, root)
    three = [r for r in rows if r["path"].endswith("three.mod")]
    ghost = [os.path.relpath(r["path"], root) for r in rows if r["id"] not in store._tracks]
    e = fstree._SCAN_ROOT_FULL_CACHE[h]
    assert not ghost and three[0]["title"] == "RETAGGED"


@pytest.mark.asyncio
async def test_nested_roots_retag_plus_add_loses_rows(library):
    """Nested roots (allowed: C64Music + C64Music/DEMOS): a re-tagged track is
    'existing' (old and new both present) so neither root is patched for it,
    while an add in the same commit patches the outer root to the live count."""
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash, upsert_scan_dir
    root, store, _ = library
    inner = root / "B"
    await scanner._run_scan([str(root)])
    await upsert_scan_dir(str(inner))
    await scanner._run_scan([str(inner)])                 # three.mod re-tagged to the inner root
    t3 = next(t for t in store._tracks.values() if t["path"].endswith("three.mod"))
    fstree._store_recursive_tracks_under(store, inner)    # browse: builds both roots' entries
    make_wav(root / "A" / "new.wav", secs=1.0)
    await scanner._run_scan([str(root)])                  # outer full scan: re-tag + add
    t3 = next(t for t in store._tracks.values() if t["path"].endswith("three.mod"))
    rows = fstree._store_recursive_tracks_under(store, inner)
    names = sorted(os.path.relpath(r["path"], root) for r in rows)
    e = fstree._SCAN_ROOT_FULL_CACHE[path_hash(str(root))]
    assert "B/three.mod" in names


@pytest.mark.asyncio
async def test_fp_recorded_before_commit_strands_new_members(library, monkeypatch):
    root, store, _ = library
    make_mod(root / "B" / "m1.mod", title=b"m1")
    make_mod(root / "B" / "m2.mod", title=b"m2")
    with zipfile.ZipFile(root / "B" / "pack.zip", "w") as zf:
        zf.write(root / "B" / "m1.mod", "a.mod")
        zf.write(root / "B" / "m2.mod", "b.mod")
    (root / "B" / "m1.mod").unlink(); (root / "B" / "m2.mod").unlink()
    fp = {"v": "rules-v1"}
    monkeypatch.setattr(scanner, "_listing_fingerprint", lambda: fp["v"])
    await scanner._run_scan([str(root)])                      # manual full scan under v1
    # Simulate: under v1, b.mod was not a discoverable member (drop it from the store)
    bid = next(t["id"] for t in store._tracks.values() if t["path"].endswith("pack.zip::b.mod"))
    store.delete_track_ids([bid])
    fp["v"] = "rules-v2"                                      # upgrade learned b.mod's format
    # The first automatic scan after the upgrade is interrupted after discovery
    # (restart / crash during the long extraction of newly-found members).
    real = scanner.store_hash_lookups_batch

    async def boom(*a, **k):
        raise asyncio.CancelledError()
    monkeypatch.setattr(scanner, "store_hash_lookups_batch", boom)
    with pytest.raises(asyncio.CancelledError):
        await scanner._run_scan([str(root)], light=True)
    monkeypatch.setattr(scanner, "store_hash_lookups_batch", real)
    from soniqboom.core.data import path_hash
    await scanner._run_scan([str(root)], light=True)          # next boot's startup reconcile
    got = [p for p in _paths(store) if "pack.zip" in p]
    assert "B/pack.zip::b.mod" in got, "new member stranded: fp recorded before the commit"


def _restart(store):
    """A new process: memory state gone, config (the AOF) kept."""
    if scanner._dup_runner_task is not None:
        scanner._dup_runner_task.cancel()
    store._on_dup_dirty = None
    store.take_dup_dirty()


@pytest.mark.asyncio
async def test_stale_delta_survives_a_crash_after_a_clean_shutdown(library, monkeypatch):
    root, store, stats = library
    await scanner._run_scan([str(root)])
    ids = sorted(store._tracks)
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 30.0)
    scanner.install_dup_regroup()
    store.update_track_fields(ids[0], {"title": "Edit in run 1"})
    scanner.cancel_background_tasks()                      # run 1: clean shutdown
    _restart(store)
    scanner.install_dup_regroup()                          # run 2 boot: restores {ids[0]}
    store.update_track_fields(ids[1], {"title": "Edit in run 2"})
    _restart(store)                                        # run 2 CRASHES (no clean save)
    scanner.install_dup_regroup()                          # run 3 boot
    try:
        assert store._dup_dirty_overflow or ids[1] in store._dup_dirty, \
            "run 2's edit is lost: the stale run-1 delta suppressed the full pass"
    finally:
        _restart(store)


@pytest.mark.asyncio
async def test_clean_shutdown_during_a_background_full_pass(library, monkeypatch):
    root, store, stats = library
    await scanner._run_scan([str(root)])
    ids = sorted(store._tracks)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_full():
        started.set()
        await gate.wait()
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", slow_full)
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 0.01)
    monkeypatch.setattr(scanner, "_dup_full_last", float("-inf"))
    store._dup_dirty_overflow = True                       # e.g. a big remote scan's delta
    store.set_config(scanner._DUP_PENDING_KEY, True)
    scanner.install_dup_regroup()                          # runner starts the full pass
    await asyncio.wait_for(started.wait(), 5)
    store.update_track_fields(ids[0], {"title": "Edited during the full pass"})
    scanner.cancel_background_tasks()                      # clean shutdown mid-pass
    await asyncio.sleep(0.05)
    saved = store.get_config(scanner._DUP_PENDING_DELTA_KEY)
    _restart(store)
    scanner.install_dup_regroup()
    try:
        assert store._dup_dirty_overflow, "the interrupted FULL pass is not redone"
    finally:
        _restart(store)


@pytest.mark.asyncio
async def test_saved_delta_json_roundtrip(library, monkeypatch, tmp_data_dir):
    """The saved delta survives the AOF's JSON encoding (``aof.AOFWriter``:
    ``json.dumps(default=str)``, replayed with ``json.loads``) — None values,
    floats, NaN — and the next start re-groups exactly those tracks."""
    root, store, stats = library
    await scanner._run_scan([str(root)])
    tid = sorted(store._tracks)[0]
    store.update_track_fields(tid, {"duration": float("nan")})
    store.take_dup_dirty()
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 30.0)
    scanner.install_dup_regroup()
    store.update_track_fields(tid, {"title": "x"})
    new_tid = "brand-new"
    store.upsert_tracks_batch([{"id": new_tid, "path": str(root / "zz.mod"), "title": "n",
                                "scan_root_hash": store._tracks[tid]["scan_root_hash"]}])
    scanner.cancel_background_tasks()
    saved = store.get_config(scanner._DUP_PENDING_DELTA_KEY)
    assert saved and {tid, new_tid} <= set(saved)
    replayed = json.loads(json.dumps(saved, default=str))          # the AOF's encoding
    assert json.dumps(replayed, sort_keys=True) == json.dumps(saved, sort_keys=True)
    assert saved[new_tid] is None and replayed[new_tid] is None    # "a new track"
    assert math.isnan(replayed[tid]["duration"])
    _restart(store)
    store.set_config(scanner._DUP_PENDING_DELTA_KEY, replayed)      # the replayed journal
    scanner.install_dup_regroup()
    assert set(store._dup_dirty) == set(replayed) and not store._dup_dirty_overflow
    scanner.cancel_background_tasks()


@pytest.mark.asyncio
async def test_drill_down_rename_leaves_listing_stale(library):
    from soniqboom.api import fstree
    root, store, _ = library
    await scanner._run_scan([str(root)])
    before = fstree._store_recursive_tracks_under(store, root / "A")    # folder open
    os.rename(root / "A" / "one.wav", root / "A" / "uno.wav")
    res = await scanner.refresh_subtree_under_root(str(root), str(root / "A"))   # folder click
    # the watcher's scoped scan then finds nothing left to do
    changed = await scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav", "A/uno.wav"))
    after = fstree._store_recursive_tracks_under(store, root / "A")
    names = sorted(os.path.basename(d["path"]) for d in after)
    assert "uno.wav" in names and "one.wav" not in names


@pytest.mark.skipif(os.geteuid() == 0, reason="non-root")
@pytest.mark.asyncio
async def test_drill_down_prunes_members_of_an_unreadable_zip(library):
    root, store, _ = library
    make_mod(root / "B" / "inner.mod", title=b"inner")
    with zipfile.ZipFile(root / "B" / "pack.zip", "w") as zf:
        zf.write(root / "B" / "inner.mod", "a.mod")
    (root / "B" / "inner.mod").unlink()
    await scanner._run_scan([str(root)])
    os.chmod(root / "B" / "pack.zip", 0)
    try:
        res = await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    finally:
        os.chmod(root / "B" / "pack.zip", 0o644)
    assert "B/pack.zip::a.mod" in _paths(store)


@pytest.mark.asyncio
async def test_dangling_zip_symlink_members_never_pruned(library):
    root, store, _ = library
    make_mod(root / "B" / "inner.mod", title=b"inner")
    target = root.parent / "outside.zip"
    with zipfile.ZipFile(target, "w") as zf:
        zf.write(root / "B" / "inner.mod", "a.mod")
    (root / "B" / "inner.mod").unlink()
    os.symlink(target, root / "B" / "link.zip")
    await scanner._run_scan([str(root)])
    assert "B/link.zip::a.mod" in _paths(store)
    target.unlink()                       # the link now dangles
    for _ in range(2):
        await scanner._run_scan([str(root)])
    assert "B/link.zip::a.mod" not in _paths(store), "members of a dangling link are kept forever"


@pytest.mark.asyncio
async def test_nested_zip_with_bad_crc_prunes_its_members(library):
    root, store, _ = library
    make_mod(root / "B" / "inner.mod", title=b"inner")
    with zipfile.ZipFile(root / "B" / "n.zip", "w", zipfile.ZIP_STORED) as nz:
        nz.write(root / "B" / "inner.mod", "deep.mod")
    with zipfile.ZipFile(root / "B" / "pack.zip", "w", zipfile.ZIP_STORED) as zf:
        zf.write(root / "B" / "n.zip", "nested.zip")
        zf.write(root / "B" / "inner.mod", "top.mod")
    for f in ("inner.mod", "n.zip"):
        (root / "B" / f).unlink()
    await scanner._run_scan([str(root)])
    assert "B/pack.zip::nested.zip::deep.mod" in _paths(store)
    data = bytearray((root / "B" / "pack.zip").read_bytes())
    i = data.find(b"synthetic") if b"synthetic" in data else data.find(b"inner")
    data[i] ^= 0xFF                                  # flip a byte inside the nested zip's data
    st = os.stat(root / "B" / "pack.zip")
    (root / "B" / "pack.zip").write_bytes(bytes(data))
    fa = set()
    listed, _ = scanner._find_audio_files([str(root)], failed_archives=fa)
    key = scanner._FP_KEY + path_hash(str(root))
    store.set_config(key, None)
    await scanner._run_scan([str(root)])
    assert "B/pack.zip::nested.zip::deep.mod" in _paths(store)
    # A NESTED failure is protected through its readable outer archive's
    # listing: it doesn't withhold the root's listing fingerprint (only an
    # unreadable top-level archive holding indexed members does).
    assert store.get_config(key) is not None


@pytest.mark.asyncio
async def test_add_then_delete_restores_stale_disk_cache(library):
    from soniqboom.api import fstree
    from soniqboom.config import get_data_dir
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    fstree.invalidate_browse_cache()
    fstree.warmup_scan_root_caches(get_data_dir())          # boot: builds + persists
    assert (get_data_dir() / fstree._BROWSE_CACHE_FILENAME).exists()
    make_mod(root / "B" / "added.mod", title=b"added")
    await scanner._run_scan([str(root)], scope=_scope(root, "B/added.mod"))   # watcher: +1
    (root / "A" / "two.wav").unlink()
    await scanner._run_scan([str(root)], scope=_scope(root, "A/two.wav"))     # watcher: -1
    # restart (clean shutdown persists only when browse_disk_stale())
    from soniqboom.core import folder_album
    fstree.invalidate_browse_cache(drop_disk=False)
    fstree._STORE_RECURSIVE_CACHE.clear()
    restored, n = fstree._load_browse_cache(get_data_dir(), store)
    rows = fstree._store_recursive_tracks_under(store, root)
    names = sorted(os.path.relpath(r["path"], root) for r in rows)
    assert names == _paths(store)


@pytest.mark.asyncio
async def test_root_removed_before_commit_drops_pending(library, monkeypatch, tmp_path):
    from soniqboom.core.data import delete_scan_dir, delete_tracks_by_scan_root
    root, store, _ = library
    await scanner._run_scan([str(root)])
    make_mod(root / "B" / "late.mod", title=b"late")
    real = scanner.get_track_ids_for_scan_root
    calls = {"n": 0}

    async def spy(r):
        calls["n"] += 1
        if calls["n"] == 2:                           # the stale-cleanup lookup: extraction done
            await delete_scan_dir(str(root))          # DELETE /api/admin/dirs, purge=true
            await delete_tracks_by_scan_root(str(root))
        return await real(r)
    monkeypatch.setattr(scanner, "get_track_ids_for_scan_root", spy)
    await scanner._run_scan([str(root)])
    assert _paths(store) == [] and str(root) not in store._scan_dirs




# ── Round 5 (performance) ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clicks_during_a_commit_are_served_the_pre_commit_rows(library, monkeypatch):
    """No rebuild from a half-committed store (seconds on a big root, and the
    source of duplicated rows); the commit then patches the same entry."""
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    h = path_hash(str(root))
    fstree._get_or_build_scan_root_sorted(store, h)
    before = fstree._SCAN_ROOT_FULL_CACHE[h]
    for i in range(30):
        make_mod(root / "B" / f"y{i:02}.mod", title=f"y{i}".encode())
    real = store.upsert_tracks_batch
    served = []

    def spy(batch, *a, **k):
        r = real(batch, *a, **k)
        fstree._store_recursive_tracks_under(store, root)            # a click mid-commit
        served.append(fstree._SCAN_ROOT_FULL_CACHE.get(h) is before)
        return r
    monkeypatch.setattr(store, "upsert_tracks_batch", spy)
    await scanner._run_scan([str(root)], scope=_scope(root, "B"))
    assert served and all(served), "the root was rebuilt mid-commit"
    after = fstree._SCAN_ROOT_FULL_CACHE[h]
    assert after is not before and len(after["paths"]) == len(set(after["paths"]))
    fresh_p, _fresh_d = _fresh_b(store, h)
    assert after["paths"] == fresh_p


@pytest.mark.asyncio
async def test_patch_is_idempotent_for_rows_already_cached(tmp_data_dir):
    from soniqboom.api import fstree
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    st.upsert_tracks_batch([{"id": f"t{i}", "path": f"/r/{i:02}.mod", "title": "T",
                             "scan_root_hash": "hh"} for i in range(10)])
    fstree._SCAN_ROOT_FULL_CACHE.pop("hh", None)
    fstree._get_or_build_scan_root_sorted(st, "hh")
    e = fstree._SCAN_ROOT_FULL_CACHE["hh"]
    # an added row that is (somehow) already cached — even twice — must end up
    # exactly once
    i = e["paths"].index("/r/05.mod")
    e["paths"].insert(i, "/r/05.mod")
    e["dicts"].insert(i, dict(e["dicts"][i]))
    new = {"id": "t10", "path": "/r/05.mod", "title": "N", "scan_root_hash": "hh"}
    e["size"] = 10
    assert await fstree.patch_scan_root_rows("hh", {"/r/05.mod"}, [new], 10)
    paths = fstree._SCAN_ROOT_FULL_CACHE["hh"]["paths"]
    assert paths.count("/r/05.mod") == 1 and len(paths) == 10


@pytest.mark.asyncio
async def test_a_big_removal_is_patched_not_dropped(tmp_data_dir, monkeypatch):
    from soniqboom.api import fstree
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(fstree, "_PATCH_MAX", 5)
    st = TrackStore()
    st.upsert_tracks_batch([{"id": f"t{i}", "path": f"/r/{i:02}.mod", "title": "T",
                             "scan_root_hash": "hr"} for i in range(40)])
    fstree._SCAN_ROOT_FULL_CACHE.pop("hr", None)
    fstree._get_or_build_scan_root_sorted(st, "hr")
    gone = [f"t{i}" for i in range(30)]
    removed = {st._tracks[t]["path"] for t in gone}
    st.delete_track_ids(gone)
    assert await fstree.patch_scan_root_rows("hr", removed, [], 10)
    assert len(fstree._SCAN_ROOT_FULL_CACHE["hr"]["paths"]) == 10


@pytest.mark.asyncio
async def test_a_count_changing_commit_marks_the_disk_copy_for_resave(library, monkeypatch):
    from soniqboom.core import folder_album
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    fstree._get_or_build_scan_root_sorted(store, path_hash(str(root)))
    monkeypatch.setattr(folder_album, "_browse_disk_stale", False)
    dropped = []
    monkeypatch.setattr(folder_album, "_drop_browse_disk_cache", lambda: dropped.append(1))
    make_mod(root / "B" / "extra.mod")
    await scanner._run_scan([str(root)], scope=_scope(root, "B/extra.mod"))
    # a graceful shutdown re-persists (the new track's duplicate annotation may
    # additionally drop the copy — ``refresh_album_caches``' own rule)
    assert folder_album.browse_disk_stale()


# ── Round 6 (QA repro tests, kept as regression tests) ─────────────────────

def _mkzip(root, name):
    make_mod(root / "B" / "inner.mod", title=b"inner")
    with zipfile.ZipFile(root / "B" / name, "w") as zf:
        zf.write(root / "B" / "inner.mod", "a.mod")
        zf.write(root / "B" / "inner.mod", "b.mod")
    (root / "B" / "inner.mod").unlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["truncated", "garbage", "zero"])
@pytest.mark.parametrize("mode", ["full", "scoped", "light"])
async def test_corrupt_zip_members(library, how, mode):
    root, store, _ = library
    _mkzip(root, "album.zip")
    await scanner._run_scan([str(root)])
    before = [p for p in _paths(store) if "album.zip::" in p]
    assert len(before) == 2
    p = root / "B" / "album.zip"
    data = p.read_bytes()
    if how == "truncated":
        p.write_bytes(data[: len(data) // 2])          # an in-place copy still running
    elif how == "garbage":
        p.write_bytes(b"\x00" * len(data))
    else:
        p.write_bytes(b"")
    fa = set()
    scanner._find_audio_files([str(root)], failed_archives=fa)
    if mode == "full":
        await scanner._run_scan([str(root)])
    elif mode == "scoped":
        await scanner._run_scan([str(root)], scope=_scope(root, "B/album.zip"))
    else:
        await scanner._run_scan([str(root)], light=True)
    after = [p for p in _paths(store) if "album.zip::" in p]
    assert after == before, f"{how}/{mode}: members of an existing-but-unreadable zip were pruned"


import shutil
from pathlib import Path
LHA = Path(__file__).resolve().parent.parent / "internal/testdata/archives/AHXSONGS.LHA"


@pytest.mark.asyncio
async def test_truncated_lha_members(library):
    if not LHA.exists():
        pytest.skip("needs the local LHA test archive")
    root, store, _ = library
    shutil.copy(LHA, root / "B" / "songs.lha")
    await scanner._run_scan([str(root)])
    before = [p for p in _paths(store) if "songs.lha::" in p]
    data = (root / "B" / "songs.lha").read_bytes()
    (root / "B" / "songs.lha").write_bytes(data[: len(data) // 50])
    os.utime(root / "B" / "songs.lha", (1, 1))
    fa = set()
    scanner._find_audio_files([str(root)], failed_archives=fa)
    await scanner._run_scan([str(root)])
    after = [p for p in _paths(store) if "songs.lha::" in p]
    assert before and after == before, "members of a truncated LHA were pruned"


def _make_d64(path, psid: bytes):
    img = bytearray(174848)
    d = 17 * 21 * 256 + 256                         # track 18 sector 1
    img[d + 0], img[d + 1] = 0, 0xFF
    img[d + 2] = 0x82                               # PRG, closed
    img[d + 3], img[d + 4] = 1, 0                   # file at track 1 sector 0
    name = b"TUNE".ljust(16, b"\xa0")
    img[d + 5:d + 21] = name
    img[d + 30] = 1
    body = psid
    img[0], img[1] = 0, len(body) + 1
    img[2:2 + len(body)] = body
    path.write_bytes(bytes(img))


@pytest.mark.asyncio
async def test_truncated_d64_members(library, tmp_path):
    from soniqboom.core import diskimage
    root, store, _ = library
    make_psid(tmp_path / "x.sid")
    _make_d64(root / "B" / "game.d64", (tmp_path / "x.sid").read_bytes())
    await scanner._run_scan([str(root)])
    before = [p for p in _paths(store) if "game.d64::" in p]
    data = (root / "B" / "game.d64").read_bytes()
    (root / "B" / "game.d64").write_bytes(data[: len(data) // 2])   # copy in progress
    os.utime(root / "B" / "game.d64", (1, 1))
    fa = set()
    scanner._find_audio_files([str(root)], failed_archives=fa)
    await scanner._run_scan([str(root)], scope=_scope(root, "B/game.d64"))
    after = [p for p in _paths(store) if "game.d64::" in p]
    assert before and after == before, "a truncated disk image lost its members"


def _fresh_r6c(fstree, st, h):
    saved = fstree._SCAN_ROOT_FULL_CACHE.pop(h, None)
    try:
        return fstree._get_or_build_scan_root_sorted(st, h)
    finally:
        if saved is not None:
            fstree._SCAN_ROOT_FULL_CACHE[h] = saved
        else:
            fstree._SCAN_ROOT_FULL_CACHE.pop(h, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(20))
async def test_patch_equals_fresh_build(tmp_data_dir, seed):
    from soniqboom.api import fstree
    from soniqboom.core.store import TrackStore
    rnd = random.Random(seed)
    st = TrackStore()
    h = f"h{seed}"
    names = ["a", "b", "c", "é", "Z", "z/", "ä", "\U0001F600", "a b", "a-b", "a.b"]
    def mkpath(i):
        return f"/r/{rnd.choice(names)}/{rnd.choice(names)}{i:05}.mod"
    n0 = rnd.randint(0, 3000)
    tracks = [{"id": f"t{i}", "path": mkpath(i), "title": "T", "scan_root_hash": h} for i in range(n0)]
    # some tracks in another root sharing path prefixes
    other = [{"id": f"o{i}", "path": mkpath(90000 + i), "title": "O", "scan_root_hash": "other"} for i in range(50)]
    st.upsert_tracks_batch(tracks + other)
    fstree._SCAN_ROOT_FULL_CACHE.pop(h, None)
    fstree._get_or_build_scan_root_sorted(st, h)
    # a commit: delete some, add some (small or big), change some in place
    ids = list(st._tag_scan_root_hash.get(h, ()))
    k_del = rnd.randint(0, min(len(ids), rnd.choice([3, 100, 2500])))
    dels = rnd.sample(ids, k_del)
    k_add = rnd.choice([0, 1, 5, 128, 129, 700, 1500, 2500])
    adds = [{"id": f"n{seed}_{i}", "path": mkpath(50000 + i), "title": "N", "scan_root_hash": h}
            for i in range(k_add)]
    old_versions = {tid: dict(st._tracks[tid]) for tid in dels}
    removed = {st._tracks[t]["path"] for t in dels}
    st.delete_track_ids(dels)
    st.upsert_tracks_batch(adds)
    for a in adds:
        old_versions[a["id"]] = None
    new_size = len(st._tag_scan_root_hash.get(h, ()))
    ok = await fstree.patch_scan_root_rows(h, removed, [st._tracks[a["id"]] for a in adds], new_size)
    assert ok
    e = fstree._SCAN_ROOT_FULL_CACHE[h]
    fp, fd = _fresh_r6c(fstree, st, h)
    assert e["paths"] == fp
    assert [d["id"] for d in e["dicts"]] == [d["id"] for d in fd]
    assert e["size"] == new_size == len(fp)


@pytest.mark.asyncio
async def test_late_write_after_the_shutdown_save_is_lost(library, monkeypatch):
    root, store, stats = library
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _REAL_FULL_DUP)
    make_wav(root / "A" / "x.wav", secs=6.0)
    make_wav(root / "A" / "y.wav", secs=6.0)
    await scanner._run_scan([str(root)])
    x, y = (next(t["id"] for t in store._tracks.values() if t["path"].endswith(n)) for n in ("x.wav", "y.wav"))
    store.update_track_fields_batch([(x, {"title": "Song", "artist": "Band"}),
                                     (y, {"title": "Song", "artist": "Band", "bitrate": 1})])
    await scanner._regroup_duplicates_now()
    assert {_dup(store, x)[1], _dup(store, y)[1]} == {True, False}
    other = next(t for t in store._tracks if t not in (x, y))
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 30.0)
    scanner.install_dup_regroup()
    store.update_track_fields(other, {"title": "pending edit"})      # pending in the runner's settle
    scanner.cancel_background_tasks()                                # shutdown step 0: saves {other}
    store.update_track_fields(x, {"title": "Another Song"})           # a scan/enrichment write during the later shutdown steps
    scanner.finalize_dup_pending()                                    # last shutdown step (before the AOF flush)
    _restart(store)                                                   # process exits
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 0.01)
    monkeypatch.setattr(scanner, "_dup_full_last", float("-inf"))     # a fresh process
    scanner.install_dup_regroup()                                     # next start
    try:
        for _ in range(750):
            await asyncio.sleep(0.02)
            if not store._dup_dirty and not store.get_config(scanner._DUP_PENDING_KEY):
                break
        assert _dup(store, y) == (None, True), "y stays a hidden non-primary of a group that no longer exists"
    finally:
        _restart(store)


def _pack(root, members):
    b = root / "B"
    make_mod(b / "inner.mod", title=b"inner")
    with zipfile.ZipFile(b / "n.zip", "w", zipfile.ZIP_STORED) as nz:
        nz.write(b / "inner.mod", "deep.mod")
    tmp = b / "pack.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        zf.write(b / "n.zip", "nested.zip")
        for m in members:
            zf.write(b / "inner.mod", m)
    data = bytearray(tmp.read_bytes())
    i = data.find(b"deep.mod")                         # inside the nested zip's bytes
    j = data.find(b"inner", i)                         # the nested member's content
    data[j] ^= 0xFF                                    # bad CRC inside the nested zip
    (b / "pack.zip").write_bytes(bytes(data))
    for f in ("inner.mod", "n.zip", "pack.tmp"):
        (b / f).unlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "scoped", "drill"])
async def test_removed_member_of_a_zip_with_a_bad_nested_member(library, mode):
    root, store, _ = library
    _pack(root, ["top.mod", "gone.mod"])
    fa = set()
    scanner._find_audio_files([str(root)], failed_archives=fa)
    await scanner._run_scan([str(root)])
    assert "B/pack.zip::gone.mod" in _paths(store)
    _pack(root, ["top.mod"])                          # new version of the pack: gone.mod removed
    os.utime(root / "B" / "pack.zip", (2e9, 2e9))
    if mode == "full":
        await scanner._run_scan([str(root)])
    elif mode == "scoped":
        await scanner._run_scan([str(root)], scope=_scope(root, "B/pack.zip"))
    else:
        await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    left = [p for p in _paths(store) if "pack.zip" in p]
    assert "B/pack.zip::gone.mod" not in left, "a member removed from the zip is kept forever"


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["raise", "cancel"])
async def test_window_closed_after_a_failing_commit(library, monkeypatch, how):
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    h = path_hash(str(root))
    fstree._get_or_build_scan_root_sorted(store, h)
    os.rename(root / "A" / "one.wav", root / "A" / "uno.wav")   # same-count change
    real = store.upsert_tracks_batch
    seen = []

    def spy(batch, *a, **k):
        seen.append(dict(fstree._ROOTS_COMMITTING))
        r = real(batch, *a, **k)
        if how == "raise":
            raise RuntimeError("boom")
        return r
    monkeypatch.setattr(store, "upsert_tracks_batch", spy)
    task = asyncio.create_task(scanner._run_scan([str(root)], scope=_scope(root, "A/one.wav", "A/uno.wav")))
    if how == "cancel":
        for _ in range(200):
            await asyncio.sleep(0)
            if seen:
                break
        task.cancel()
    try:
        await task
    except (RuntimeError, asyncio.CancelledError):
        pass                                        # the cancelled scan
    assert fstree._ROOTS_COMMITTING == {}
    rows = fstree._store_recursive_tracks_under(store, root)
    names = sorted(os.path.relpath(r["path"], root) for r in rows)
    assert names == _paths(store), "stale listing after an interrupted same-count commit"


@pytest.mark.asyncio
async def test_drill_down_window_closed_after_raise(library, monkeypatch):
    from soniqboom.api import fstree
    root, store, _ = library
    await scanner._run_scan([str(root)])
    make_mod(root / "B" / "new.mod", title=b"n")

    async def boom(*a, **k):
        raise RuntimeError("upsert failed")
    monkeypatch.setattr(scanner, "upsert_tracks_batch", boom)
    with pytest.raises(RuntimeError):
        await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    assert fstree._ROOTS_COMMITTING == {}


@pytest.mark.skipif(os.geteuid() == 0, reason="non-root")
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["zip", "lha"])
async def test_archive_in_a_folder_without_search_permission(library, kind):
    if kind == "lha" and not LHA.exists():
        pytest.skip("needs the local LHA test archive")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("needs a non-root user")
    import shutil
    from pathlib import Path
    root, store, _ = library
    b = root / "B"
    if kind == "zip":
        make_mod(b / "inner.mod", title=b"inner")
        with zipfile.ZipFile(b / "pack.zip", "w") as zf:
            zf.write(b / "inner.mod", "a.mod")
        (b / "inner.mod").unlink()
        name = "pack.zip"
    else:
        shutil.copy(LHA, b / "songs.lha")
        name = "songs.lha"
    await scanner._run_scan([str(root)])
    before = [p for p in _paths(store) if f"{name}::" in p]
    plain_before = [p for p in _paths(store) if p.startswith("B/") and "::" not in p]
    assert before
    from soniqboom.core import archive as _arc
    with _arc._CACHE_LOCK:
        _arc._MAP_CACHE.clear(); _arc._OPEN_CACHE.clear()      # a fresh process
    os.chmod(b, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)    # r--r--r--: names listable, no stat/open
    try:
        fa = set()
        listed, errs = scanner._find_audio_files([str(root)], failed_archives=fa)
        await scanner._run_scan([str(root)])
        after = [p for p in _paths(store) if f"{name}::" in p]
        plain_after = [p for p in _paths(store) if p.startswith("B/") and "::" not in p]
    finally:
        os.chmod(b, 0o755)
    assert len(after) == len(before), "members of an existing-but-unreadable archive pruned"
    assert plain_after == plain_before, "files of an unsearchable folder pruned"


@pytest.mark.asyncio
async def test_root_removed_mid_commit(library, monkeypatch):
    from soniqboom.core.data import delete_scan_dir, delete_tracks_by_scan_root
    root, store, _ = library
    await scanner._run_scan([str(root)])
    for i in range(60):
        make_mod(root / "B" / f"m{i:02}.mod", title=f"m{i}".encode())
    real = store.upsert_tracks_batch
    calls = {"n": 0}

    def spy(batch, *a, **k):
        r = real(batch, *a, **k)
        calls["n"] += 1
        if calls["n"] == 1:
            async def purge():
                await delete_scan_dir(str(root))           # DELETE /api/admin/dirs purge=true
                await delete_tracks_by_scan_root(str(root))
                scanner.forget_root(str(root))
            asyncio.get_running_loop().create_task(purge())
        return r
    monkeypatch.setattr(store, "upsert_tracks_batch", spy)
    await scanner._run_scan([str(root)])
    await asyncio.sleep(0.05)
    assert str(root) not in store._scan_dirs
    assert not store._tracks, "tracks of a removed folder re-appear after the purge"


@pytest.mark.asyncio
async def test_drill_down_window_spans_extraction(library, monkeypatch):
    """The drill-down's commit window covers its WRITES only: during the
    (slow) extraction other writers' changes show in the listing at once."""
    from soniqboom.api import fstree
    from soniqboom.core.data import delete_track
    root, store, _ = library
    await scanner._run_scan([str(root)])
    fstree._store_recursive_tracks_under(store, root)             # browsed
    for i in range(20):
        make_wav(root / "B" / f"n{i:02}.wav", secs=0.5, freq=200 + i)
    real = scanner._extract_one

    def slow(p):
        time.sleep(0.05)                                          # a realistic per-file extraction cost
        return real(p)
    monkeypatch.setattr(scanner, "_extract_one", slow)
    victim = next(t["id"] for t in store._tracks.values() if t["path"].endswith("two.wav"))
    task = asyncio.create_task(scanner.refresh_subtree_under_root(str(root), str(root / "B")))
    await asyncio.sleep(0.3)                                      # extraction under way
    assert not fstree._ROOTS_COMMITTING, "the window is open during extraction"
    await delete_track(victim)                                    # DELETE /api/tracks/{id}
    rows = fstree._store_recursive_tracks_under(store, root)
    assert not any(r["id"] == victim for r in rows)
    res = await asyncio.wait_for(task, 30)
    assert res["added"] == 20 and not fstree._ROOTS_COMMITTING
    rows2 = fstree._store_recursive_tracks_under(store, root)
    assert not any(r["id"] == victim for r in rows2)


# ── Round 6: untested spots ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_commit_window_is_left_open(library):
    from soniqboom.api import fstree
    root, store, _ = library
    await scanner._run_scan([str(root)])
    make_mod(root / "B" / "w.mod")
    await scanner._run_scan([str(root)], scope=_scope(root, "B/w.mod"))
    await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    assert fstree._ROOTS_COMMITTING == {}


@pytest.mark.asyncio
async def test_the_drill_down_writes_inside_the_root_window(library, monkeypatch):
    from soniqboom.api import fstree
    from soniqboom.core import data
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    make_mod(root / "B" / "dd.mod")
    seen = []
    real = data.upsert_tracks_batch

    async def spy(tracks):
        seen.append(path_hash(str(root)) in fstree._ROOTS_COMMITTING)
        return await real(tracks)
    monkeypatch.setattr(scanner, "upsert_tracks_batch", spy)
    await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    assert seen and all(seen)


@pytest.mark.asyncio
async def test_a_patch_gives_up_if_the_entry_was_replaced_while_shaping(tmp_data_dir, monkeypatch):
    from soniqboom.api import fstree
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    st.upsert_tracks_batch([{"id": f"t{i}", "path": f"/r/{i:04}.mod", "title": "T",
                             "scan_root_hash": "hz"} for i in range(10)])
    fstree._SCAN_ROOT_FULL_CACHE.pop("hz", None)
    fstree._get_or_build_scan_root_sorted(st, "hz")
    new = [{"id": f"n{i}", "path": f"/r/n{i:04}.mod", "title": "N", "scan_root_hash": "hz"}
           for i in range(1500)]                                     # > 1 shaping chunk → yields
    st.upsert_tracks_batch([dict(t) for t in new])
    real_sleep = asyncio.sleep

    async def swap_entry(d, *a, **k):
        fstree._SCAN_ROOT_FULL_CACHE["hz"] = {"size": 0, "paths": [], "dicts": []}
        return await real_sleep(d, *a, **k)
    monkeypatch.setattr(asyncio, "sleep", swap_entry)
    try:
        ok = await fstree.patch_scan_root_rows("hz", set(), [st._tracks[t["id"]] for t in new], 1510)
    finally:
        monkeypatch.setattr(asyncio, "sleep", real_sleep)
    assert ok is False


@pytest.mark.asyncio
async def test_an_automatic_full_scan_never_registers_a_root(library):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    store._scan_dirs.pop(str(root))                                   # removed meanwhile
    await scanner._run_scan([str(root)], light=True)
    assert str(root) not in store._scan_dirs


@pytest.mark.asyncio
async def test_the_listing_fingerprint_is_withheld_only_for_an_unreadable_top_level_archive(library, monkeypatch):
    from soniqboom.core.data import path_hash
    root, store, _ = library
    key = scanner._FP_KEY + path_hash(str(root))
    real = scanner._find_audio_files

    def with_failure(name):
        def f(*a, **k):
            out = real(*a, **k)
            fa = a[4] if len(a) > 4 else k.get("failed_archives")
            if fa is not None:
                fa.add(name)
            return out
        return f
    with zipfile.ZipFile(root / "B" / "bad.zip", "w") as zf:            # an archive with indexed members
        zf.write(root / "B" / "three.mod", "m.mod")
    await scanner._run_scan([str(root)])
    store.set_config(key, None)
    monkeypatch.setattr(scanner, "_find_audio_files", with_failure(str(root / "B" / "bad.zip")))
    await scanner._run_scan([str(root)])
    assert store.get_config(key) is None, "recorded despite an unreadable archive with members"
    monkeypatch.setattr(scanner, "_find_audio_files", with_failure(str(root / "B" / "empty.zip")))
    await scanner._run_scan([str(root)])
    assert store.get_config(key) == scanner._listing_fingerprint(), "withheld for an archive with nothing to protect"
    store.set_config(key, None)
    monkeypatch.setattr(scanner, "_find_audio_files",
                        with_failure(str(root / "B" / "ok.zip") + "::inner.zip"))
    await scanner._run_scan([str(root)])
    assert store.get_config(key) == scanner._listing_fingerprint()


# ── Round 6 (performance) ───────────────────────────────────────────────────

def test_listing_memos_evict_the_least_recently_used(monkeypatch):
    from soniqboom.api import fstree
    monkeypatch.setattr(fstree, "_LISTING_MEMO_MIN_ROWS", 1)
    monkeypatch.setattr(fstree, "_LISTING_MEMO_MAX", 3)
    memo: dict = {}
    lists = [[{"i": i}] for i in range(4)]
    for lst in lists[:3]:
        fstree._memo(memo, lst, lambda rows: list(rows))
    fstree._memo(memo, lists[0], lambda rows: list(rows))        # hit → most recent
    fstree._memo(memo, lists[3], lambda rows: list(rows))        # evicts lists[1], not lists[0]
    assert id(lists[0]) in memo and id(lists[1]) not in memo


@pytest.mark.asyncio
async def test_a_folder_listing_is_not_rebuilt_inside_a_commit_window(library):
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    h = path_hash(str(root))
    first = fstree._store_recursive_tracks_under(store, root / "B")
    with fstree.root_commit({h}):
        store.upsert_tracks_batch([{"id": "wx", "path": str(root / "B" / "wx.mod"), "title": "W",
                                    "scan_root_hash": h}])
        again = fstree._store_recursive_tracks_under(store, root / "B")
        assert again is first, "the listing was rebuilt from the moving live count"
    store.delete_track_ids(["wx"])


@pytest.mark.asyncio
async def test_a_patch_removing_most_rows_uses_the_filter_and_matches(tmp_data_dir):
    from soniqboom.api import fstree
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    st.upsert_tracks_batch([{"id": f"t{i}", "path": f"/r/{i:04}.mod", "title": "T",
                             "scan_root_hash": "hf"} for i in range(200)])
    fstree._SCAN_ROOT_FULL_CACHE.pop("hf", None)
    fstree._get_or_build_scan_root_sorted(st, "hf")
    gone = [f"t{i}" for i in range(0, 200, 2)]                 # half: touched * 16 > n
    removed = {st._tracks[t]["path"] for t in gone}
    st.delete_track_ids(gone)
    assert await fstree.patch_scan_root_rows("hf", removed, [], 100)
    patched = fstree._SCAN_ROOT_FULL_CACHE.pop("hf")
    paths, dicts = fstree._get_or_build_scan_root_sorted(st, "hf")
    assert patched["paths"] == paths and patched["dicts"] == dicts


@pytest.mark.asyncio
async def test_the_drill_down_reads_existing_tracks_from_the_cached_rows(library, monkeypatch):
    from soniqboom.api import fstree
    from soniqboom.core.data import path_hash
    root, store, _ = library
    await scanner._run_scan([str(root)])
    fstree._get_or_build_scan_root_sorted(store, path_hash(str(root)))
    calls = []
    real = scanner.get_track_ids_for_scan_root

    async def spy(r):
        calls.append(r)
        return await real(r)
    monkeypatch.setattr(scanner, "get_track_ids_for_scan_root", spy)
    (root / "B" / "three.mod").unlink()
    res = await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    assert calls == [] and res["removed"] == 1



@pytest.mark.asyncio
async def test_the_bisect_patch_path_is_idempotent_on_a_big_root(tmp_data_dir):
    from soniqboom.api import fstree
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    st.upsert_tracks_batch([{"id": f"t{i}", "path": f"/r/{i:04}.mod", "title": "T",
                             "scan_root_hash": "hb"} for i in range(400)])
    fstree._SCAN_ROOT_FULL_CACHE.pop("hb", None)
    fstree._get_or_build_scan_root_sorted(st, "hb")
    e = fstree._SCAN_ROOT_FULL_CACHE["hb"]
    i = e["paths"].index("/r/0050.mod")
    e["paths"].insert(i, "/r/0050.mod")                          # a stale duplicate copy
    e["dicts"].insert(i, dict(e["dicts"][i]))
    e["size"] = 400
    new = {"id": "t0050b", "path": "/r/0050.mod", "title": "N", "scan_root_hash": "hb"}
    assert await fstree.patch_scan_root_rows("hb", {"/r/0050.mod"}, [new], 400)
    paths = fstree._SCAN_ROOT_FULL_CACHE["hb"]["paths"]
    assert paths.count("/r/0050.mod") == 1 and len(paths) == 400


# ── Round 7 (QA repro tests, kept as regression tests) ─────────────────────

def _ndos_adf(p: Path):
    img = bytearray(901120)
    img[0:4] = b"\x00\x00\x00\x00"        # NDOS: trackloader, no filesystem boot block
    p.write_bytes(bytes(img))


def _kick_adf(p: Path):
    img = bytearray(901120)
    img[0:4] = b"KICK"
    p.write_bytes(bytes(img))


def _ext_adf(p: Path):
    img = bytearray(1782 * 512)            # 81-cylinder DOS image
    img[0:4] = b"DOS\x00"
    p.write_bytes(bytes(img))


def _uae_adf(p: Path):
    p.write_bytes(b"UAE-1ADF" + b"\x00" * 5000)


def _d64_42(p: Path):
    p.write_bytes(bytes(205312))           # 42-track .d64 (no error bytes)


def _d64_40_err(p: Path):
    p.write_bytes(bytes(197376))


def _dos_adf_empty(p: Path):
    """A freshly formatted, empty OFS disk: boot block + root block."""
    img = bytearray(901120)
    img[0:4] = b"DOS\x00"
    struct.pack_into(">I", img, 880 * 512, 2)             # T_HEADER
    struct.pack_into(">i", img, 880 * 512 + 508, 1)       # ST_ROOT
    p.write_bytes(bytes(img))


IMG_CASES = {"ndos.adf": _ndos_adf, "kick.adf": _kick_adf,
         "uae.adf": _uae_adf, "t42.d64": _d64_42, "t40e.d64": _d64_40_err,
         "dosempty.adf": _dos_adf_empty}


@pytest.mark.parametrize("name", sorted(IMG_CASES))
@pytest.mark.asyncio
async def test_a_valid_image_without_music_is_not_unreadable(library, name):
    root, store, _ = library
    IMG_CASES[name](root / "B" / name)
    fa = set()
    scanner._find_audio_files([str(root)], failed_archives=fa)
    key = scanner._FP_KEY + path_hash(str(root))
    await scanner._run_scan([str(root)])
    rec1 = store.get_config(key)
    await scanner._run_scan([str(root)], light=True)
    rec2 = store.get_config(key)
    assert not fa, f"{name}: a structurally valid image with no music is 'unreadable'"
    assert rec1 is not None and rec2 is not None, "the listing fingerprint was withheld"


@pytest.mark.asyncio
async def test_regroup_in_flight_at_finalize(library, monkeypatch):
    root, store, stats = library
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _REAL_FULL_DUP)
    make_wav(root / "A" / "x.wav", secs=6.0)
    make_wav(root / "A" / "y.wav", secs=6.0)
    await scanner._run_scan([str(root)])
    x, y = (next(t["id"] for t in store._tracks.values() if t["path"].endswith(n)) for n in ("x.wav", "y.wav"))
    store.update_track_fields_batch([(x, {"title": "Song", "artist": "Band"}),
                                     (y, {"title": "Song", "artist": "Band", "bitrate": 1})])
    await scanner._regroup_duplicates_now()
    assert {_dup(store, x)[1], _dup(store, y)[1]} == {True, False}
    other = next(t for t in store._tracks if t not in (x, y))
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 30.0)
    scanner.install_dup_regroup()
    store.update_track_fields(other, {"title": "pending edit"})
    scanner.cancel_background_tasks()                                 # shutdown step 0: saves {other}
    # a scan still running during shutdown commits x's change and starts its own re-group
    gate = asyncio.Event()
    real_incr = scanner._run_duplicate_detection_incremental

    async def slow_incr(delta):
        await gate.wait()
        return await real_incr(delta)
    monkeypatch.setattr(scanner, "_run_duplicate_detection_incremental", slow_incr)
    store.update_track_fields(x, {"title": "Another Song"})
    t = asyncio.get_running_loop().create_task(scanner._regroup_duplicates_now())
    await asyncio.sleep(0.05)
    scanner.finalize_dup_pending()                                     # last shutdown step
    t.cancel()                                                         # process exits mid-regroup
    try:
        await t
    except BaseException:
        pass
    monkeypatch.setattr(scanner, "_run_duplicate_detection_incremental", real_incr)
    _restart(store)
    monkeypatch.setattr(scanner, "_DUP_REGROUP_SETTLE_S", 0.01)
    monkeypatch.setattr(scanner, "_dup_full_last", float("-inf"))
    scanner.install_dup_regroup()                                      # next start
    try:
        for _ in range(750):
            await asyncio.sleep(0.02)
            if not store._dup_dirty and not store.get_config(scanner._DUP_PENDING_KEY):
                break
        assert _dup(store, y) == (None, True), "y stays a hidden non-primary of a group that no longer exists"
    finally:
        _restart(store)


def _mkzip_r7(root, name):
    make_mod(root / "B" / "inner.mod", title=b"inner")
    with zipfile.ZipFile(root / "B" / name, "w") as zf:
        zf.write(root / "B" / "inner.mod", "a.mod")
        zf.write(root / "B" / "inner.mod", "b.mod")
    (root / "B" / "inner.mod").unlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["album.zip", "ST.zip", "MA.lha", "MA.adf"])
@pytest.mark.parametrize("mode", ["full", "scoped", "light"])
async def test_truncated_prefix_named_zip(library, name, mode):
    root, store, _ = library
    if name.endswith('.zip'):
        _mkzip_r7(root, name)
    elif name.endswith('.lha'):
        shutil.copy(LHA, root / 'B' / name)
    else:
        _adf_with_mod(root / 'B' / name, root)
    await scanner._run_scan([str(root)])
    before = [p for p in _paths(store) if f"{name}::" in p]
    assert len(before) >= 1, _paths(store)
    p = root / "B" / name
    data = p.read_bytes()
    p.write_bytes(data[: len(data) // 2])          # a copy still running / damaged
    os.utime(p, (5, 5))
    fa = set()
    scanner._find_audio_files([str(root)], failed_archives=fa)
    if mode == "full":
        await scanner._run_scan([str(root)])
    elif mode == "scoped":
        await scanner._run_scan([str(root)], scope=_scope(root, f"B/{name}"))
    else:
        await scanner._run_scan([str(root)], light=True)
    after = [p for p in _paths(store) if f"{name}::" in p]
    assert after == before, f"{name}/{mode}: members of a damaged archive were pruned"





def _adf_with_mod(p: Path, root):
    """A DD OFS ADF with one file 'mod.tune' — minimal: root block at 880, one file header, data blocks."""
    import struct
    make_mod(root / "B" / "m.mod", title=b"adf")
    body = (root / "B" / "m.mod").read_bytes(); (root / "B" / "m.mod").unlink()
    img = bytearray(901120)
    img[0:4] = b"DOS\x00"
    BS = 512
    def put32(blk, off, v): struct.pack_into(">I", img, blk * BS + off, v & 0xFFFFFFFF)
    rootb, hdr = 880, 882
    name = b"mod.tune"
    # root block hash table slot
    put32(rootb, 24, hdr)
    # file header
    put32(hdr, BS - 4, (-3) & 0xFFFFFFFF)
    put32(hdr, 0x144, len(body))
    img[hdr * BS + BS - 80] = len(name); img[hdr * BS + BS - 79: hdr * BS + BS - 79 + len(name)] = name
    nblk = (len(body) + 487) // 488
    put32(hdr, 0x08, nblk)
    for i in range(nblk):
        db = 900 + i
        put32(hdr, BS - 204 - i * 4, db)
        chunk = body[i * 488:(i + 1) * 488]
        put32(db, 0x0C, len(chunk))
        img[db * BS + 24: db * BS + 24 + len(chunk)] = chunk
    p.write_bytes(bytes(img))


def _pack_r7(root, with_nested=True, corrupt_nested=False):
    b = root / "B"
    make_mod(b / "inner.mod", title=b"inner")
    nb = io.BytesIO()
    with zipfile.ZipFile(nb, "w", zipfile.ZIP_STORED) as nz:
        nz.write(b / "inner.mod", "deep.mod")
    data = nb.getvalue()
    if corrupt_nested:
        data = data[: len(data) // 2]           # nested zip truncated (BadZipFile)
    with zipfile.ZipFile(b / "pack.zip", "w", zipfile.ZIP_STORED) as zf:
        if with_nested:
            zf.writestr("nested.zip", data)
        zf.write(b / "inner.mod", "top.mod")
    (b / "inner.mod").unlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "scoped", "light"])
@pytest.mark.parametrize("change", ["nested_removed", "nested_truncated"])
async def test_outer_rewritten(library, mode, change):
    root, store, _ = library
    _pack_r7(root)
    await scanner._run_scan([str(root)])
    assert "B/pack.zip::nested.zip::deep.mod" in _paths(store)
    if change == "nested_removed":
        _pack_r7(root, with_nested=False)
    else:
        _pack_r7(root, corrupt_nested=True)
    os.utime(root / "B" / "pack.zip", (10, 10))
    fa = set()
    scanner._find_audio_files([str(root)], failed_archives=fa)
    if mode == "full":
        await scanner._run_scan([str(root)])
    elif mode == "scoped":
        await scanner._run_scan([str(root)], scope=_scope(root, "B/pack.zip"))
    else:
        await scanner._run_scan([str(root)], light=True)
    assert "B/pack.zip::nested.zip::deep.mod" not in _paths(store)
    assert "B/pack.zip::top.mod" in _paths(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [True, False])
async def test_drill_down_counts(library, warm):
    from soniqboom.api import fstree
    root, store, _ = library
    make_mod(root / "B" / "four.mod", title=b"four")
    await scanner._run_scan([str(root)])
    h = path_hash(str(root))
    if warm:
        fstree._get_or_build_scan_root_sorted(store, h)
    else:
        fstree._SCAN_ROOT_FULL_CACHE.pop(h, None)
    make_mod(root / "B" / "five.mod", title=b"five")                 # new
    make_mod(root / "B" / "three.mod", title=b"three-changed")       # changed
    os.utime(root / "B" / "three.mod", (time.time() + 5, time.time() + 5))
    (root / "B" / "four.mod").unlink()                               # gone
    res = await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    assert (res["added"], res["updated"], res["removed"]) == (1, 1, 1)
    assert sorted(p for p in _paths(store) if p.startswith("B/")) == ["B/five.mod", "B/three.mod"]
    rows = fstree._store_recursive_tracks_under(store, root / "B")
    assert sorted(os.path.basename(r["path"]) for r in rows) == ["five.mod", "three.mod"]
    assert not fstree._ROOTS_COMMITTING


@pytest.mark.asyncio
async def test_drill_down_prefix_boundary(library):
    """/lib/B and /lib/B2: the cached-id bisect must not leak B2 rows into B."""
    from soniqboom.api import fstree
    root, store, _ = library
    (root / "B2").mkdir()
    make_mod(root / "B2" / "x.mod", title=b"x")
    make_mod(root / "B" / "y.mod", title=b"y")
    await scanner._run_scan([str(root)])
    fstree._get_or_build_scan_root_sorted(store, path_hash(str(root)))
    (root / "B" / "y.mod").unlink()
    res = await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    assert "B2/x.mod" in _paths(store) and "B/y.mod" not in _paths(store)
    assert res["removed"] == 1


@pytest.mark.asyncio
async def test_root_removed_during_drill_down(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    make_mod(root / "B" / "new.mod", title=b"new")
    gate = threading.Event()
    real = scanner._extract_one

    def slow(p):
        gate.wait(5)
        return real(p)
    monkeypatch.setattr(scanner, "_extract_one", slow)
    task = asyncio.get_running_loop().create_task(
        scanner.refresh_subtree_under_root(str(root), str(root / "B")))
    await asyncio.sleep(0.2)
    # the user removes the folder (with purge) meanwhile
    store._scan_dirs.pop(str(root), None)
    from soniqboom.core.data import delete_track_ids
    await delete_track_ids(list(store._tag_scan_root_hash.get(path_hash(str(root)), ())))
    gate.set()
    res = await task
    assert not _paths(store), "a removed root's tracks came back"



# ── Round 7: untested spots ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finalize_drops_the_saved_set_on_overflow_or_a_busy_regroup(library, monkeypatch):
    root, store, _ = library
    store.set_config(scanner._DUP_PENDING_DELTA_KEY, {"a": None})
    store._dup_dirty_overflow = True
    scanner.finalize_dup_pending()
    assert store.get_config(scanner._DUP_PENDING_DELTA_KEY) is None
    store._dup_dirty_overflow = False
    store.set_config(scanner._DUP_PENDING_DELTA_KEY, {"a": None})
    lock = scanner._dup_lock()
    await lock.acquire()                                      # a re-group in flight
    try:
        scanner.finalize_dup_pending()
    finally:
        lock.release()
    assert store.get_config(scanner._DUP_PENDING_DELTA_KEY) is None
    store.set_config(scanner._DUP_PENDING_DELTA_KEY, {"a": None})
    scanner.finalize_dup_pending()                           # idle and complete → kept
    assert store.get_config(scanner._DUP_PENDING_DELTA_KEY) == {"a": None}
    store.set_config(scanner._DUP_PENDING_DELTA_KEY, None)


def test_shutdown_finalizes_before_the_journal_flush():
    import inspect
    from soniqboom import main
    src = inspect.getsource(main.shutdown)
    assert src.index("finalize_dup_pending()") < src.index('_step("aof-flush"')


@pytest.mark.asyncio
async def test_cached_ids_under_refuses_an_out_of_step_cache(library):
    from soniqboom.api import fstree
    root, store, _ = library
    await scanner._run_scan([str(root)])
    h = path_hash(str(root))
    fstree._get_or_build_scan_root_sorted(store, h)
    prefix = str(root / "B") + os.sep
    assert fstree.cached_ids_under(store, h, prefix)
    store.upsert_tracks_batch([{"id": "zz", "path": str(root / "B" / "zz.mod"), "title": "Z",
                                "scan_root_hash": h}])            # a write that bypassed the patch
    assert fstree.cached_ids_under(store, h, prefix) is None
    store.delete_track_ids(["zz"])


def test_a_nested_failure_key_matches_normalised_member_paths():
    key = str(Path("/lib/B/pack.zip::sub//n.zip"))
    member = str(Path("/lib/B/pack.zip::sub//n.zip::deep.mod"))
    assert scanner._in_failed_archive(member, {key})
    assert not scanner._in_failed_archive(str(Path("/lib/B/pack.zip::top.mod")), {key})


# ── a rescan stores a checksum the stored track lacks ───────────────────────

@pytest.mark.asyncio
async def test_rescan_stores_a_missing_checksum(library):
    """When a scan re-extracts every file of a root (≤ 2 % of it indexed — a
    big batch of new files), an unchanged file whose stored track lacks a
    checksum (indexed by an older build) gets it written; an unchanged track
    that already has everything is not rewritten."""
    root, store, _ = library
    make_mod(root / "B" / "tune.mod", title=b"tune")
    make_mod(root / "B" / "kept.mod", title=b"kept")
    await scanner._run_scan([str(root)])
    ids = {t["path"].rsplit("/", 1)[-1]: t["id"] for t in store.all_tracks()}
    md5 = store.get_track(ids["tune.mod"]).get("file_md5")
    assert md5
    store._tracks[ids["tune.mod"]].pop("file_md5")
    kept = dict(store.get_track(ids["kept.mod"]))
    (root / "C").mkdir()
    for i in range(300):                       # the root is now mostly new files
        make_mod(root / "C" / f"n{i:03}.mod", title=b"n%03d" % i)
    await scanner._run_scan([str(root)])
    assert store.get_track(ids["tune.mod"]).get("file_md5") == md5
    assert store.get_track(ids["kept.mod"]) == kept, "an unchanged track must not be rewritten"


def test_amiga_sidmon_sid_gets_a_scene_checksum(tmp_path):
    import hashlib
    from soniqboom.core import metadata
    c64 = metadata.extract(make_psid(tmp_path / "tune.sid"), "c64")
    assert c64.sid_md5 and c64.file_md5 is None          # C64: the HVSC key only
    sidmon = Path(__file__).resolve().parents[1] / "internal/testdata/uade/SidMon 1/primitive.sid"
    if not sidmon.is_file():
        pytest.skip("local SidMon test module not available")
    m = metadata.extract(sidmon, "sidmon")
    assert m.sid_md5 is None
    assert m.file_md5 == hashlib.md5(sidmon.read_bytes()).hexdigest()


# ── a disk image cut short keeps its indexed members (QA round 8) ─────────

def _hd_adf_with_mod(p: Path, root):
    """A 1.76 MB HD OFS ADF holding one module, ``mod.tune``."""
    make_mod(root / "B" / "m.mod", title=b"adf")
    body = (root / "B" / "m.mod").read_bytes()
    (root / "B" / "m.mod").unlink()
    total, bs = 3520, 512
    img = bytearray(total * bs)
    img[0:4] = b"DOS\x00"

    def put32(blk, off, v):
        struct.pack_into(">I", img, blk * bs + off, v & 0xFFFFFFFF)
    rootb, hdr = total // 2, total // 2 + 2
    name = b"mod.tune"
    put32(rootb, 24, hdr)
    put32(hdr, bs - 4, (-3) & 0xFFFFFFFF)
    put32(hdr, 0x144, len(body))
    img[hdr * bs + bs - 80] = len(name)
    img[hdr * bs + bs - 79: hdr * bs + bs - 79 + len(name)] = name
    nblk = (len(body) + 487) // 488
    put32(hdr, 0x08, nblk)
    for i in range(nblk):
        db = 100 + i
        put32(hdr, bs - 204 - i * 4, db)
        chunk = body[i * 488:(i + 1) * 488]
        put32(db, 0x0C, len(chunk))
        img[db * bs + 24: db * bs + 24 + len(chunk)] = chunk
    p.write_bytes(bytes(img))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "scoped", "light"])
async def test_hd_adf_cut_short_keeps_its_members(library, mode):
    root, store, _ = library
    p = root / "B" / "Game.adf"
    _hd_adf_with_mod(p, root)
    assert diskimage.list_members(p, strict=True)
    await scanner._run_scan([str(root)])
    before = [x for x in _paths(store) if "Game.adf::" in x]
    assert before
    p.write_bytes(p.read_bytes()[:1_000_000])       # between the DD and HD sizes
    os.utime(p, (5, 5))
    if mode == "full":
        await scanner._run_scan([str(root)])
    elif mode == "scoped":
        await scanner._run_scan([str(root)], scope=_scope(root, "B/Game.adf"))
    else:
        await scanner._run_scan([str(root)], light=True)
    assert [x for x in _paths(store) if "Game.adf::" in x] == before


@pytest.mark.parametrize("size,whole", [
    (901120, True), (912384, True), (1802240, True),     # DD, 81 cylinders, HD
    (450560, False), (1_000_000, False), (1_100_800, False), (1_901_120, False)])
def test_adf_whole_image_sizes(size, whole):
    assert diskimage._adf_whole(size) is whole


@pytest.mark.asyncio
async def test_a_40_track_d64_cut_above_the_35_track_size_keeps_its_members(library, tmp_path):
    root, store, _ = library
    make_psid(tmp_path / "x.sid")
    p = root / "B" / "game40.d64"
    _make_d64(p, (tmp_path / "x.sid").read_bytes())
    p.write_bytes(p.read_bytes() + bytes(196608 - 174848))       # a 40-track image
    await scanner._run_scan([str(root)])
    before = [x for x in _paths(store) if "game40.d64::" in x]
    assert before
    p.write_bytes(p.read_bytes()[:185000])          # longer than a 35-track image, not whole
    os.utime(p, (1, 1))
    await scanner._run_scan([str(root)], scope=_scope(root, "B/game40.d64"))
    assert [x for x in _paths(store) if "game40.d64::" in x] == before


@pytest.mark.parametrize("ext,size,whole", [
    (".d64", 174848, True), (".d64", 206114, True), (".d64", 185000, False),
    (".d71", 349696, True), (".d71", 350000, False),
    (".d81", 822400, True), (".d81", 820000, False)])
def test_cbm_whole_image_sizes(tmp_path, ext, size, whole):
    p = tmp_path / f"x{ext}"
    p.write_bytes(bytes(size))
    assert diskimage._looks_like_an_image(p) is whole


# ── perf round 7: the folder-click refresh writes in small chunks ──────────

@pytest.mark.asyncio
async def test_drill_down_writes_small_chunks_with_loop_turns(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    for i in range(60):
        make_mod(root / "B" / f"n{i:02}.mod", title=b"n%02d" % i)
    sizes: list[int] = []
    marks: list[int] = []
    turns = [0]
    real = scanner.upsert_tracks_batch

    async def spy(chunk):
        sizes.append(len(chunk))
        marks.append(turns[0])
        return await real(chunk)
    monkeypatch.setattr(scanner, "upsert_tracks_batch", spy)

    async def ticker():
        while True:
            turns[0] += 1
            await asyncio.sleep(0)
    tk = asyncio.get_running_loop().create_task(ticker())
    try:
        res = await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    finally:
        tk.cancel()
    assert res["added"] == 60 and not res["skipped"]
    assert len(sizes) >= 3 and max(sizes) <= 25 and sum(sizes) == 60
    assert all(b > a for a, b in zip(marks, marks[1:])), "no loop turn between chunk writes"
    assert sum(1 for p in _paths(store) if p.startswith("B/n")) == 60


@pytest.mark.asyncio
async def test_drill_down_stops_writing_once_the_root_is_removed(library, monkeypatch):
    root, store, _ = library
    await scanner._run_scan([str(root)])
    for i in range(60):
        make_mod(root / "B" / f"n{i:02}.mod", title=b"n%02d" % i)
    real = scanner.upsert_tracks_batch
    calls = [0]

    async def remove_after_first(chunk):
        out = await real(chunk)
        calls[0] += 1
        if calls[0] == 1:                    # the folder is removed between chunks
            store._scan_dirs.pop(str(root), None)
        return out
    monkeypatch.setattr(scanner, "upsert_tracks_batch", remove_after_first)
    res = await scanner.refresh_subtree_under_root(str(root), str(root / "B"))
    assert calls[0] == 1 and res["skipped"]
    assert sum(1 for p in _paths(store) if p.startswith("B/n")) == scanner._DRILL_WRITE_CHUNK



@pytest.mark.asyncio
@pytest.mark.parametrize("cut", [901120, 912384])
async def test_hd_adf_cut_at_a_whole_dd_size_keeps_its_members(library, cut):
    """Cut at exactly a DD size, an HD image reads as a DD disk without a root
    block — damaged, not an empty disk (QA round 9)."""
    root, store, _ = library
    p = root / "B" / "Game.adf"
    _hd_adf_with_mod(p, root)
    for blk, off, v in ((1760, 0, 2), (1760, 508, 1)):      # a proper HD root block
        data = bytearray(p.read_bytes())
        struct.pack_into(">i", data, blk * 512 + off, v)
        p.write_bytes(bytes(data))
    await scanner._run_scan([str(root)])
    before = [x for x in _paths(store) if "Game.adf::" in x]
    assert before
    p.write_bytes(p.read_bytes()[:cut])
    os.utime(p, (5, 5))
    await scanner._run_scan([str(root)], scope=_scope(root, "B/Game.adf"))
    assert [x for x in _paths(store) if "Game.adf::" in x] == before



@pytest.mark.asyncio
async def test_an_unlistable_dos_adf_is_unreadable_but_keeps_the_fingerprint(library):
    """An 81-cylinder AmigaDOS image can't be listed by this reader: it counts
    as unreadable (a damaged copy's members would be kept), and as it has no
    indexed members the root's listing fingerprint is still recorded."""
    root, store, _ = library
    _ext_adf(root / "B" / "ext81.adf")
    assert not diskimage._looks_like_an_image(root / "B" / "ext81.adf")
    fa = set()
    scanner._find_audio_files([str(root)], failed_archives=fa)
    assert any(a.endswith("ext81.adf") for a in fa)
    await scanner._run_scan([str(root)])
    assert store.get_config(scanner._FP_KEY + path_hash(str(root))) is not None


@pytest.mark.asyncio
async def test_a_game_disk_without_a_filesystem_scans_quietly(library, caplog):
    """A bootable trackloader disk (DOS boot block, no root block) isn't a
    disk this reader can list — routine, logged at debug level; a damaged
    image that still holds indexed tracks is warned about once per scan."""
    import logging
    root, store, _ = library
    img = bytearray(os.urandom(901120))
    img[0:4] = b"DOS\x00"
    (root / "B" / "game.adf").write_bytes(bytes(img))
    with caplog.at_level(logging.DEBUG, logger="soniqboom.core.scanner"):
        await scanner._run_scan([str(root)])
        await scanner._run_scan([str(root)], light=True)
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING and "game.adf" in r.getMessage()]
    assert not warned
    assert any("game.adf" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    # a damaged HD image that holds indexed tracks: one warning per scan
    p = root / "B" / "hdgame.adf"
    _hd_adf_with_mod(p, root)
    for blk, off, v in ((1760, 0, 2), (1760, 508, 1)):
        data = bytearray(p.read_bytes())
        struct.pack_into(">i", data, blk * 512 + off, v)
        p.write_bytes(bytes(data))
    await scanner._run_scan([str(root)])
    p.write_bytes(p.read_bytes()[:901120])
    os.utime(p, (5, 5))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="soniqboom.core.scanner"):
        await scanner._run_scan([str(root)])
    msgs = [r.getMessage() for r in caplog.records if "keep their indexed tracks" in r.getMessage()]
    assert len(msgs) == 1 and "hdgame.adf" in msgs[0] and "/game.adf" not in msgs[0]
