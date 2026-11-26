# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A moved or renamed file keeps its user data (opportunity review v2.2.0,
defect 1).

A track id is ``uuid5`` of its path (of ``<scan root>:<path>`` on a share), so
a move gives a file a new id.  A scan that no longer lists a stored file and
lists the same file under a new path (``core/track_moves``: checksum or
content key plus size; size, time and tags for rows without one) hands its
rating, plays, history, playlist entries, waveform, Subsonic stars and parked
playlist entries over, and the new row keeps when it was added and the user's
edits.  Covered: a full scan (a folder renamed), the watcher's MOVED event (a
scoped scan of both paths), a local HVSC update that moved tunes (checksum,
new times), a folder click's refresh, a share added again under another
address (the old one offline, or removed with its tracks), and the cases that
must NOT move (two identical files, other bytes, another share path).

Fixtures are generated (WAV / ProTracker MOD / PSID) — no private test data."""
from __future__ import annotations

import math
import os
import shutil
import struct
import sys
import time
import wave
from pathlib import Path

import pytest

from soniqboom.core import scanner, track_moves


# ── Generated fixtures ──────────────────────────────────────────────────────

def make_wav(path: Path, secs: float = 1.0, freq: float = 440.0, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(secs * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)))
    return path


def make_mod(path: Path, title: bytes = b"synthetic", fill: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = b"square".ljust(22, b"\0") + struct.pack(">HBBHH", 512, 0, 64, 0, 512)
    samples += (b"\0" * 22 + struct.pack(">HBBHH", 0, 0, 0, 0, 1)) * 30
    pat = bytearray(1024)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, fill & 0xFF])
    path.write_bytes(title[:20].ljust(20, b"\0") + samples + bytes([1, 127]) + bytes(128)
                     + b"M.K." + bytes(pat) + bytes(([100] * 16 + [156] * 16) * 32))
    return path


def make_psid(path: Path, name: bytes = b"Tune", author: bytes = b"Someone") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = (b"PSID" + struct.pack(">HHHHHHHI", 2, 0x7C, 0, 0x1000, 0x1003, 1, 1, 0)
           + name.ljust(32, b"\0") + author.ljust(32, b"\0") + b"1989".ljust(32, b"\0")
           + struct.pack(">HBBBB", 0, 0, 0, 0, 0))
    path.write_bytes(hdr + struct.pack("<H", 0x1000) + b"\x60" * 8)
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch, tmp_data_dir):
    """A fresh store, a fresh Subsonic state and a library folder."""
    from soniqboom.core import store as store_mod, subsonic_state
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(subsonic_state, "_state", None, raising=False)
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _none)
    root = (tmp_path / "lib")
    root.mkdir()
    return root.resolve(), store_mod._store


def _by_rel(store, root: Path) -> dict:
    return {os.path.relpath(t["path"], str(root)): tid for tid, t in store._tracks.items()}


async def _give_user_data(store, tid: str) -> None:
    """Everything a listener leaves on a track."""
    from soniqboom.core.store import PARKED_PLAYLIST_IDS
    from soniqboom.core.subsonic_state import get_state
    store.set_rating(tid, 4)
    store.record_play(tid, uid="u1")
    store.push_history({"track_id": tid, "ts": int(time.time())}, uid="u1")
    store.create_playlist("p1", "Mix", [tid, {"id": tid, "subsong": 2}], owner_user_id="u1")
    store.store_waveform(tid, [0.1, 0.9, 0.4])
    store.update_track_fields(tid, {"title": "My own title", "user_edited": ["title"],
                                    "added_at": 1_000_000})
    store.set_config(PARKED_PLAYLIST_IDS, [tid])
    await get_state().star("u1", "song", [tid])


async def _assert_user_data_on(store, new: str, old: str) -> None:
    from soniqboom.core.store import PARKED_PLAYLIST_IDS
    from soniqboom.core.subsonic_state import get_state
    assert store.get_rating(new) == 4 and store.get_rating(old) == 0
    assert store.get_play_stats(new, uid="u1").get("count") == 1
    assert store.get_play_stats(old, uid="u1").get("count") == 0
    assert [e["track_id"] for e in store.get_history(10, uid="u1")] == [new]
    assert store.get_playlist("p1")["track_ids"] == [new, {"id": new, "subsong": 2}]
    assert store.get_waveform(new) == [0.1, 0.9, 0.4] and store.get_waveform(old) is None
    t = store.get_track(new)
    assert t["title"] == "My own title" and t["user_edited"] == ["title"]
    assert t["added_at"] == 1_000_000, "a moved file showed up as newly added"
    assert store.get_config(PARKED_PLAYLIST_IDS) == [new]
    assert set(get_state().starred("u1", "song")) == {new}
    assert old not in store._tracks


# ── A full scan: a folder renamed ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_renamed_folder_keeps_all_user_data(lib):
    root, store = lib
    make_wav(root / "Album" / "a.wav", freq=330)
    make_wav(root / "Album" / "b.wav", freq=440)
    await scanner._run_scan([str(root)])
    old = _by_rel(store, root)["Album/a.wav"]
    assert store.get_track(old).get("content_key"), "the scan made no content key"
    await _give_user_data(store, old)
    (root / "Album").rename(root / "Album (2001)")
    assert await scanner._run_scan([str(root)]) is True
    ids = _by_rel(store, root)
    assert sorted(ids) == ["Album (2001)/a.wav", "Album (2001)/b.wav"]
    new = ids["Album (2001)/a.wav"]
    assert new != old
    await _assert_user_data_on(store, new, old)
    assert not store._absent_ids, "a moved file was kept as missing"
    assert store.verify_indexes()["index_ok"]


@pytest.mark.asyncio
async def test_a_moved_file_with_other_bytes_is_a_new_file(lib):
    """Same name, size, length and file time — only the bytes differ: the
    content keys disagree, so it is not the same file."""
    root, store = lib
    make_wav(root / "A" / "x.wav", freq=440)
    make_wav(root / "A" / "keep.wav", secs=0.5)
    await scanner._run_scan([str(root)])
    old = _by_rel(store, root)["A/x.wav"]
    t_old = store.get_track(old)
    store.set_rating(old, 5)
    (root / "A" / "x.wav").unlink()
    make_wav(root / "B" / "x.wav", freq=880)                # another recording, same size
    os.utime(root / "B" / "x.wav", (t_old["mtime"], t_old["mtime"]))
    await scanner._run_scan([str(root)])
    ids = _by_rel(store, root)
    new = store.get_track(ids["B/x.wav"])
    assert new["file_size"] == t_old["file_size"] and int(new["mtime"]) == int(t_old["mtime"])
    assert new["content_key"] != t_old["content_key"]
    assert "A/x.wav" not in ids                             # (its folder holds files: deleted)
    assert store.get_rating(ids["B/x.wav"]) == 0 and store.get_rating(old) == 5


@pytest.mark.asyncio
async def test_two_identical_files_are_not_guessed(lib):
    """Two old rows with the same bytes and tags and two new rows: which went
    where is unknown — nothing is handed over (the old rows wait as missing).
    (Same bytes, other names: the names — the titles — tell them apart.)"""
    root, store = lib
    make_wav(root / "Dup" / "x" / "one.wav", freq=500)
    (root / "Dup" / "y").mkdir()
    shutil.copy2(root / "Dup" / "x" / "one.wav", root / "Dup" / "y" / "one.wav")
    shutil.copy2(root / "Dup" / "x" / "one.wav", root / "Dup" / "x" / "two.wav")
    await scanner._run_scan([str(root)])
    ids = _by_rel(store, root)
    store.set_rating(ids["Dup/x/one.wav"], 5)
    store.set_rating(ids["Dup/x/two.wav"], 2)
    (root / "Dup").rename(root / "Dupes")
    await scanner._run_scan([str(root)])
    now = _by_rel(store, root)
    assert store.get_rating(now["Dupes/x/one.wav"]) == 0
    assert store.get_rating(now["Dupes/y/one.wav"]) == 0
    assert store.get_rating(ids["Dup/x/one.wav"]) == 5
    assert {ids["Dup/x/one.wav"], ids["Dup/y/one.wav"]} <= store._absent_ids
    assert store.get_rating(now["Dupes/x/two.wav"]) == 2      # (the only "two")


@pytest.mark.asyncio
async def test_rows_indexed_before_content_keys_move_by_size_time_and_tags(lib):
    """Warm path with another input: rows without a content key (indexed by
    2.2.0) — the weak identity decides: a row whose title the user edited
    (the store's title is not the file's) still matches by size, time and
    format; a file rewritten on the way (same size, another time) does
    not."""
    root, store = lib
    make_wav(root / "Old" / "p.wav", freq=600)
    make_wav(root / "Old" / "e.wav", freq=650)
    make_mod(root / "Old" / "q.mod", title=b"Q tune")
    await scanner._run_scan([str(root)])
    ids = _by_rel(store, root)
    for tid in ids.values():                                # (as 2.2.0 stored them)
        store._tracks[tid].pop("content_key", None)
        store._tracks[tid].pop("file_md5", None)
    store.set_rating(ids["Old/p.wav"], 3)
    store.update_track_fields(ids["Old/e.wav"], {"title": "Mine", "user_edited": ["title"]})
    store.set_rating(ids["Old/e.wav"], 4)
    store.set_rating(ids["Old/q.mod"], 2)
    (root / "Old").rename(root / "New")
    make_mod(root / "New" / "q.mod", title=b"Q retag")      # same size, another time
    os.utime(root / "New" / "q.mod", (time.time() - 10, time.time() - 10))
    await scanner._run_scan([str(root)])
    now = _by_rel(store, root)
    assert store.get_rating(now["New/p.wav"]) == 3
    assert store.get_rating(now["New/e.wav"]) == 4
    assert store.get_track(now["New/e.wav"])["title"] == "Mine"
    assert store.get_rating(now["New/q.mod"]) == 0 and store.get_rating(ids["Old/q.mod"]) == 2


@pytest.mark.asyncio
async def test_a_local_hvsc_update_that_moved_tunes_keeps_their_data(lib):
    """An HVSC update unpacked over the old tree: tunes moved to another
    folder with new file times — the SID checksum (``sid_md5``) decides."""
    root, store = lib
    make_psid(root / "MUSICIANS" / "U" / "Unknown" / "Ninja.sid", b"Ninja")
    make_psid(root / "MUSICIANS" / "U" / "Unknown" / "Other.sid", b"Other")
    make_psid(root / "MUSICIANS" / "H" / "Hubbard_Rob" / "Delta.sid", b"Delta")
    await scanner._run_scan([str(root)])
    ids = _by_rel(store, root)
    old = ids["MUSICIANS/U/Unknown/Ninja.sid"]
    assert store.get_track(old).get("sid_md5")
    store.set_rating(old, 5)
    store.record_play(old, uid="u1")
    dst = root / "MUSICIANS" / "T" / "Tel_Jeroen"
    dst.mkdir(parents=True)
    shutil.move(str(root / "MUSICIANS/U/Unknown/Ninja.sid"), str(dst / "Ninja.sid"))
    later = time.time() + 5
    os.utime(dst / "Ninja.sid", (later, later))             # (the update's own time)
    await scanner._run_scan([str(root)])
    now = _by_rel(store, root)
    new = now["MUSICIANS/T/Tel_Jeroen/Ninja.sid"]
    assert store.get_rating(new) == 5 and store.get_play_stats(new, uid="u1")["count"] == 1
    assert old not in store._tracks


# ── The folder watcher's MOVED event ────────────────────────────────────────

@pytest.fixture
def watched(lib, monkeypatch):
    """The watcher's real event → debounce → scoped-scan path, the queued
    scan run at once (``start_scan`` → ``_run_scan`` with the same scope)."""
    pytest.importorskip("watchdog")
    import asyncio
    from soniqboom.core import watcher
    root, store = lib
    scans: list = []

    async def run_now(dirs, on_progress=None, *, scope=None, rescan_if_running=False,
                      light=False):
        scans.append({k: sorted(v) for k, v in (scope or {}).items()})
        await scanner._run_scan(list(dirs), scope=scope)
    monkeypatch.setattr(scanner, "start_scan", run_now)
    monkeypatch.setattr(watcher, "_QUIET_SEC", 0.1)
    monkeypatch.setattr(watcher, "_MAX_WAIT_SEC", 5.0)
    monkeypatch.setattr(watcher._state, "pending", {})
    monkeypatch.setattr(watcher._state, "watches", {str(root): object()})
    monkeypatch.setattr(watcher._state, "debounce_task", None)
    monkeypatch.setattr(watcher, "_mark_dirty",
                        lambda r, paths=None: watcher._note_change(r, list(paths or [])))

    async def settle():
        while watcher._state.debounce_task is not None and not watcher._state.debounce_task.done():
            await asyncio.sleep(0.05)
    return root, store, watcher._Handler(str(root)), scans, settle


@pytest.mark.asyncio
async def test_a_watcher_move_event_is_one_move(watched):
    from watchdog.events import DirMovedEvent, FileMovedEvent
    root, store, h, scans, settle = watched
    make_mod(root / "In" / "song.mod", title=b"Song")
    make_mod(root / "In" / "other.mod", title=b"Other")
    make_wav(root / "Disc" / "w.wav")
    await scanner._run_scan([str(root)])
    ids = _by_rel(store, root)
    store.set_rating(ids["In/song.mod"], 4)
    store.set_rating(ids["Disc/w.wav"], 2)
    # A file renamed (cold) and a folder renamed (warm, another input).
    (root / "In" / "song.mod").rename(root / "In" / "song (remix).mod")
    h.on_any_event(FileMovedEvent(str(root / "In" / "song.mod"),
                                  str(root / "In" / "song (remix).mod")))
    (root / "Disc").rename(root / "Disc 1")
    h.on_any_event(DirMovedEvent(str(root / "Disc"), str(root / "Disc 1")))
    await settle()
    assert len(scans) == 1 and len(scans[0][str(root)]) == 4   # one scan, both sides
    now = _by_rel(store, root)
    assert store.get_rating(now["In/song (remix).mod"]) == 4
    assert store.get_rating(now["Disc 1/w.wav"]) == 2
    assert "In/song.mod" not in now and "Disc/w.wav" not in now
    assert not store._absent_ids


@pytest.mark.asyncio
async def test_a_move_scanned_in_two_passes_still_moves(lib):
    """The two halves of a move in two scans (the watcher scans a folder still
    receiving events later; a walk that hit a read error prunes nothing):
    the source first — the old rows wait as missing, the destination's scan
    pairs them (cold) — or the destination first — the source's scan pairs
    its orphans with the rows added lately (warm, other input)."""
    root, store = lib
    make_mod(root / "A" / "a.mod", title=b"A")
    make_mod(root / "B" / "b.mod", title=b"B")
    make_wav(root / "Keep" / "k.wav")
    await scanner._run_scan([str(root)])
    ids = _by_rel(store, root)
    store.update_track_fields_batch([(t, {"added_at": 1_000_000}) for t in ids.values()])
    store.set_rating(ids["A/a.mod"], 5)
    store.set_rating(ids["B/b.mod"], 3)
    scope = lambda *rel: {str(root): frozenset(str(root / r) for r in rel)}  # noqa: E731
    (root / "A").rename(root / "A2")
    await scanner._run_scan([str(root)], scope=scope("A"))          # source only
    assert ids["A/a.mod"] in store._absent_ids
    await scanner._run_scan([str(root)], scope=scope("A2"))         # then the destination
    now = _by_rel(store, root)
    assert store.get_rating(now["A2/a.mod"]) == 5 and ids["A/a.mod"] not in store._tracks
    (root / "B").rename(root / "B2")
    await scanner._run_scan([str(root)], scope=scope("B2"))         # destination first
    assert store.get_rating(_by_rel(store, root)["B2/b.mod"]) == 0
    await scanner._run_scan([str(root)], scope=scope("B"))          # then the source
    now = _by_rel(store, root)
    assert store.get_rating(now["B2/b.mod"]) == 3 and ids["B/b.mod"] not in store._tracks
    assert not store._absent_ids


# ── A folder click's refresh ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_deleted_copy_never_hands_its_data_to_another_copy(lib):
    """Two copies of one file indexed together, one deleted later: the other
    copy is not where it went (it was there all along) — its own data stays
    its own; the deleted copy's rows go with it."""
    root, store = lib
    make_mod(root / "One" / "same.mod", title=b"Same")
    make_mod(root / "Two" / "same.mod", title=b"Same")
    make_wav(root / "One" / "keep.wav")
    await scanner._run_scan([str(root)])
    ids = _by_rel(store, root)
    store.set_rating(ids["One/same.mod"], 5)
    store.set_rating(ids["Two/same.mod"], 1)
    (root / "One" / "same.mod").unlink()
    await scanner._run_scan([str(root)])
    assert store.get_rating(ids["Two/same.mod"]) == 1
    assert ids["One/same.mod"] not in store._tracks


