# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-4 enrichment fixes:

* a LONE tracker file's ``<game>-<part>`` name guess needs a tune-part word
  (``fleetwood mac-dreams.xm`` is no game), and the version-bumped re-apply
  withdraws the old guesses;
* remote console rips get their in-file game name once per share, after a
  completed scan of it (``repair.run_remote_album_backfill``);
* an album revert a restart interrupted resumes at startup
  (``folder_album.resume_pending_revert``);
* the Demozoo apply freezes a grown memo out of the cyclic GC;
* audio-format / disc-layer / platform container folders are generic names;
* the folder-album passes yield with a short timer, not ``sleep(0)``;
* the multi-tune default tune (``start_subsong``) is recorded at scan time;
* ``index_generation`` covers every index-feeding write; the deep-page memo
  never pins a replaced sorted list."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import struct
from pathlib import Path

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import metadata
from soniqboom.core import repair
from soniqboom.core import scanner
from soniqboom.core import scene_metadata as sm
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


async def _no_refresh(_ids):
    return None


async def _no_broadcast(_p):
    return None


# ── r4-enr-4: lone tracker files need a tune-part word ───────────────────────

_ROWS = [
    ("1" * 32, "Fasttracker 2/Steve Roz/fleetwood mac-dreams.xm"),
    ("2" * 32, "Protracker/Rico/i01-experience 1.mod"),
    ("3" * 32, "Protracker/Someone/huckleberry-title.mod"),
    ("4" * 32, "Fasttracker 2/Other/the hulk-end theme.xm"),
    ("5" * 32, "Protracker/Beast/beast2-ingame.mod"),
    ("6" * 32, "Protracker/Beast/beast2-intro.mod"),
]


def _index(tmp_path, rows):
    db = tmp_path / "modland.sqlite"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.executemany("INSERT INTO mods VALUES (?,?)", rows)
    con.commit()
    con.close()
    return db


@pytest.fixture
def sibs(tmp_path, monkeypatch):
    db = _index(tmp_path, _ROWS)
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    monkeypatch.setattr(sm, "_siblings_cache", None)
    return sm.modland_filename_siblings()


@pytest.mark.parametrize("path,title,expect", [
    ("Fasttracker 2/Steve Roz/fleetwood mac-dreams.xm", "dreams", None),
    ("Protracker/Rico/i01-experience 1.mod", "experience 1", None),
    ("Protracker/Someone/huckleberry-title.mod", "title", "Huckleberry"),
    ("Fasttracker 2/Other/the hulk-end theme.xm", "end theme", "The Hulk"),
    # Not lone: a sibling with the same game prefix in the same dir.
    ("Protracker/Beast/beast2-ingame.mod", "ingame", "Beast2"),
])
def test_a_lone_tracker_file_needs_a_part_word(sibs, path, title, expect):
    d, _, fn = path.rpartition("/")
    assert sm.filename_game(fn, title, siblings=sibs, modland_dir=d) == expect


def test_without_a_sibling_map_the_title_gate_alone_decides():
    assert sm.filename_game("fleetwood mac-dreams.xm", "dreams") == "Fleetwood Mac"


async def test_the_reapply_withdraws_a_stamped_lone_guess(tmp_path, monkeypatch):
    assert sm.MODLAND_APPLY_VERSION >= 5
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    db = _index(tmp_path, _ROWS)
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    monkeypatch.setattr(sm, "_siblings_cache", None)
    monkeypatch.setattr(sm, "_WITHDRAW_MIN_INDEX_ROWS", 1)
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    monkeypatch.setitem(sm._status, "applying", False)
    monkeypatch.setattr(sm, "_last_auto_sig", None)
    base = {"artist": "", "genre": [], "format": "FastTracker 2"}
    s.upsert_tracks_batch([
        {**base, "id": "fm", "path": "/m/fm.xm", "title": "dreams", "file_md5": "1" * 32,
         "album": "Fleetwood Mac", "album_source": fa.SOURCE_MODLAND_FILENAME},
        {**base, "id": "hk", "path": "/m/hk.xm", "title": "end theme", "file_md5": "4" * 32,
         "album": "The Hulk", "album_source": fa.SOURCE_MODLAND_FILENAME},
    ])
    s.set_config(sm.APPLY_VERSION_CONFIG_KEY, 4)
    assert not sm.apply_version_current()
    res = await sm.apply_to_library(auto=True)
    assert res.get("error") is None
    fm, hk = s.get_track("fm"), s.get_track("hk")
    assert (fm["album"], fm["album_source"]) == ("", None)
    assert (hk["album"], hk["album_source"]) == ("The Hulk", fa.SOURCE_MODLAND_FILENAME)
    assert s.get_config(sm.APPLY_VERSION_CONFIG_KEY) == sm.MODLAND_APPLY_VERSION


