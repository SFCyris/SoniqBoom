# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-1 review fixes around re-extraction and rescans:

* the header-GAME backfill (SPC/NSF/NSFe/GBS/VGM/VGZ tracks indexed before the
  extractor read the game) — candidates, the write, the one-time run;
* repair relabels a derived ``album_source`` when the file's real tag carries
  the SAME album string;
* a Modland-filled artist survives a rescan of the unchanged file;
* the SPC extended ID666 (xid6) full game name."""
from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import metadata
from soniqboom.core import repair
from soniqboom.core.store import TrackStore, _carry_enrichment


def _pad(s: bytes, n: int) -> bytes:
    return s[:n].ljust(n, b"\x00")


def _nsf(path: Path, name: bytes = b"AXEL F", artist: bytes = b"Nullsleep") -> Path:
    b = bytearray(0x80)
    b[0:5] = b"NESM\x1a"
    b[0x0E:0x2E] = _pad(name, 32)
    b[0x2E:0x4E] = _pad(artist, 32)
    path.write_bytes(bytes(b) + b"\x00" * 64)
    return path


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    calls = []
    async def _refresh(ids):
        calls.append(1)
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    s.invalidations = calls
    monkeypatch.setattr(repair, "_broadcast_progress", _no_broadcast)
    repair._progress = repair.RepairProgress()
    return s


async def _no_broadcast(_p):
    return None


async def _wait_repair():
    for _ in range(300):
        # the task itself done (its finish — cache refresh, then the
        # done-callbacks that settle a one-time backfill — included)
        if not repair.is_running() and (repair._task is None or repair._task.done()):
            await asyncio.sleep(0)              # let the done-callback run
            return
        await asyncio.sleep(0.01)
    raise AssertionError("repair did not finish")


def _row(tid, path, **kw):
    t = {"id": tid, "path": str(path), "title": "AXEL F", "artist": "Nullsleep",
         "album": "", "album_source": None, "format": "NSF", "genre": ["Chiptune"]}
    t.update(kw)
    return t


# ── candidates ───────────────────────────────────────────────────────────────

def test_backfill_candidates(store, tmp_path):
    store.upsert_tracks_batch([
        _row("empty", tmp_path / "a.nsf"),
        _row("folder", tmp_path / "b.spc", album="Some Dir", album_source="folder", format="SPC"),
        _row("tag", tmp_path / "c.nsfe", album="Game", album_source="tag", format="NSFe"),
        # a typed album whose header name is recorded (read once already)
        _row("mine", tmp_path / "d.vgz", album="Mine", user_edited=["album"], format="VGZ",
             game_by_tag=None),
        _row("zip", f"{tmp_path}/pack.zip::e.gbs", format="GBS"),
        _row("mod", tmp_path / "f.mod", format="ProTracker"),
        _row("remote", "ftp://host/share:/g.vgm", format="VGM"),
    ])
    ids = {t["id"] for t in repair.find_album_backfill_candidates()}
    assert ids == {"empty", "folder", "zip", "remote"}
    local = {t["id"] for t in repair.find_album_backfill_candidates(include_remote=False)}
    assert local == {"empty", "folder", "zip"}


# ── the write ────────────────────────────────────────────────────────────────

async def test_backfill_fills_the_header_game_and_keeps_derived_albums(store, tmp_path):
    nsf = _nsf(tmp_path / "axel.nsf")
    nameless = _nsf(tmp_path / "nameless.nsf", name=b"<?>")
    store.upsert_tracks_batch([
        _row("n1", nsf),
        _row("n2", nameless, title="<?>", album="Folder Game", album_source="folder"),
        _row("n3", nsf, album="Hand Made", user_edited=["album"]),
    ])
    assert await repair.start_repair(repair.find_album_backfill_candidates())
    await _wait_repair()
    n1 = store.get_track("n1")
    assert (n1["album"], n1["album_source"]) == ("AXEL F", "tag")
    n2 = store.get_track("n2")
    assert (n2["album"], n2["album_source"]) == ("Folder Game", "folder")   # header has none
    n3 = store.get_track("n3")
    assert (n3["album"], n3.get("album_source")) == ("Hand Made", None)     # user edit wins
    assert store.invalidations == [1]                  # album caches dropped once
    assert "n1" in store.filter_track_ids(album="AXEL F")


async def test_backfill_on_the_real_nsf_fixture_when_present(store, repo_root):
    nsf = repo_root / "internal/testdata/gme/8bp028-b1-nullsleep-axel_f.nsf"
    if not nsf.is_file():
        pytest.skip("private test fixture not present")
    store.upsert_tracks_batch([_row("real", nsf, title="8bp028-b1-nullsleep-axel_f")])
    assert await repair.start_repair(repair.find_album_backfill_candidates())
    await _wait_repair()
    assert (store.get_track("real")["album"], store.get_track("real")["album_source"]) == \
        ("AXEL F", "tag")


async def test_album_only_change_skips_the_duplicate_recompute(store, tmp_path, monkeypatch):
    calls = []

    async def fake_recompute():
        calls.append(1)
        return 0
    monkeypatch.setattr(repair, "recompute_duplicate_groups_now", fake_recompute)
    store.upsert_tracks_batch([_row("n1", _nsf(tmp_path / "axel.nsf"))])
    assert await repair.start_repair(repair.find_album_backfill_candidates())
    await _wait_repair()
    assert store.get_track("n1")["album"] == "AXEL F"
    assert calls == []            # title/artist unchanged → group keys unchanged


async def test_one_time_backfill_runs_once_and_skips_remote(store, tmp_path):
    store.upsert_tracks_batch([_row("n1", _nsf(tmp_path / "axel.nsf")),
                               _row("r", "ftp://host/share:/x.nsf")])
    assert await repair.run_album_backfill_once() is True
    await _wait_repair()
    await asyncio.sleep(0)                   # the done-callback
    assert store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY) is True
    assert repair._progress.total == 1       # the remote row was not fetched
    assert store.get_track("n1")["album"] == "AXEL F"
    assert await repair.run_album_backfill_once() is False


async def test_one_time_backfill_marks_done_when_nothing_to_do(store):
    assert await repair.run_album_backfill_once() is False
    assert store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY) is True


def test_duplicate_recompute_runs_the_grouping_off_the_loop(store, monkeypatch):
    import threading
    from soniqboom.core import duplicates
    seen = []
    real = duplicates.compute_duplicate_groups
    monkeypatch.setattr(duplicates, "compute_duplicate_groups",
                        lambda tracks: seen.append(threading.current_thread().name) or real(tracks))
    store.upsert_tracks_batch([
        {"id": "a", "path": "/a.mp3", "title": "Song", "artist": "X", "duration": 100.0,
         "format": "MP3", "bitrate": 128},
        {"id": "b", "path": "/b.flac", "title": "Song", "artist": "X", "duration": 100.0,
         "format": "FLAC", "bitrate": 900},
    ])
    n = asyncio.run(repair.recompute_duplicate_groups_now())
    assert n == 2
    assert seen and seen[0] != threading.main_thread().name
    assert store.get_track("b")["is_duplicate_primary"] is True
    assert store.get_track("a")["is_duplicate_primary"] is False


# ── repair provenance when the value is unchanged ────────────────────────────

@pytest.mark.parametrize("old,new,expect", [
    ({"album": "Turrican II", "album_source": "folder"},
     {"album": "Turrican II", "album_source": "tag"}, {"album_source": "tag"}),
    ({"album": "Turrican II", "album_source": "modland"},
     {"album": "Turrican II"}, {"album_source": None}),
    # an unlabelled album the header names (an older version's PSF game=) is
    # labelled the file's own, so the game follows it
    ({"album": "Turrican II", "album_source": None},
     {"album": "Turrican II", "album_source": "tag"}, {"album_source": "tag"}),
    ({"album": "Turrican II", "album_source": None},
     {"album": "Turrican II", "album_source": None}, {}),
    ({"album": "Turrican II", "album_source": "tag"},
     {"album": "Turrican II"}, {}),
    ({"album": "Turrican II", "album_source": "folder", "user_edited": ["album"]},
     {"album": "Turrican II", "album_source": "tag"}, {}),
])
def test_repair_relabels_a_derived_source_when_the_tag_matches(old, new, expect):
    assert repair._changed_fields(old, new) == expect


# ── a Modland-filled artist survives a rescan ────────────────────────────────

_SCENE = "Protracker/Dalezy/coop-Jester/tune.mod"


@pytest.mark.parametrize("old_kw,new_kw,expect_artist", [
    ({}, {}, "Dalezy & Jester"),                                    # carried
    ({}, {"file_md5": "f" * 32}, ""),                               # the file changed
    ({}, {"artist": "Real Tag"}, "Real Tag"),                       # a tag wins
    ({"artist": "Someone Else"}, {}, ""),                           # not the credit
])
def test_modland_artist_carried_across_rescans(old_kw, new_kw, expect_artist):
    old = {"id": "t", "artist": "Dalezy & Jester", "file_md5": "a" * 32,
           "scene_path": _SCENE, "scene_group": "Fairlight", "year": 1992,
           "year_source": "demozoo", "year_file": 1991}
    old.update(old_kw)
    new = {"id": "t", "artist": "", "file_md5": "a" * 32, "year": 1991}
    new.update(new_kw)
    _carry_enrichment(old, new)
    assert new["artist"] == expect_artist
    if expect_artist == "Dalezy & Jester":
        # Restored before the identity test, so the composer-keyed Demozoo
        # enrichment survives with it.
        assert new["scene_group"] == "Fairlight"
        assert (new["year"], new["year_source"]) == (1992, "demozoo")
        again = dict(new)
        _carry_enrichment(old, again)                 # idempotent (AOF replay)
        assert again == new


# ── SPC extended ID666 ───────────────────────────────────────────────────────

def _spc(path: Path, game: bytes, xid6: bytes | None) -> Path:
    b = bytearray(0x10200)
    b[0:33] = b"SNES-SPC700 Sound File Data v0.30"
    b[0x21:0x23] = b"\x1a\x1a"
    b[0x23] = 26
    b[0x2E:0x4E] = _pad(b"Title Screen", 32)
    b[0x4E:0x6E] = _pad(game, 32)
    path.write_bytes(bytes(b) + (xid6 or b""))
    return path


def _xid6(*subs: tuple[int, int, bytes]) -> bytes:
    body = b""
    for sid, typ, data in subs:
        if typ == 0:
            body += struct.pack("<BBH", sid, 0, len(data))
            continue
        body += struct.pack("<BBH", sid, typ, len(data)) + data
        body += b"\x00" * ((-len(data)) % 4)
    return b"xid6" + struct.pack("<I", len(body)) + body


_FULL = b"Street Fighter 2 - The World War"          # exactly 32 bytes, no NUL


def test_spc_full_game_field_prefers_the_extended_tag(tmp_path):
    x = _xid6((0x01, 1, b"Title Screen\x00"), (0x13, 0, b""),
              (0x02, 1, b"Street Fighter 2 - The World Warrior\x00"))
    d = metadata._extract_gme(_spc(tmp_path / "a.spc", _FULL, x), "t")
    assert d["album"] == "Street Fighter 2 - The World Warrior"
    assert d["album_source"] == "tag"


@pytest.mark.parametrize("xid6", [
    None,                                                    # no extended tag
    b"xid6" + struct.pack("<I", 400) + b"\x02\x01\xff\x7f",  # length past the end
    b"xid6" + b"\x10\x00",                                   # truncated header
    b"xid5" + b"\x00" * 16,                                  # wrong magic
])
def test_spc_without_a_usable_extended_tag_keeps_the_32_char_name(tmp_path, xid6):
    d = metadata._extract_gme(_spc(tmp_path / "b.spc", _FULL, xid6), "t")
    assert d["album"] == _FULL.decode()


def test_spc_short_game_never_reads_the_extended_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "_spc_xid6_game",
                        lambda p: (_ for _ in ()).throw(AssertionError("read xid6")))
    d = metadata._extract_gme(_spc(tmp_path / "c.spc", b"Super Metroid", None), "t")
    assert d["album"] == "Super Metroid"