@pytest.mark.asyncio
async def test_a_folder_refresh_hands_a_renamed_subfolder_over(lib):
    root, store = lib
    make_mod(root / "Box" / "CD1" / "t.mod", title=b"T")
    make_mod(root / "Box" / "CD2" / "u.mod", title=b"U")
    await scanner._run_scan([str(root)])
    old = _by_rel(store, root)["Box/CD1/t.mod"]
    store.set_rating(old, 3)
    (root / "Box" / "CD1").rename(root / "Box" / "Disc 1")
    res = await scanner.refresh_subtree_under_root(str(root), str(root / "Box"))
    now = _by_rel(store, root)
    assert store.get_rating(now["Box/Disc 1/t.mod"]) == 3
    assert old not in store._tracks and res["removed"] == 1


# ── A share added again under another address ───────────────────────────────

sys.path.insert(0, str(Path(__file__).parent))
from _webdav_server import DavServer  # noqa: E402


class _Dav:
    """A WebDAV server on ``lib`` and its source registered as a scan root
    (each instance: another port — another address of the same share)."""

    def __init__(self, folder: Path):
        from soniqboom.core.filesource import register_source
        from soniqboom.core.filesource_webdav import WebDAVFileSource
        self.srv = DavServer(folder).start()
        self.root = self.srv.url
        self.src = WebDAVFileSource(self.root + "/")
        register_source(self.root, self.src)

    def offline(self):
        from soniqboom.core.filesource import remove_source
        remove_source(self.root)                     # (no source: the address is down)

    def stop(self):
        self.offline()
        self.srv.stop()