# ── r4-enr-5: remote console rips, once per share ────────────────────────────

_ROOT = "ftp://10.0.0.88/Music/Demo"
_RH = hashlib.sha256(_ROOT.encode()).hexdigest()[:16]


def _nsf_bytes(name: bytes = b"AXEL F") -> bytes:
    b = bytearray(0x80)
    b[0:5] = b"NESM\x1a"
    b[0x0E:0x2E] = name[:32].ljust(32, b"\x00")
    b[0x2E:0x4E] = b"Nullsleep".ljust(32, b"\x00")
    return bytes(b) + b"\x00" * 64


class _FakeSource:
    def __init__(self, files):
        self.files = files
        self.reads: list[str] = []

    def read_file(self, rel, lane="stream"):
        self.reads.append(rel)
        if rel not in self.files:
            raise OSError("gone")
        return self.files[rel]


@pytest.fixture
def remote(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    monkeypatch.setattr(repair, "_broadcast_progress", _no_broadcast)
    monkeypatch.setattr(repair, "_progress", repair.RepairProgress())
    monkeypatch.setattr(repair, "_backfill_settled", set())
    monkeypatch.setattr(scanner, "_remote_backfill_pending", set())
    src = _FakeSource({"/music/axel_f.nsf": _nsf_bytes()})
    from soniqboom.core import filesource
    monkeypatch.setattr(filesource, "get_source", lambda root: src if root == _ROOT else None)
    base = {"artist": "Nullsleep", "album": "", "album_source": None, "format": "NSF",
            "genre": ["Chiptune"], "scan_root_hash": _RH}
    s.upsert_tracks_batch([
        {**base, "id": "r1", "path": f"{_ROOT}:/music/axel_f.nsf", "title": "AXEL F"},
        # Another share: not this root's business.
        {**base, "id": "o1", "path": "ftp://other/x:/y.nsf", "title": "Y",
         "scan_root_hash": "f" * 16},
    ])
    return s, src


async def _wait_repair():
    for _ in range(500):
        # the task itself done (its finish — cache refresh, then the
        # done-callbacks that settle a one-time backfill — included)
        if not repair.is_running() and (repair._task is None or repair._task.done()):
            await asyncio.sleep(0)              # let the done-callback run
            return
        await asyncio.sleep(0.01)
    raise AssertionError("repair did not finish")


_PLAN = {"walked": 10, "extract": 0, "skip": 10, "mtime_refresh": 0, "full_walk_ok": True}


async def test_a_completed_remote_scan_fills_the_game_once(remote):
    s, src = remote
    assert scanner._queue_remote_album_backfill(_ROOT, _PLAN) is True
    await scanner._run_remote_album_backfills()
    await _wait_repair()
    t = s.get_track("r1")
    assert (t["album"], t["album_source"]) == ("AXEL F", "tag")
    assert s.get_track("o1")["album"] == ""                   # other share untouched
    assert src.reads == ["/music/axel_f.nsf"]
    assert s.get_config(repair.ALBUM_BACKFILL_ROOTS_CONFIG_KEY) == [_RH]
    assert scanner._remote_backfill_pending == set()
    # The next completed scan of the share: one config read, nothing queued.
    assert scanner._queue_remote_album_backfill(_ROOT, _PLAN) is False
    assert await repair.run_remote_album_backfill(_RH) is True
    assert src.reads == ["/music/axel_f.nsf"]


async def test_an_unreachable_file_keeps_the_share_pending(remote):
    s, src = remote
    src.files.clear()                                        # share dropped mid-run
    assert scanner._queue_remote_album_backfill(_ROOT, _PLAN) is True
    await scanner._run_remote_album_backfills()
    await _wait_repair()
    assert s.get_config(repair.ALBUM_BACKFILL_ROOTS_CONFIG_KEY) is None
    src.files["/music/axel_f.nsf"] = _nsf_bytes()           # back online
    assert scanner._queue_remote_album_backfill(_ROOT, _PLAN) is True
    await scanner._run_remote_album_backfills()
    await _wait_repair()
    assert s.get_track("r1")["album"] == "AXEL F"
    assert s.get_config(repair.ALBUM_BACKFILL_ROOTS_CONFIG_KEY) == [_RH]


async def test_a_busy_repair_task_defers_the_share(remote, monkeypatch):
    s, src = remote
    monkeypatch.setattr(repair, "is_running", lambda: True)
    scanner._queue_remote_album_backfill(_ROOT, _PLAN)
    await scanner._run_remote_album_backfills()
    assert scanner._remote_backfill_pending == {_RH}           # retried later
    assert src.reads == []


def test_a_first_full_scan_needs_no_backfill(remote):
    s, src = remote
    first = {"walked": 3, "extract": 3, "new": 3, "skip": 0, "mtime_refresh": 0,
             "full_walk_ok": True}
    assert scanner._queue_remote_album_backfill(_ROOT, first) is False
    assert s.get_config(repair.ALBUM_BACKFILL_ROOTS_CONFIG_KEY) == [_RH]
    assert src.reads == []


async def test_start_remote_scan_queues_the_backfill(remote, monkeypatch):
    s, _src = remote
    spawned = []
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda: spawned.append(1))
    monkeypatch.setattr(scanner, "_schedule_folder_album_pass", lambda: None)

    async def body(*_a, **_kw):
        return dict(_PLAN)                                   # a no-change poll
    monkeypatch.setattr(scanner, "_remote_scan_body", body)
    await scanner.start_remote_scan("share", _ROOT, None)
    assert spawned == [1] and scanner._remote_backfill_pending == {_RH}
    # An aborted scan (empty plan) never queues it.
    scanner._remote_backfill_pending.clear()

    async def dead(*_a, **_kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(scanner, "_remote_scan_body", dead)
    await scanner.start_remote_scan("share", _ROOT, None)
    assert scanner._remote_backfill_pending == set() and spawned == [1]


# ── r4-enr-6: an interrupted revert resumes at startup ───────────────────────

@pytest.fixture
def folder_env(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    monkeypatch.setattr(sm, "has_index", lambda: False)
    s.set_config(repair.ALBUM_BACKFILL_CONFIG_KEY, True)       # nothing else to run
    s.upsert_tracks_batch([
        {"id": f"f{i}", "path": f"/r/Game/{i}.mod", "title": f"t{i}", "artist": "",
         "album": "Game", "album_source": fa.SOURCE_FOLDER, "format": "ProTracker",
         "genre": []} for i in range(3)]
        + [{"id": "tag", "path": "/r/x.mod", "title": "x", "artist": "", "album": "Real",
            "album_source": None, "format": "ProTracker", "genre": []}])
    return s


async def _drain_fa_tasks():
    for _ in range(200):
        if not fa._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("revert did not finish")


async def test_an_interrupted_revert_resumes_at_startup(folder_env, monkeypatch):
    s = folder_env
    s.set_config(fa.CONFIG_KEY, False)
    # The revert crashed after its first chunk: the marker is still set.
    s.update_track_fields("f0", {"album": "", "album_source": None})
    s.set_config(fa.REVERT_PENDING_KEY, [fa.SOURCE_FOLDER])
    scanner.schedule_startup_enrichment()
    await _drain_fa_tasks()
    assert [s.get_track(f"f{i}")["album"] for i in range(3)] == ["", "", ""]
    assert s.get_track("tag")["album"] == "Real"
    assert s.get_config(fa.REVERT_PENDING_KEY) == []


async def test_a_revert_marker_for_a_switched_on_option_is_dropped(folder_env):
    s = folder_env
    s.set_config(fa.CONFIG_KEY, True)
    s.set_config(fa.REVERT_PENDING_KEY, [fa.SOURCE_FOLDER])
    assert fa.resume_pending_revert() is False
    assert s.get_config(fa.REVERT_PENDING_KEY) == []
    assert s.get_track("f1")["album"] == "Game"


async def test_the_marker_outlives_a_revert_that_dies(folder_env, monkeypatch):
    s = folder_env

    async def crash(*_a, **_kw):
        raise RuntimeError("killed mid-revert")
    monkeypatch.setattr(fa, "commit_album_updates", crash)
    with pytest.raises(RuntimeError):
        await fa.revert_album_source(fa.SOURCE_FOLDER)
    assert s.get_config(fa.REVERT_PENDING_KEY) == [fa.SOURCE_FOLDER]


def test_no_marker_costs_one_config_read(folder_env, monkeypatch):
    assert fa.resume_pending_revert() is False
    assert fa._tasks == set()


# ── r4-enr-12: folder-album passes yield with a timer ────────────────────────

async def test_folder_passes_yield_with_a_short_timer(folder_env, monkeypatch):
    s = folder_env
    s.set_config(fa.CONFIG_KEY, True)
    s.upsert_tracks_batch([
        {"id": f"n{i}", "path": f"/r/Games/Turrican {i % 7}/{i}.mod", "title": f"n{i}",
         "artist": "Someone Else", "album": "", "format": "ProTracker", "genre": []}
        for i in range(600)])
    monkeypatch.setattr(s, "list_scan_dirs", lambda: [{"path": "/r"}])
    monkeypatch.setattr(fa, "_YIELD_BUDGET_S", 0.0)          # yield at every check
    monkeypatch.setattr(sm, "modland_name_keys", lambda: (frozenset(), frozenset()))
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def rec(d=0, *a, **kw):
        delays.append(d)
        return await real_sleep(0)
    monkeypatch.setattr(fa.asyncio, "sleep", rec)
    res = await fa.apply_folder_albums(force=True)
    assert res["updated"] >= 600
    n = await fa.revert_album_source(fa.SOURCE_FOLDER)
    assert n >= 600
    assert delays and set(delays) == {fa._YIELD_SEC}


# ── r4-enr-8: the Demozoo memo is frozen out of the GC once it grew ──────────

def test_a_grown_demozoo_memo_is_frozen_once(tmp_path, monkeypatch):
    from soniqboom.core import demozoo
    db = tmp_path / "demozoo.sqlite"
    db.write_bytes(b"x")
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    monkeypatch.setattr(demozoo, "_memo_sig", None)
    monkeypatch.setattr(demozoo, "_memo_frozen", (0, 0))
    froze: list[str] = []
    monkeypatch.setattr("soniqboom.core.store.freeze_long_lived_heap", froze.append)
    memo = demozoo._memos()[1]
    for i in range(demozoo._MEMO_FREEZE_GROWTH - 1):
        memo[f"a{i}"] = [(), None]
    assert demozoo._freeze_grown_memo() is False              # not grown enough
    memo["last"] = [(), None]
    assert demozoo._freeze_grown_memo() is True and len(froze) == 1
    assert demozoo._freeze_grown_memo() is False              # a warm apply: no pause
    for i in range(10):
        memo[f"b{i}"] = [(), None]
    assert demozoo._freeze_grown_memo() is False
    # Clear-at-cap, then a refill to the same size: new objects, frozen again.
    monkeypatch.setattr(demozoo, "_MEMO_CAP", len(memo))
    demozoo._memo_put(memo, "c0", [(), None])
    assert len(memo) == 1
    for i in range(1, demozoo._MEMO_FREEZE_GROWTH):
        memo[f"c{i}"] = [(), None]
    assert demozoo._freeze_grown_memo() is True and len(froze) == 2


async def test_the_demozoo_apply_freezes_its_memo(tmp_path, monkeypatch):
    from soniqboom.core import demozoo
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    db = tmp_path / "demozoo.sqlite"
    db.write_bytes(b"x")
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    monkeypatch.setattr(demozoo, "_memo_sig", None)
    monkeypatch.setattr(demozoo, "_memo_frozen", (0, 0))
    monkeypatch.setattr(demozoo, "_last_apply_sig", None)
    monkeypatch.setitem(demozoo._status, "applying", False)
    froze: list[str] = []
    monkeypatch.setattr("soniqboom.core.store.freeze_long_lived_heap", froze.append)

    def collect(tracks=None, matched_ids=None):
        memo = demozoo._memos()[1]
        for i in range(demozoo._MEMO_FREEZE_GROWTH + 1):
            memo[f"h{i}"] = [(), None]
        return 0, []
    monkeypatch.setattr(demozoo, "collect_updates", collect)
    res = await demozoo.apply_to_library(force=True)
    assert res.get("error") is None and froze == ["the Demozoo apply memo"]


# ── r4-enr-11: container folders are generic ─────────────────────────────────

@pytest.mark.parametrize("path,root,expect", [
    ("/r/a/distance/mp3", "/r/a", "distance / mp3"),
    ("/r/Isao Tomita - PLANETS Ultimate Edition/Multi", "/r",
     "Isao Tomita - PLANETS Ultimate Edition / Multi"),
    ("/r/maf/MOD - 4 channels/co-op", "/r/maf", "MOD - 4 channels / co-op"),
    ("/r/demos/groups/fairlight/ms-dos", "/r/demos", "fairlight / ms-dos"),
    ("/r/x/Soundtrack/AIFF", "/r/x", "Soundtrack / AIFF"),
    ("/r/x/Gold of the Aztecs", "/r/x", "Gold of the Aztecs"),   # a real album
])
def test_container_folders_are_qualified_by_their_parent(path, root, expect):
    from soniqboom.core.subsonic_index import _folder_display_name
    assert _folder_display_name(path, root) == expect


@pytest.mark.parametrize("name", ["mp3", "FLAC", "ogg", "AIFF", "Multi", "hi-res",
                                  "ms-dos", "co-op", "2ch", "wv"])
def test_container_words_are_generic(name):
    assert fa._generic(name)


# ── r4-enr-10: the default tune of a multi-tune file ─────────────────────────

def _psid(path: Path, songs: int, start: int) -> Path:
    h = bytearray(0x7C)
    h[0:4] = b"PSID"
    struct.pack_into(">HHHHHHH", h, 4, 2, 0x7C, 0, 0x1000, 0x1003, songs, start)
    h[0x16:0x16 + 5] = b"Tunes"
    h[0x36:0x36 + 3] = b"Rob"
    h[0x56:0x56 + 4] = b"1987"
    path.write_bytes(bytes(h) + b"\x00" * 32)
    return path


class _NoHvsc:
    def is_configured(self):
        return False


class _Hvsc:
    def __init__(self, lengths):
        self.lengths = lengths

    def is_configured(self):
        return True

    def reload(self):
        pass

    def lookup_durations_by_md5(self, md5):
        return list(self.lengths)

    def lookup_stil(self, path):
        return None

    def stil_key_for(self, path):
        return None

    def lookup_stil_by_relpath(self, key):
        return None


@pytest.mark.parametrize("songs,start,want", [
    (5, 3, 2), (5, 1, None), (5, 0, None), (5, 6, None), (1, 1, None), (2, 2, 1)])
def test_sid_records_its_default_tune(tmp_path, monkeypatch, songs, start, want):
    monkeypatch.setattr("soniqboom.core.hvsc.get_hvsc", lambda: _NoHvsc())
    d = metadata.extract(_psid(tmp_path / "t.sid", songs, start), "t").model_dump()
    assert d["start_subsong"] == want


def test_sid_wire_contract_matches_the_renderer(tmp_path, monkeypatch):
    from soniqboom.api.stream import sid_wire_tune
    from soniqboom.core import subsonic_index as sx
    monkeypatch.setattr("soniqboom.core.hvsc.get_hvsc",
                        lambda: _Hvsc([100.0, 200.0, 300.0, 400.0, 500.0]))
    d = metadata.extract(_psid(tmp_path / "t.sid", 5, 3), "t").model_dump()
    assert d["start_subsong"] == 2 and d["subsongs"] == 5
    assert d["duration"] == 300.0                             # the bare id plays tune 3
    assert d["hvsc_lengths"][0] == 100.0
    for w in range(5):
        tune = sid_wire_tune(w, 3, 5)
        assert sx.wire_tune(d, w) + 1 == tune
        assert sx.wire_length(d, w) == d["hvsc_lengths"][tune - 1]
    assert sx.default_duration(d) == 300.0


def test_sndh_records_its_default_tune(tmp_path, monkeypatch):
    out = ("tag field TITL Funfares\ntag field ## 5\ntag field !# 3\n"
           "tag field TIME 1 60\ntag field TIME 3 95\n")

    class R:
        stdout = out
    monkeypatch.setattr(metadata, "_find_atari_binary", lambda *_a: "/bin/psgplay")
    monkeypatch.setattr(metadata.forksafe, "run", lambda *a, **kw: R())
    p = tmp_path / "t.sndh"
    p.write_bytes(b"SNDH")
    d = metadata._extract_sndh(p, "t")
    assert (d["subsongs"], d["start_subsong"], d["duration"]) == (5, 2, 95.0)
    R.stdout = out.replace("!# 3", "!# 1")
    assert "start_subsong" not in metadata._extract_sndh(p, "t")


async def test_hvsc_apply_keeps_the_default_tunes_duration(monkeypatch):
    from soniqboom.core import hvsc_apply
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr("soniqboom.core.hvsc.get_hvsc",
                        lambda: _Hvsc([100.0, 200.0, 300.0]))
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)

    async def no_purge(ids, keep_duration=None):
        return 0
    monkeypatch.setattr("soniqboom.core.conversion_cache.purge_sid_entries_for", no_purge)
    s.upsert_tracks_batch([
        {"id": "a", "path": "/m/a.sid", "title": "a", "format": "SID", "duration": 180.0,
         "sid_md5": "a" * 32, "subsongs": 3, "start_subsong": 1},
        {"id": "b", "path": "/m/b.sid", "title": "b", "format": "SID", "duration": 180.0,
         "sid_md5": "b" * 32, "subsongs": 3}])
    await hvsc_apply.apply_hvsc_to_library()
    assert s.get_track("a")["duration"] == 200.0
    assert s.get_track("b")["duration"] == 100.0
    # Idempotent: a second apply changes nothing.
    seq = s._mutation_seq
    res = await hvsc_apply.apply_hvsc_to_library()
    assert res["updated"] == 0 and s._mutation_seq == seq


def test_a_default_tune_change_rekeys_the_subsonic_catalogue():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a.sid", "title": "a", "format": "SID",
                    "subsongs": 3})
    cat = s._catalog_seq
    s.update_track_fields("a", {"start_subsong": 1})
    assert s._catalog_seq == cat + 1
    s.update_track_fields("a", {"subsongs": 4})
    assert s._catalog_seq == cat + 2