@pytest.fixture
def share_files(tmp_path):
    d = tmp_path / "nas"
    make_mod(d / "Amiga" / "a.mod", title=b"A")
    make_mod(d / "Amiga" / "b.mod", title=b"B")
    make_wav(d / "Rips" / "c.wav")
    old = time.time() - 3600
    for p in d.rglob("*"):
        os.utime(p, (old, old))
    return d


async def _scan_share(sh) -> dict:
    return await scanner.start_remote_scan("", sh.root, sh.src)


def _share_ids(store, root: str) -> dict:
    return {t["path"][len(root) + 1:]: tid for tid, t in store._tracks.items()
            if t["path"].startswith(root + ":")}


@pytest.mark.asyncio
async def test_a_share_added_under_a_new_address_takes_the_offline_ones_data(lib, share_files):
    _root, store = lib
    a, b = _Dav(share_files), _Dav(share_files)
    try:
        await _scan_share(a)
        ida = _share_ids(store, a.root)
        assert sorted(ida) == ["/Amiga/a.mod", "/Amiga/b.mod", "/Rips/c.wav"]
        await _give_user_data(store, ida["/Amiga/a.mod"])
        store.set_rating(ida["/Rips/c.wav"], 1)
        a.offline()                                         # the old address is gone
        await _scan_share(b)
        idb = _share_ids(store, b.root)
        assert sorted(idb) == sorted(ida)
        await _assert_user_data_on_share(store, idb["/Amiga/a.mod"], ida["/Amiga/a.mod"])
        assert store.get_rating(idb["/Rips/c.wav"]) == 1
    finally:
        a.stop()
        b.stop()


async def _assert_user_data_on_share(store, new: str, old: str, *, waveform: bool = True) -> None:
    from soniqboom.core.subsonic_state import get_state
    assert store.get_rating(new) == 4 and store.get_rating(old) == 0
    assert store.get_play_stats(new, uid="u1")["count"] == 1
    assert store.get_playlist("p1")["track_ids"][0] == new
    # (a purged row's waveform went with it — made again at the next play)
    assert store.get_waveform(new) == ([0.1, 0.9, 0.4] if waveform else None)
    t = store.get_track(new)
    assert t["title"] == "My own title" and t["added_at"] == 1_000_000
    assert set(get_state().starred("u1", "song")) == {new}


@pytest.mark.asyncio
async def test_a_share_removed_then_added_under_a_new_address_takes_its_data(lib, share_files):
    """Cold path with another input: the old address was removed with its
    tracks first — its rows with user data were kept aside
    (``remember_removed_root``) and the new address takes them; the entry is
    used up."""
    from soniqboom.core import data
    _root, store = lib
    a, b = _Dav(share_files), _Dav(share_files)
    try:
        await _scan_share(a)
        ida = _share_ids(store, a.root)
        await _give_user_data(store, ida["/Amiga/b.mod"])
        store.set_rating(ida["/Rips/c.wav"], 2)             # (no checksum: time + format)
        a.offline()
        store.delete_scan_dir(a.root)                       # (the removal: dropped,
        await data.delete_tracks_by_scan_root(a.root)       #  then its tracks purged)
        assert not _share_ids(store, a.root)
        kept = store.get_config(track_moves.REMOVED_ROOTS_KEY)
        assert list(kept) == [a.root] and sorted(kept[a.root]["rows"]) == ["/Amiga/b.mod",
                                                                           "/Rips/c.wav"]
        await _scan_share(b)
        idb = _share_ids(store, b.root)
        await _assert_user_data_on_share(store, idb["/Amiga/b.mod"], ida["/Amiga/b.mod"],
                                         waveform=False)
        assert store.get_rating(idb["/Amiga/a.mod"]) == 0
        assert store.get_rating(idb["/Rips/c.wav"]) == 2
        assert not store.get_config(track_moves.REMOVED_ROOTS_KEY), "the kept rows were not used up"
    finally:
        a.stop()
        b.stop()