def test_repair_writes_a_newly_read_default_tune():
    old = {"id": "a", "title": "T", "duration": 100.0, "start_subsong": None}
    new = {"id": "a", "title": "T", "duration": 300.0, "start_subsong": 2,
           "hvsc_lengths": [100.0, 200.0, 300.0]}
    assert repair._changed_fields(old, new) == {"start_subsong": 2, "duration": 300.0}
    # Without the per-tune lengths the stored duration is left alone.
    assert repair._changed_fields(old, {**new, "hvsc_lengths": None}) == {"start_subsong": 2}
    # Unchanged → nothing.
    assert repair._changed_fields({**old, "start_subsong": 2}, new) == {}


# ── r4-enr-1: the index generation ───────────────────────────────────────────

def test_index_generation_moves_with_every_index_write():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a.mod", "title": "a", "format": "MOD",
                    "genre": []})
    g = s.index_generation()
    s.update_track_fields("a", {"title": "b"})
    assert s.index_generation() != g
    g = s.index_generation()
    s.update_track_fields("a", {"duration": 12.0})             # duration-only
    assert s.index_generation() != g
    g = s.index_generation()
    s.record_play("a")                                        # leaves _unplayed_ids
    assert s.index_generation() != g
    g = s.index_generation()
    s.update_track_fields("a", {"cover_art": "/api/art/a"})    # feeds no index
    s.set_rating("a", 5)
    assert s.index_generation() == g


def test_seq_exempt_fields_feed_no_index():
    # Otherwise a write to one would change an index without moving
    # ``index_generation`` and a rebuild could swap in stale indexes.
    assert store_mod._SEQ_EXEMPT_FIELDS.isdisjoint(store_mod._INDEXED_FIELDS)


# ── r4-enr-9: the deep-page memo never pins a replaced list ──────────────────

def test_primary_order_memo_drops_replaced_lists(monkeypatch):
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 5)
    s = TrackStore()
    s.upsert_tracks_batch([{"id": f"t{i:03d}", "path": f"/m/{i}.mod", "title": f"t{i:03d}",
                            "artist": f"a{i % 5}", "format": "MOD", "genre": [],
                            "added_at": 1} for i in range(50)])
    kw = dict(filter_duplicates=True, sort_order="asc")
    s._paginate_all(5, 10, sort_by="title", **kw)
    s._paginate_all(5, 10, sort_by="artist", **kw)
    assert set(s._primary_order_memo) == {"_sorted_title", "_sorted_artist"}
    s.update_track_fields("t001", {"title": "zzz"})           # both keys go stale
    s._paginate_all(5, 10, sort_by="title", **kw)
    assert set(s._primary_order_memo) == {"_sorted_title"}
    # A batch exit that replaces a list drops its entry right away.
    s.enter_batch_mode()
    s.upsert_tracks_batch([{"id": "new", "path": "/m/new.mod", "title": "aaa",
                            "format": "MOD", "genre": [], "added_at": 1}])
    s._paginate_all(5, 10, sort_by="title", **kw)
    s.exit_batch_mode()
    assert "_sorted_title" not in s._primary_order_memo
    s._paginate_all(5, 10, sort_by="title", **kw)
    s._rebuild_sorted_indexes()
    assert s._primary_order_memo == {}