@pytest.mark.asyncio
async def test_a_folder_renamed_on_the_share_keeps_its_data(lib, share_files):
    _root, store = lib
    a = _Dav(share_files)
    try:
        await _scan_share(a)
        ida = _share_ids(store, a.root)
        store.set_rating(ida["/Amiga/a.mod"], 5)
        store.set_rating(ida["/Rips/c.wav"], 2)
        (share_files / "Amiga").rename(share_files / "Amiga 500")
        (share_files / "Rips").rename(share_files / "Rips 2")
        await _scan_share(a)
        now = _share_ids(store, a.root)
        assert sorted(now) == ["/Amiga 500/a.mod", "/Amiga 500/b.mod", "/Rips 2/c.wav"]
        assert store.get_rating(now["/Amiga 500/a.mod"]) == 5
        assert store.get_rating(now["/Rips 2/c.wav"]) == 2
    finally:
        a.stop()


@pytest.mark.asyncio
async def test_a_folder_being_removed_hands_its_data_to_its_new_path(lib, tmp_path):
    """The old folder is still being removed (its rows in the library, and
    already in the removed roots' registry while its purge runs): found
    twice, it is still one old row — handed over, the registry used up."""
    from soniqboom.core import removals
    root, store = lib
    make_mod(root / "X" / "x.mod", title=b"X")
    make_wav(root / "Y" / "y.wav")
    await scanner._run_scan([str(root)])
    old = _by_rel(store, root)["X/x.mod"]
    store.set_rating(old, 4)
    moved = tmp_path / "Music2"
    shutil.copytree(root, moved)
    store.set_config(removals.KEY, {str(root): {"share_id": None, "at": time.time(), "total": 2}})
    scanner.remember_removed_root(store, str(root), list(store._tracks))
    await scanner._run_scan([str(moved.resolve())])
    new = _by_rel(store, moved.resolve())["X/x.mod"]
    assert store.get_rating(new) == 4
    assert str(root) not in (store.get_config(track_moves.REMOVED_ROOTS_KEY) or {})
    store.delete_config(removals.KEY)