# ── duplicate recompute: batched write-back of the changed annotations ──────

async def test_duplicate_recompute_writes_only_changes_in_batches(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    refreshed: list[list[str]] = []

    async def _refresh(ids):
        refreshed.append(sorted(ids))
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    s.upsert_tracks_batch(
        [{"id": "a", "path": "/a.mp3", "title": "Song", "artist": "X", "duration": 100.0,
          "format": "MP3", "bitrate": 128, "genre": []},
         {"id": "b", "path": "/b.flac", "title": "Song", "artist": "X", "duration": 100.0,
          "format": "FLAC", "bitrate": 900, "genre": []}]
        + [{"id": f"u{i}", "path": f"/u{i}.mp3", "title": f"Other {i}", "artist": "Y",
            "duration": 50.0 + i, "format": "MP3", "genre": []} for i in range(30)])
    ops: list[str] = []
    s._aof_append = lambda op, **kw: ops.append(op)
    assert await repair.recompute_duplicate_groups_now() == 32
    assert s.get_track("b")["is_duplicate_primary"] is True
    assert s.get_track("a")["is_duplicate_primary"] is False
    assert "update_track_fields" not in ops and ops.count("update_track_fields_batch") >= 1
    first = refreshed[-1]
    assert {"a", "b"} <= set(first)
    # Nothing changed on a second pass: no write, no browse refresh.
    ops.clear()
    n_refresh = len(refreshed)
    assert await repair.recompute_duplicate_groups_now() == 32
    assert ops == [] and len(refreshed) == n_refresh


def test_the_shared_writer_skips_no_op_fields():
    s = TrackStore()
    s.upsert_track({"id": "x", "path": "/a.mod", "title": "t", "album": "A",
                    "format": "MOD", "genre": []})
    assert fa._guard_item(s, "x", {"album": "A"}, None) == (None, False)
    assert fa._guard_item(s, "x", {"album": "B", "title": "t"}, None) == ({"album": "B"}, True)


def test_a_duration_backfill_keeps_the_other_deep_page_orders(monkeypatch):
    monkeypatch.setattr(store_mod, "_PRIMARY_ORDER_MIN_OFFSET", 5)
    s = TrackStore()
    s.upsert_tracks_batch([{"id": f"t{i:03d}", "path": f"/m/{i}.mod", "title": f"t{i:03d}",
                            "format": "MOD", "genre": [], "duration": 10.0 + i,
                            "added_at": 1} for i in range(50)])
    kw = dict(filter_duplicates=True, sort_order="asc")
    s._paginate_all(5, 10, sort_by="title", **kw)
    s._paginate_all(5, 10, sort_by="duration", **kw)
    title_memo = s._primary_order_memo["_sorted_title"]
    seq = s._mutation_seq
    s.update_track_fields("t000", {"duration": 999.0})         # a first-play backfill
    assert s._mutation_seq == seq
    page = [d["id"] for d in s._paginate_all(5, 10, sort_by="duration", **kw)]
    assert page == [f"t{i:03d}" for i in range(11, 16)]
    s._paginate_all(5, 10, sort_by="title", **kw)
    assert s._primary_order_memo["_sorted_title"] is title_memo   # not rebuilt