@pytest.mark.asyncio
async def test_handing_many_files_over_keeps_the_loop_responsive(lib):
    """10,000 moved rows taking their old rows' fields: the writes re-key the
    added-at order, so they go in a batch section with turns for the loop
    (measured: max stall ~15 ms on the dev Mac; before, ~2 s)."""
    import asyncio
    root, store = lib
    olds, news = [], []
    for i in range(10_000):
        o = {"id": f"o{i}", "path": f"/gone/{i}.flac", "title": f"T{i}", "format": "FLAC",
             "added_at": 1_000_000 + i, "scan_root_hash": "r", "file_size": 1000 + i}
        n = dict(o, id=f"n{i}", path=f"/here/{i}.flac", added_at=2_000_000_000 + i)
        olds.append(o)
        news.append(n)
    store.upsert_tracks_batch(olds + news)
    for i in range(0, 10_000, 7):
        store.set_rating(f"o{i}", 3)
    worst = [0.0]
    done = [False]

    async def tick():
        while not done[0]:
            t0 = time.perf_counter()
            await asyncio.sleep(0.002)
            worst[0] = max(worst[0], time.perf_counter() - t0 - 0.002)
    t = asyncio.create_task(tick())
    await asyncio.sleep(0.01)
    n = await scanner._hand_over_user_data({f"o{i}": f"n{i}" for i in range(10_000)}, "Test")
    done[0] = True
    await t
    assert n == 10_000 and store.get_track("n9999")["added_at"] == 1_009_999
    assert store.get_rating("n7") == 3 and store.verify_indexes()["index_ok"]
    assert worst[0] < 0.5, f"the loop stalled {worst[0] * 1000:.0f} ms"


@pytest.mark.asyncio
async def test_another_share_path_or_other_files_take_nothing(lib, share_files, tmp_path):
    _root, store = lib
    other = tmp_path / "other"
    make_mod(other / "Amiga" / "a.mod", title=b"Not A")     # same path, other file
    a, b = _Dav(share_files), _Dav(other)
    try:
        await _scan_share(a)
        ida = _share_ids(store, a.root)
        store.set_rating(ida["/Amiga/a.mod"], 5)
        a.offline()
        await _scan_share(b)
        idb = _share_ids(store, b.root)
        assert store.get_rating(idb["/Amiga/a.mod"]) == 0
        assert store.get_rating(ida["/Amiga/a.mod"]) == 5
    finally:
        a.stop()
        b.stop()
    # another share path: never a candidate
    assert track_moves.share_key("smb://10.0.0.5/Music") == track_moves.share_key("smb://nas/music")
    assert track_moves.share_key("smb://10.0.0.5/Music") != track_moves.share_key("smb://nas/Other")
    assert track_moves.share_key("ftp://h/pub") != track_moves.share_key("smb://h/pub")
    assert track_moves.share_key("/Volumes/Music") is None


@pytest.mark.asyncio
async def test_a_folder_removed_then_added_at_a_new_path_takes_its_data(lib, tmp_path):
    from soniqboom.core import data
    root, store = lib
    make_mod(root / "X" / "x.mod", title=b"X")
    make_wav(root / "Y" / "y.wav")
    await scanner._run_scan([str(root)])
    old = _by_rel(store, root)["X/x.mod"]
    store.set_rating(old, 4)
    moved = tmp_path / "Music2"
    shutil.copytree(root, moved)                            # (times kept)
    store.delete_scan_dir(str(root))
    await data.delete_tracks_by_scan_root(str(root))
    await scanner._run_scan([str(moved.resolve())])
    new = _by_rel(store, moved.resolve())["X/x.mod"]
    assert store.get_rating(new) == 4


# ── The pure helpers ────────────────────────────────────────────────────────

def _row(tid, **kw):
    base = {"id": tid, "file_size": 1000, "mtime": 1_700_000_000.4, "format": "FLAC",
            "duration": 200.0, "title": "T", "artist": "A", "album": "B"}
    base.update(kw)
    return base


def test_match_moves_strong_then_weak_and_only_unambiguous():
    old = {"o1": _row("o1", content_key="k1"), "o2": _row("o2", title="U"),
           "o3": _row("o3", file_md5="m3"), "o4": _row("o4", file_md5="m4", title="V")}
    new = {"n1": _row("n1", content_key="k1", mtime=5.0),   # strong: the time may differ
           "n2": _row("n2", title="U", content_key="k2"),  # weak: the old row has no key
           "n3": _row("n3", file_md5="other"),             # other bytes
           "n4": _row("n4", file_md5="other4", title="V")}  # weak fits, strong keys differ
    assert track_moves.match_moves(old, new) == {"o1": "n1", "o2": "n2"}
    # two candidates: nothing
    assert track_moves.match_moves({"a": _row("a", content_key="k"), "b": _row("b", content_key="k")},
                                   {"n": _row("n", content_key="k")}) == {}
    assert track_moves.match_moves({"a": _row("a")}, {"n": _row("n"), "m": _row("m")}) == {}


def test_carried_fields_take_the_users_and_the_learned_ones_not_the_paths():
    old = _row("o", added_at=5, user_edited=["title", "year"], title="Mine", year=1990,
               year_source="user", year_file=1989, health="broken", health_note="clicks",
               default_subsong=2, album_source="folder", album="FolderAlbum", duration=0.0,
               game_by_folder="G")
    new = _row("n", title="Tag", year=1989, added_at=99, duration=0.0)
    c = track_moves.carried_fields(old, new)
    assert c == {"added_at": 5, "title": "Mine", "user_edited": ["title", "year"],
                 "year": 1990, "year_source": "user", "year_file": 1989,
                 "health": "broken", "health_note": "clicks", "default_subsong": 2}


def test_file_changed_reads_size_checksum_then_time():
    a = _row("a", file_md5="x")
    assert not track_moves.file_changed(a, _row("a", file_md5="x", mtime=1.0))   # touched
    assert track_moves.file_changed(a, _row("a", file_md5="y"))
    assert track_moves.file_changed(a, _row("a", file_md5="x", file_size=1001))
    b = _row("b", content_key="k")                    # (no whole-file checksum: the time)
    assert not track_moves.file_changed(b, _row("b", content_key="k"))
    assert track_moves.file_changed(b, _row("b", content_key="k", mtime=1_700_000_100.0))
    assert not track_moves.file_changed(None, b)


def test_content_key_reads_head_and_tail(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"a" * 200_000)
    k = track_moves.content_key(str(p))
    q = tmp_path / "g.bin"
    q.write_bytes(b"a" * 100_000 + b"b" + b"a" * 99_999)     # (the middle: not read)
    r = tmp_path / "h.bin"
    r.write_bytes(b"a" * 199_999 + b"b")                      # (the tail: read)
    assert track_moves.content_key(str(q)) == k != track_moves.content_key(str(r))
    assert track_moves.content_key(str(tmp_path / "missing")) is None
