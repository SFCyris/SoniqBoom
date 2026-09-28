# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The distinct ``game`` field.

* a retro track's game follows its game album (``album_source`` a game source)
  on every store write path — upsert, rescan carry, field updates, withdrawals
  — and on load, with ``game_source`` naming the same source;
* a file's own GAME tag (``game_source`` None) and a game the user typed
  (``user_edited``) are never replaced; an album the user typed on a retro
  track is its game (``user-album``), and an AOF replay lands on the same game;
* ``game:`` and plain search read it; its index follows every write;
* modern files: the GAME tag is read (ID3 TXXX:GAME, Vorbis GAME, MP4
  ``----:GAME``, trimmed) and written by the tag editor; "Read game names"
  re-reads it and writes only the game — a network-share MP3 / FLAC / MP4
  only as far as its tags (``tag_window``), offline shares deferred;
* a rescan of a track stored before the field existed is no change;
* Opus files keep their tags (a missing sample-rate field used to drop them)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from soniqboom.core import repair
from soniqboom.core.store import TrackStore, game_follow


def _t(tid, **kw):
    t = {"id": tid, "path": f"/m/{tid}.mod", "title": tid, "artist": "", "album": "",
         "format": "ProTracker", "genre": [], "file_md5": "a" * 32, "file_size": 10}
    t.update(kw)
    return t


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    return s


@pytest.mark.parametrize("src", ["tag", "modland", "modland-filename", "songdb", "folder"])
def test_the_game_follows_a_game_album(store, src):
    store.upsert_tracks_batch([_t("a", album="Gold of the Aztecs", album_source=src)])
    a = store.get_track("a")
    assert (a["game"], a["game_source"]) == ("Gold of the Aztecs", src)
    store.update_track_fields("a", {"album": "Turrican", "album_source": src})
    assert store.get_track("a")["game"] == "Turrican"
    store.update_track_fields("a", {"album": "", "album_source": None})     # withdrawn
    a = store.get_track("a")
    assert (a["game"], a["game_source"]) == ("", None)


def test_a_plain_album_is_no_game(store):
    store.upsert_tracks_batch([_t("a", album="Best of 1990"),
                               _t("mp3", album="Disney's Greatest", format="MP3")])
    assert store.get_track("a").get("game", "") == ""
    assert store.get_track("mp3").get("game", "") == ""


def test_the_files_own_tag_and_the_users_game_are_kept(store):
    store.upsert_tracks_batch([
        _t("remix", format="FLAC", album="Remixes", game="Uridium 2"),
        _t("typed", album="Stage 1", album_source="modland"),
    ])
    store.update_track_fields("remix", {"album": "Other", "album_source": "folder"})
    assert (store.get_track("remix")["game"], store.get_track("remix").get("game_source")) == \
        ("Uridium 2", None)
    store.update_track_fields("typed", {"game": "My Game", "game_source": None,
                                        "user_edited": ["game"]})
    store.update_track_fields("typed", {"album": "Stage 2", "album_source": "modland"})
    assert store.get_track("typed")["game"] == "My Game"
    store.update_track_fields("typed", {"album": "", "album_source": None})
    assert store.get_track("typed")["game"] == "My Game"


def test_a_game_the_user_cleared_stays_empty(store):
    store.upsert_tracks_batch([_t("a", album="Turrican", album_source="modland")])
    store.update_track_fields("a", {"game": "", "game_source": None, "user_edited": ["game"]})
    store.update_track_fields("a", {"album": "Turrican II", "album_source": "modland"})
    assert store.get_track("a")["game"] == ""
    assert store._candidate_ids(game="turrican") == set()


def test_a_users_retro_album_is_the_game(store):
    store.upsert_tracks_batch([_t("a", album="Unsorted Stuff", album_source="folder"),
                               _t("mp3", format="MP3", album="Remixes")])
    store.update_track_fields("a", {"album": "Turrican II", "album_source": None,
                                    "user_edited": ["album"]})
    a = store.get_track("a")
    assert (a["album"], a["game"], a["game_source"]) == ("Turrican II", "Turrican II",
                                                         "user-album")
    assert store._candidate_ids(game="turrican") == {"a"}
    # the folder name stays that source's name — an alias
    assert store._candidate_ids(game="unsorted") == {"a"}
    assert store.get_track("a")["game_aliases"] == ["Unsorted Stuff"]
    # the folder option going off (a withdrawal skipping user albums) changes nothing
    store.update_track_fields("a", {"album": "Turrican III"})
    assert store.get_track("a")["game"] == "Turrican III"
    store.update_track_fields("a", {"album": ""})              # the user cleared it
    # the game stays distinct: the best name a source gives
    assert (store.get_track("a")["game"], store.get_track("a")["game_source"]) == \
        ("Unsorted Stuff", "folder")
    # a modern file's album is never its game
    store.update_track_fields("mp3", {"album": "Mine", "user_edited": ["album"]})
    assert store.get_track("mp3").get("game", "") == ""


def test_aof_replay_lands_on_the_same_game(tmp_path):
    """The live store and a store rebuilt from snapshot + AOF agree on every
    game — the snapshot predates the field (load fills it in memory only)."""
    from soniqboom.core import persistence
    snap = {"a": _t("a", album="Carrier Command", album_source="songdb")}
    live = TrackStore()
    live.bulk_load({k: dict(v) for k, v in snap.items()}, {}, {}, {}, {}, [], {}, {}, {})
    records = []
    live._aof = lambda op, **kw: records.append((op, kw))
    live.update_track_fields("a", {"album": "Carrier Command II", "album_source": None,
                                   "user_edited": ["album"]})
    replayed = {k: dict(v) for k, v in snap.items()}
    for op, kw in records:
        assert op == "update_track_fields"
        replayed[kw["id"]].update(kw["data"])
    rebuilt = TrackStore()
    rebuilt.bulk_load(replayed, {}, {}, {}, {}, [], {}, {}, {})
    rebuilt.rebuild_indexes()
    for s in (live, rebuilt):
        a = s.get_track("a")
        assert (a["game"], a["game_source"]) == ("Carrier Command II", "user-album")
    assert rebuilt._candidate_ids(game="carrier") == {"a"}
    assert persistence  # the replay above mirrors persistence.replay_aof's update


def test_a_rescan_of_a_track_stored_before_the_field_is_no_change(store):
    from soniqboom.core.scanner import _same_track_content
    old = _t("m", path="/m/x.mp3", format="MP3")          # no game key at all
    fresh = dict(old, game="", game_source=None)           # the extractor's defaults
    assert _same_track_content(old, fresh)
    assert not _same_track_content(old, dict(fresh, game="Uridium"))
    assert not _same_track_content(dict(old, game="Uridium", game_source=None), fresh)


def test_a_rescan_keeps_the_game_without_churn(store):
    from soniqboom.core.scanner import _same_track_content
    store.upsert_tracks_batch([_t("a", album="Turrican", album_source="modland",
                                  scene_path="X/Y/Turrican/a.mod")])
    old = dict(store.get_track("a"))
    fresh = _t("a")                                   # the file carries no album / game
    assert _same_track_content(old, dict(fresh))
    store.upsert_tracks_batch([dict(fresh)])
    a = store.get_track("a")
    assert (a["album"], a["game"], a["game_source"]) == ("Turrican", "Turrican", "modland")
    # a different file: the derived album and its game go
    store.upsert_tracks_batch([_t("a", file_md5="b" * 32)])
    assert store.get_track("a").get("game", "") == ""


def test_load_brings_games_in_step():
    s = TrackStore()
    tracks = {"a": _t("a", album="Turrican", album_source="songdb"),
              "b": _t("b", format="MP3", game="Uridium")}
    s.bulk_load(tracks, {}, {}, {}, {}, [], {}, {}, {})
    s.rebuild_indexes()
    assert (s.get_track("a")["game"], s.get_track("a")["game_source"]) == ("Turrican", "songdb")
    assert s._candidate_ids(game="turr") == {"a"}
    assert s._candidate_ids(game="uridium") == {"b"}


def test_game_index_and_plain_search_follow_writes(store):
    store.upsert_tracks_batch([_t("r", format="MP3", game="Pokémon Gold", title="Remix")])
    assert store._candidate_ids(game="pokemon") == {"r"}
    assert "r" in store._candidate_ids(query="pokemon gold")
    store.update_track_fields("r", {"game": "Zelda", "user_edited": ["game"]})
    assert store._candidate_ids(game="pokemon") == set()
    assert store._candidate_ids(game="the zelda") == {"r"}
    store.enter_batch_mode()
    store.update_track_fields_batch([("r", {"game": "Uridium"})])
    store.exit_batch_mode()
    assert store._candidate_ids(game="uridium") == {"r"}
    store.delete_track("r")
    assert store._candidate_ids(game="uridium") == set()
    assert store.verify_indexes()["index_ok"]


def test_game_follow_is_pure():
    t = {"album": "X", "album_source": "songdb"}
    assert game_follow(t) == {"game": "X", "game_source": "songdb", "game_by_songdb": "X"}
    assert t == {"album": "X", "album_source": "songdb"}
    assert game_follow({"album": "X", "album_source": "songdb", "game": "X",
                        "game_source": "songdb", "game_by_songdb": "X"}) == {}


async def test_songdb_fill_and_reset_move_the_game(store, tmp_path, monkeypatch):
    from soniqboom.core import songdb
    from soniqboom.core import folder_album as fa
    from tests.test_songdb import META, LENS, A, _tsvs
    db = tmp_path / "songdb.sqlite"
    monkeypatch.setattr(songdb, "_db_path", lambda: db)
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 1)
    monkeypatch.setattr(songdb, "_MIN_LENGTH_ROWS", 1)
    monkeypatch.setattr("soniqboom.config.PREFS_PATH", tmp_path / "prefs.json")

    async def _refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    monkeypatch.setattr(fa, "enabled", lambda: False)
    songdb._status.update(applying=False)
    songdb._last_auto_sig = None
    m, s_ = _tsvs(tmp_path, META, LENS)
    songdb.build_index(m, s_, db)
    store.upsert_tracks_batch([_t("a", file_md5=A + "0" * 20, genre=["Amiga", "Module"],
                                  format="Amiga custom")])
    await songdb.apply_to_library(force=True)
    a = store.get_track("a")
    assert (a["game"], a["game_source"]) == ("Carrier Command", "songdb")
    await songdb.reset_to_file_state()
    assert store.get_track("a")["game"] == ""


async def test_meta_edit_sets_a_users_game(store):
    from soniqboom.api.tracks import _MetaUpdate, update_meta
    store.upsert_tracks_batch([_t("a", album="Turrican", album_source="modland")])
    res = await update_meta("a", _MetaUpdate(game="Turrican II"), user=None)
    assert res["applied"]["game"] == "Turrican II" and res["applied"]["game_source"] is None
    a = store.get_track("a")
    assert "game" in a["user_edited"]
    store.update_track_fields("a", {"album": "", "album_source": None})
    assert store.get_track("a")["game"] == "Turrican II"


# ── re-extract / "Read game names" ──────────────────────────────────────────

def test_repair_diff_reads_a_game_tag_and_keeps_a_derived_game():
    old = {"game": "Turrican", "game_source": "modland", "album": "Turrican",
           "album_source": "modland"}
    assert "game" not in repair._changed_fields(old, {"game": "", "album": ""})
    modern = {"game": "", "game_source": None}
    out = repair._changed_fields(modern, {"game": "Uridium 2"})
    assert out == {"game": "Uridium 2", "game_source": None}
    tagged = {"game": "Uridium 2", "game_source": None}
    assert repair._changed_fields(tagged, {"game": ""}) == {"game": ""}


def test_game_only_reextract_writes_only_the_game(store, monkeypatch):
    store.upsert_tracks_batch([_t("m", path="/m/x.mp3", format="MP3", title="Old")])
    monkeypatch.setattr(repair, "_game_only_ids", frozenset({"m"}))
    ok, applied, _err = repair._apply_re_extract(
        "m", {"title": "New Title", "game": "Uridium 2", "artist": "X"})
    m = store.get_track("m")
    assert ok and applied and (m["title"], m["game"]) == ("Old", "Uridium 2")


async def test_game_tag_candidates(store):
    store.upsert_tracks_batch([
        _t("mp3", path="/m/a.mp3", format="MP3"),
        _t("flac", path="ftp://h/b.flac", format="FLAC"),
        _t("zipped", path="/m/c.zip::d.ogg", format="Ogg Vorbis"),
        _t("typed", path="/m/e.m4a", format="AAC", user_edited=["game"]),
        _t("mod", path="/m/f.mod", format="ProTracker"),
        _t("sid", path="/m/g.sid", format="SID"),
        _t("aiff", path="/m/h.aiff", format="AIFF"),      # ID3 chunk: TXXX:GAME
        _t("adts", path="/m/i.aac", format="AAC"),
        _t("wav", path="/m/j.wav", format="WAV"),         # no GAME tag is read from it
    ])
    got = [t["id"] for t in await repair.find_game_tag_candidates()]
    assert got == ["mp3", "zipped", "aiff", "flac"]          # path order
    assert {t["id"] for t in await repair.find_game_tag_candidates(include_remote=False)} == \
        {"mp3", "zipped", "aiff"}


async def test_game_only_ids_belong_to_one_run(store):
    """A later repair run re-extracts every field of a track the previous
    "Read game names" run limited to its game."""
    store.upsert_tracks_batch([_t("m", path="/m/x.mp3", format="MP3", title="Old")])
    assert await repair.start_repair([], game_only_ids={"m"})
    await repair._task
    assert repair._game_only_ids == frozenset({"m"})
    assert await repair.start_repair([])
    await repair._task
    assert repair._game_only_ids == frozenset()
    ok, applied, _err = repair._apply_re_extract("m", {"title": "New Title", "game": ""})
    assert ok and applied and store.get_track("m")["title"] == "New Title"


async def test_the_endpoint_skips_offline_shares_and_can_skip_network(store, monkeypatch):
    from soniqboom.api import admin
    store.upsert_tracks_batch([
        _t("local", path="/m/a.mp3", format="MP3"),
        _t("up", path="ftp://h/up:/b.flac", format="FLAC", scan_root_hash="U"),
        _t("down", path="ftp://h/down:/c.flac", format="FLAC", scan_root_hash="D"),
        _t("spc", path="ftp://h/down:/d.spc", format="SPC", scan_root_hash="D"),
    ])
    monkeypatch.setattr(store, "list_scan_dirs", lambda: [
        {"path": "ftp://h/up", "path_hash": "U", "status": "ok"},
        {"path": "ftp://h/down", "path_hash": "D", "status": "unavailable"}])
    seen = {}

    async def _start(cands, *, game_only_ids=frozenset(), kind="repair"):
        seen["ids"], seen["game_only"] = [t["id"] for t in cands], set(game_only_ids)
        seen["kind"] = kind
        return True
    monkeypatch.setattr(repair, "start_repair", _start)
    r = await admin.metadata_backfill_game_albums(None, _tok="t")
    assert (seen["ids"], seen["game_only"]) == (["local", "up"], {"local", "up"})
    assert (r["total"], r["game_tag_files"], r["deferred"]) == (2, 2, 2)
    assert seen["kind"] == "game-names"
    r = await admin.metadata_backfill_game_albums({"include_remote": False}, _tok="t")
    assert (seen["ids"], r["deferred"]) == (["local"], 0)


# ── real files: the tag round trip, Opus ─────────────────────────────────────

def _make(path, codec):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=1",
                        "-c:a", codec, "-metadata", "title=T", "-metadata", "artist=A",
                        str(path)], capture_output=True)
    if r.returncode:
        pytest.skip(f"ffmpeg can't encode {codec}")


@pytest.mark.parametrize("ext,codec", [(".mp3", "libmp3lame"), (".flac", "flac"),
                                       (".m4a", "aac"), (".opus", "libopus")])
def test_game_tag_written_and_read(tmp_path, ext, codec):
    from soniqboom.core import metadata, tagwriter
    f = tmp_path / f"t{ext}"
    _make(f, codec)
    assert tagwriter.write_tags(str(f), {"game": "Uridium 2"}) == {"game": "Uridium 2"}
    m = metadata.extract(f, "x")
    assert (m.game, m.title, m.artist) == ("Uridium 2", "T", "A")


def test_opus_keeps_its_tags(tmp_path):
    from soniqboom.core import metadata
    f = tmp_path / "t.opus"
    _make(f, "libopus")
    m = metadata.extract(f, "x")
    assert (m.title, m.artist, m.sample_rate) == ("T", "A", 48000) and m.duration > 0.9


def test_game_tags_are_trimmed_and_one_id3_frame_is_kept(tmp_path):
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, TXXX
    from soniqboom.core import metadata, tagwriter
    fl = tmp_path / "t.flac"
    _make(fl, "flac")
    f = FLAC(str(fl))
    f["GAME"] = "  Spaced Game  "
    f.save()
    assert metadata.extract(fl, "x").game == "Spaced Game"
    mp = tmp_path / "t.mp3"
    _make(mp, "libmp3lame")
    tags = ID3(str(mp))
    tags.add(TXXX(encoding=3, desc="game", text=["Old Game"]))   # as ffmpeg writes it
    tags.save(str(mp))
    assert metadata.extract(mp, "x").game == "Old Game"
    tagwriter.write_tags(str(mp), {"game": "New Game"})
    frames = [fr for fr in ID3(str(mp)).getall("TXXX") if fr.desc.upper() == "GAME"]
    assert [(fr.desc, list(fr.text)) for fr in frames] == [("GAME", ["New Game"])]
    assert metadata.extract(mp, "x").game == "New Game"


# ── network shares: only the tag bytes ──────────────────────────────────────

class _CountingSource:
    """A FileSource stand-in over a local file that counts what is read."""

    def __init__(self, path):
        self.data = open(path, "rb").read()
        self.read = 0
        self.full = 0

    def read_partial(self, path, n, *, lane="scan"):
        d = self.data[:n]
        self.read += len(d)
        return d

    def read_at(self, path, off, n, *, lane="scan"):
        d = self.data[off:off + n]
        self.read += len(d)
        return d

    def read_file(self, path, *, lane="scan"):
        self.full += 1
        self.read += len(self.data)
        return self.data


def _tagged_file(tmp_path, ext, codec, *, faststart=False, picture_first=False):
    """A 60 s file with a GAME tag and a 1 MB (incompressible) cover."""
    import os
    from mutagen import File as MFile
    from mutagen.flac import Picture
    from mutagen.id3 import APIC
    from mutagen.mp4 import MP4Cover
    from soniqboom.core import tagwriter
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    f = tmp_path / f"t{ext}"
    args = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=60",
            "-c:a", codec, "-metadata", "title=T", "-metadata", "artist=A"]
    if faststart:
        args += ["-movflags", "+faststart"]
    if subprocess.run(args + [str(f)], capture_output=True).returncode:
        pytest.skip(f"ffmpeg can't encode {codec}")
    img = b"\xff\xd8\xff\xe0" + os.urandom(1_000_000)
    m = MFile(str(f))
    if ext == ".flac":
        pic = Picture()
        pic.data, pic.type, pic.mime = img, 3, "image/jpeg"
        m.add_picture(pic)
    elif ext == ".mp3":
        m.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="c", data=img))
    else:
        m["covr"] = [MP4Cover(img, imageformat=MP4Cover.FORMAT_JPEG)]
    m.save()
    tagwriter.write_tags(str(f), {"game": "Uridium 2"})
    if picture_first:                           # VORBIS_COMMENT after the cover
        m = MFile(str(f))
        vc = next(b for b in m.metadata_blocks if b.code == 4)
        m.metadata_blocks.remove(vc)
        m.metadata_blocks.insert(2, vc)
        m.save()
        assert [b.code for b in MFile(str(f)).metadata_blocks][:3] == [0, 6, 4]
    return f


@pytest.mark.parametrize("ext,codec,kw,limit", [
    (".flac", "flac", {"picture_first": True}, 100_000),     # the cover is stepped over
    (".flac", "flac", {}, 100_000),
    (".mp3", "libmp3lame", {}, 1_200_000),                   # the cover is in the tag
    (".m4a", "aac", {}, 1_400_000),                          # moov at the end
    (".m4a", "aac", {"faststart": True}, 1_400_000),
])
def test_tag_window_reads_only_the_tags(tmp_path, ext, codec, kw, limit):
    import os
    from soniqboom.core import metadata, tag_window
    f = _tagged_file(tmp_path, ext, codec, **kw)
    src = _CountingSource(f)
    win = tag_window.tag_window(src, "x" + ext, ext, os.path.getsize(f))
    assert win is not None and src.full == 0
    assert src.read < limit < os.path.getsize(f)
    got, err = repair._re_extract_remote_sync(win, "ftp://h/s:/t" + ext, "x")
    full = metadata.extract(f, "x")
    assert err is None and got["game"] == full.game == "Uridium 2"


def test_tag_window_steps_over_an_id3_tag_before_flac(tmp_path):
    """Some taggers put an ID3v2 tag in front of ``fLaC``."""
    from soniqboom.core import tag_window
    f = _tagged_file(tmp_path, ".flac", "flac")
    body = b"\0" * 70_000                                    # padding, past the front read
    size = len(body)
    ss = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    g = tmp_path / "id3.flac"
    g.write_bytes(b"ID3\x04\x00\x00" + ss + body + f.read_bytes())
    src = _CountingSource(g)
    win = tag_window.tag_window(src, "x.flac", ".flac")
    got, err = repair._re_extract_remote_sync(win, "ftp://h/s:/t.flac", "x")
    assert err is None and got["game"] == "Uridium 2" and src.read < 200_000


def test_tag_window_declines_other_formats_and_layouts():
    from soniqboom.core import tag_window

    class _Src(_CountingSource):
        def __init__(self, data):
            self.data, self.read, self.full = data, 0, 0
    assert tag_window.tag_window(_Src(b"OggS" + b"\0" * 100), "x.ogg", ".ogg") is None
    assert tag_window.tag_window(_Src(b"RIFF" + b"\0" * 100), "x.flac", ".flac") is None
    assert tag_window.tag_window(_Src(b"\0" * 4), "x.m4a", ".m4a") is None
    # an MP3 without an ID3v2 tag: its front (no TXXX frame to find)
    assert tag_window.tag_window(_Src(b"\xff\xfb" + b"\0" * 70_000), "x.mp3", ".mp3") \
        == b"\xff\xfb" + b"\0" * (64 * 1024 - 2)


async def test_remote_game_only_reads_use_the_tag_window(tmp_path, store, monkeypatch):
    import os
    from soniqboom.core import filesource
    f = _tagged_file(tmp_path, ".flac", "flac", picture_first=True)
    src = _CountingSource(f)
    monkeypatch.setattr(filesource, "get_source", lambda root: src)
    store.upsert_tracks_batch([
        _t("g", path="ftp://h/s:/a/t.flac", format="FLAC", title="T",
           file_size=os.path.getsize(f)),
        _t("r", path="ftp://h/s:/a/u.flac", format="FLAC", title="Old")])
    monkeypatch.setattr(repair, "_game_only_ids", frozenset({"g"}))
    ok, applied, err = await repair._process_remote(store.get_track("g"), None)
    assert (ok, applied, err, src.full) == (True, True, None, 0)
    assert store.get_track("g")["game"] == "Uridium 2" and src.read < 100_000
    # any other re-extract still reads the whole file
    ok, applied, err = await repair._process_remote(store.get_track("r"), None)
    assert (ok, src.full, store.get_track("r")["title"]) == (True, 1, "T")


def test_a_blank_game_is_no_game(store):
    store.upsert_tracks_batch([_t("a", album="Turrican", album_source="modland", game="   "),
                               _t("m", format="MP3", game="  ")])
    assert (store.get_track("a")["game"], store.get_track("a")["game_source"]) == \
        ("Turrican", "modland")
    assert store.get_track("m")["game"] == ""


# ── QA round 2 ───────────────────────────────────────────────────────────────

def test_a_track_stored_before_the_field_is_not_rewritten(store):
    """A re-read that finds no GAME tag leaves a track without a ``game`` key
    alone — no write, not counted as updated."""
    assert repair._changed_fields({"title": "T"}, {"title": "T", "game": ""}) == {}
    assert repair._changed_fields({"title": "T"}, {"title": "T", "game": "X"}) == \
        {"game": "X", "game_source": None}
    store._tracks["m"] = _t("m", path="/m/x.mp3", format="MP3")
    store._tracks["m"].pop("game", None)
    seq = store._mutation_seq
    ok, applied, _err = repair._apply_re_extract("m", {"title": "m", "game": ""})
    assert (ok, applied, store._mutation_seq) == (True, False, seq)


def _junk_after_tag(f, tmp_path):
    """``f`` with 20 KB of junk between its ID3v2 tag and the audio — past the
    window's 16 KB of audio."""
    from soniqboom.core.tag_window import _id3_size
    data = f.read_bytes()
    n = _id3_size(data)
    g = tmp_path / "junk.mp3"
    g.write_bytes(data[:n] + b"\0" * 20_000 + data[n:])
    return g


async def test_a_window_that_does_not_read_cleanly_falls_back_to_the_whole_file(
        tmp_path, store, monkeypatch):
    from soniqboom.core import filesource, metadata
    f = _junk_after_tag(_tagged_file(tmp_path, ".mp3", "libmp3lame"), tmp_path)
    assert metadata.extract(f, "x").game == "Uridium 2"      # a whole read finds it
    src = _CountingSource(f)
    monkeypatch.setattr(filesource, "get_source", lambda root: src)
    store.upsert_tracks_batch([_t("g", path="ftp://h/s:/a/junk.mp3", format="MP3",
                                  game="Uridium 2")])
    monkeypatch.setattr(repair, "_game_only_ids", frozenset({"g"}))
    ok, applied, err = await repair._process_remote(store.get_track("g"), None)
    assert (ok, err, src.full) == (True, None, 1)
    assert (store.get_track("g")["game"], store.get_track("g").get("game_source")) == \
        ("Uridium 2", None)


async def test_a_tag_the_scan_missed_is_found_past_junk(tmp_path, store, monkeypatch):
    """The usual case — no game stored yet: a window with no audio in reach
    (junk past the tag) is not taken as "no GAME tag"; the whole file is."""
    from soniqboom.core import filesource
    f = _junk_after_tag(_tagged_file(tmp_path, ".mp3", "libmp3lame"), tmp_path)
    src = _CountingSource(f)
    monkeypatch.setattr(filesource, "get_source", lambda root: src)
    store.upsert_tracks_batch([_t("g", path="ftp://h/s:/a/junk.mp3", format="MP3")])
    monkeypatch.setattr(repair, "_game_only_ids", frozenset({"g"}))
    ok, applied, err = await repair._process_remote(store.get_track("g"), None)
    assert (ok, applied, err, src.full) == (True, True, None, 1)
    assert store.get_track("g")["game"] == "Uridium 2"


async def test_a_lost_game_is_confirmed_by_a_whole_read(tmp_path, store, monkeypatch):
    """A window that reads cleanly but finds no GAME tag where the stored
    track has the file's own is checked against the whole file."""
    from soniqboom.core import filesource, tag_window
    f = _tagged_file(tmp_path, ".flac", "flac")
    src = _CountingSource(f)
    monkeypatch.setattr(filesource, "get_source", lambda root: src)
    real = tag_window.flac_window

    def _no_comment(source, path):                # a window without the comment block
        w = real(source, path)
        return b"fLaC" + bytes([0x80]) + w[5:8] + w[8:8 + int.from_bytes(w[5:8], "big")]
    monkeypatch.setattr(tag_window, "flac_window", _no_comment)
    store.upsert_tracks_batch([_t("g", path="ftp://h/s:/a/t.flac", format="FLAC",
                                  game="Uridium 2")])
    monkeypatch.setattr(repair, "_game_only_ids", frozenset({"g"}))
    ok, applied, err = await repair._process_remote(store.get_track("g"), None)
    assert (ok, err, src.full, store.get_track("g")["game"]) == (True, None, 1, "Uridium 2")


async def test_no_window_without_real_range_reads_or_when_a_range_read_fails(
        tmp_path, store, monkeypatch):
    from soniqboom.core import filesource, tag_window
    from soniqboom.core.filesource import FTPFileSource, SMBFileSource
    from soniqboom.core.filesource_webdav import WebDAVFileSource
    assert not tag_window.supports_ranges(WebDAVFileSource.__new__(WebDAVFileSource))
    assert tag_window.supports_ranges(FTPFileSource.__new__(FTPFileSource))
    assert tag_window.supports_ranges(SMBFileSource.__new__(SMBFileSource))
    f = _tagged_file(tmp_path, ".flac", "flac")

    class _Broken(_CountingSource):               # e.g. an FTP server without REST
        def read_at(self, path, off, n, *, lane="scan"):
            raise OSError("502 REST not implemented")

        def read_partial(self, path, n, *, lane="scan"):
            raise OSError("502 REST not implemented")
    src = _Broken(f)
    monkeypatch.setattr(filesource, "get_source", lambda root: src)
    store.upsert_tracks_batch([_t("g", path="ftp://h/s:/a/t.flac", format="FLAC")])
    monkeypatch.setattr(repair, "_game_only_ids", frozenset({"g"}))
    ok, applied, err = await repair._process_remote(store.get_track("g"), None)
    assert (ok, applied, err, src.full) == (True, True, None, 1)
    assert store.get_track("g")["game"] == "Uridium 2"


async def test_an_album_edit_answers_with_the_game_it_implies(store):
    from soniqboom.api.tracks import _MetaUpdate, update_meta
    store.upsert_tracks_batch([_t("a", album="Turrican", album_source="folder")])
    res = await update_meta("a", _MetaUpdate(album="Turrican II"), user=None)
    assert (res["applied"]["game"], res["applied"]["game_source"]) == \
        ("Turrican II", "user-album")


def test_clearing_the_game_removes_the_tag(tmp_path):
    from soniqboom.core import metadata, tagwriter
    for ext, codec in ((".mp3", "libmp3lame"), (".flac", "flac"), (".m4a", "aac")):
        f = tmp_path / f"c{ext}"
        _make(f, codec)
        tagwriter.write_tags(str(f), {"game": "Uridium 2"})
        assert metadata.extract(f, "x").game == "Uridium 2"
        assert tagwriter.write_tags(str(f), {"game": ""}) == {"game": ""}
        m = metadata.extract(f, "x")
        assert (m.game, m.title) == ("", "T")


def test_the_generic_reader_knows_the_game_key():
    """Registered when metadata is imported — in a fresh process, where no tag
    was written."""
    import sys
    r = subprocess.run([sys.executable, "-c",
                        "import soniqboom.core.metadata\n"
                        "from mutagen.easyid3 import EasyID3\n"
                        "from mutagen.easymp4 import EasyMP4Tags\n"
                        "print('game' in EasyID3.valid_keys, 'game' in EasyMP4Tags.Get)"],
                       capture_output=True, text=True)
    assert r.stdout.split() == ["True", "True"], r.stderr


def test_progress_names_its_kind():
    assert repair.RepairProgress(kind="game-names").to_dict()["kind"] == "game-names"
    assert repair.RepairProgress().to_dict()["kind"] == "repair"


# ── QA round 3 ───────────────────────────────────────────────────────────────

def _psf(path, **tags):
    """A minimal PSF v1 file (no program) with a ``[TAG]`` block."""
    body = "".join(f"{k}={v}\n" for k, v in tags.items()).encode()
    path.write_bytes(b"PSF\x01" + b"\0" * 12 + b"[TAG]" + body)
    return path


async def test_a_psf_game_stored_by_an_older_version_is_labelled_and_followed(
        tmp_path, store):
    """An older version stored a PSF ``game=`` as the album with no source;
    "Read game names" re-reads it, labels it "tag", and the game follows."""
    f = _psf(tmp_path / "a.minipsf", title="Main Theme", game="Final Fantasy VII",
             length="1:30")
    old = _t("p", path=str(f), format="PSF", title="Main Theme", album="Final Fantasy VII")
    old.pop("album_source", None)
    store.upsert_tracks_batch([old])
    assert store._candidate_ids(game="final fantasy") == set()
    cands = repair.find_album_backfill_candidates()
    assert [t["id"] for t in cands] == ["p"]
    ok, applied, err = await repair._process_local(cands[0])
    p = store.get_track("p")
    assert (ok, applied, err) == (True, True, None)
    assert (p["album_source"], p["game"], p["game_source"]) == ("tag", "Final Fantasy VII", "tag")
    assert store._candidate_ids(game="final fantasy") == {"p"}
    assert repair.find_album_backfill_candidates() == []
    # an album the user typed keeps its (absent) label
    assert "album_source" not in repair._changed_fields(
        {"album": "X", "album_source": None, "user_edited": ["album"]},
        {"album": "X", "album_source": "tag"})


async def test_an_unreadable_local_file_changes_nothing(tmp_path, store):
    import os
    f = tmp_path / "t.mp3"
    _make(f, "libmp3lame")
    store.upsert_tracks_batch([_t("m", path=str(f), format="MP3", title="Real",
                                  artist="Someone", game="Uridium")])
    os.chmod(f, 0)
    try:
        if os.access(f, os.R_OK):
            pytest.skip("running with read access to a mode-000 file (root)")
        ok, applied, err = await repair._process_local(store.get_track("m"))
    finally:
        os.chmod(f, 0o644)
    m = store.get_track("m")
    assert (ok, applied) == (False, False) and err.startswith("local-unreadable")
    assert (m["title"], m["artist"], m["game"]) == ("Real", "Someone", "Uridium")
    # final for the run: a file that stays unreadable can't keep the one-time
    # backfill pending
    assert "local-unreadable" not in repair._UNREACHABLE_REASONS


def test_an_aiff_game_is_not_reported_cleared(tmp_path):
    from soniqboom.core import tagwriter
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    f = tmp_path / "t.aiff"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=1",
                    "-write_id3v2", "1", "-metadata", "game=AiffGame", str(f)], check=True)
    with pytest.raises(ValueError, match="does not support tag editing"):
        tagwriter.write_tags(str(f), {"game": ""})


async def test_nothing_to_read_starts_no_run(store, monkeypatch):
    from soniqboom.api import admin
    called = []

    async def _start(*a, **kw):
        called.append(1)
        return True
    monkeypatch.setattr(repair, "start_repair", _start)
    r = await admin.metadata_backfill_game_albums(None, _tok="t")
    assert (r["started"], r["total"], called) == (False, 0, [])


# ── QA round 4 ───────────────────────────────────────────────────────────────

async def test_an_io_error_mid_read_changes_nothing(tmp_path, store, monkeypatch):
    """An I/O error after the file opened (a mount dropping) is an error, never
    the extractor's stub over the stored fields — also when mutagen wraps it."""
    import errno
    from mutagen import MutagenError
    from soniqboom.core import metadata
    f = tmp_path / "t.mp3"
    _make(f, "libmp3lame")
    store.upsert_tracks_batch([_t("m", path=str(f), format="MP3", title="Real",
                                  artist="Someone", game="Uridium")])

    def _eio(path, track_id):
        raise OSError(errno.EIO, "Input/output error")

    def _wrapped(path, track_id):
        try:
            raise OSError(errno.EIO, "Input/output error")
        except OSError as e:
            raise MutagenError(e) from e
    for fail in (_eio, _wrapped):
        monkeypatch.setattr(metadata, "_mp3", fail)
        ok, applied, err = await repair._process_local(store.get_track("m"))
        # EIO: a flaky read — retried (counted toward giving up)
        assert (ok, applied) == (False, False) and err.startswith("local-io")
        m = store.get_track("m")
        assert (m["title"], m["artist"], m["game"]) == ("Real", "Someone", "Uridium")
    # an unparsable file is still listed (the scan's stub), strict or not
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio at all" * 10)
    monkeypatch.undo()
    assert metadata.extract(bad, "b", strict_io=True).title == "bad"


def test_a_dreamcast_dsf_is_a_backfill_candidate_and_dsd_audio_is_not(tmp_path, store):
    f = tmp_path / "a.dsf"
    body = b"title=Opening\ngame=Shenmue\n"
    f.write_bytes(b"PSF\x12" + b"\0" * 12 + b"[TAG]" + body)
    from soniqboom.core import metadata
    m = metadata.extract(f, "x")
    assert (m.format, m.album, m.album_source) == ("DSF (Dreamcast)", "Shenmue", "tag")
    old = _t("dc", path=str(f), format="DSF (Dreamcast)", title="Opening", album="Shenmue")
    store.upsert_tracks_batch([old, _t("dsd", path=str(tmp_path / "b.dsf"), format="DSD")])
    assert [t["id"] for t in repair.find_album_backfill_candidates()] == ["dc"]


async def test_progress_events_carry_error_samples_only_at_the_end(monkeypatch):
    from soniqboom.api import library
    sent = []

    async def _b(msg):
        sent.append(msg)
    monkeypatch.setattr(library, "_broadcast", _b)
    p = repair.RepairProgress(running=True, total=2)
    p.record_error("/a.mp3", "local-error: x")
    await repair._broadcast_progress(p)
    p.running = False
    await repair._broadcast_progress(p)
    assert "error_samples" not in sent[0] and sent[1]["error_samples"][0]["path"] == "/a.mp3"


# ── QA round 5 ───────────────────────────────────────────────────────────────

async def test_a_read_that_comes_back_emptier_is_not_applied(tmp_path, store, monkeypatch):
    """An extractor that swallows an I/O error returns the file name as title
    and no tags (PSF, VGM, AIFF …): that never replaces stored tags."""
    from soniqboom.core import metadata
    f = _psf(tmp_path / "ff7_101.minipsf", title="Prelude", game="Final Fantasy VII",
             artist="Nobuo Uematsu", year="1997")
    old = _t("p", path=str(f), format="PSF", title="Prelude", album="Final Fantasy VII",
             artist="Nobuo Uematsu", year=1997)
    old.pop("album_source", None)
    store.upsert_tracks_batch([old])

    def _swallowed(path, track_id):           # what _extract_psf returns after EIO
        return {"id": track_id, "path": str(path), "format": "PSF",
                "title": path.stem, "duration": 0.0, "genre": ["Chiptune", "Game Rip"]}
    real = metadata._extract_psf
    monkeypatch.setattr(metadata, "_extract_psf", _swallowed)
    ok, applied, err = await repair._process_local(store.get_track("p"))
    p = store.get_track("p")
    assert (ok, applied, err) == (False, False, "read-incomplete")
    assert (p["title"], p["artist"], p["album"], p["year"]) == \
        ("Prelude", "Nobuo Uematsu", "Final Fantasy VII", 1997)
    assert "read-incomplete" in repair._UNREACHABLE_REASONS           # retried (capped)
    monkeypatch.setattr(metadata, "_extract_psf", real)
    ok, applied, err = await repair._process_local(store.get_track("p"))   # the real read
    assert (ok, applied, err, store.get_track("p")["album_source"]) == (True, True, None, "tag")


def test_what_counts_as_a_lost_tag():
    """Only a result shaped like the extractor's stub (the file name as title,
    no tags) that would take a stored tag away is a failed read."""
    lost = repair._read_lost_data
    cur = {"path": "/m/x.mp3", "title": "Song", "artist": "A", "game": "G", "year": 1990}
    stub = {"title": "x"}
    assert lost(cur, stub, {"game": ""}) and lost(cur, stub, {"artist": " "})
    assert lost(cur, stub, {"year": None}) and lost(cur, stub, {"title": "x"})
    assert not lost(cur, stub, {"album_source": "tag"})             # takes nothing away
    # a real read that names anything is applied, also when it empties a field
    real = {"title": "Song", "album": "Remixes"}
    assert not lost(cur, real, {"game": "", "artist": ""})
    assert not lost(cur, {"title": "x", "year": 1990}, {"artist": ""})
    # no title tag (the file name as title) but other tags: a real read
    assert not lost(dict(cur, title="x"), {"title": "x", "artist": "A"}, {"game": ""})
    # a garbled value may go (the Garbled repair's target)
    assert not lost(dict(cur, artist="\ufffd\ufffd", title="x"), stub, {"artist": ""})
    assert not lost(dict(cur, title="S\ufffdng"), stub, {"title": "x"})
    # a stored bad tracker title becomes the module's file name
    assert not lost({"path": "/m/nameless.ahx", "title": "THX"}, {"title": "nameless"},
                    {"title": "nameless"})
    # an archive member's own name
    assert lost({"path": "/m/a.zip::d/t.mod", "title": "Real"}, {"title": "t"}, {"title": "t"})
    assert lost({"path": "/m/a.zip::t.mod", "title": "Real"}, {"title": "t"}, {"title": "t"})


def test_real_reads_that_empty_a_field_are_applied(store):
    """The cases a broad "never empty a field" rule refused: a placeholder
    artist the extractor now drops, a composer Demozoo filled, a GAME tag
    removed without the file's size or time changing."""
    store.upsert_tracks_batch([
        _t("nsf", path="/m/cv.nsf", format="NSF", title="Castlevania", artist="<?>"),
        _t("mod", path="/m/tune.mod", title="Tune", composer="Tim Follin"),
        _t("mp3", path="/m/r.mp3", format="MP3", title="Remix", game="Uridium")])
    ok, applied, err = repair._apply_re_extract("nsf", {
        "title": "Castlevania", "artist": "", "album": "Castlevania", "album_source": "tag"})
    n = store.get_track("nsf")
    assert (ok, applied, n["artist"], n["game"]) == (True, True, "", "Castlevania")
    ok, applied, err = repair._apply_re_extract("mod", {"title": "Tune", "composer": ""})
    assert (ok, applied, store.get_track("mod")["composer"]) == (True, True, "")
    import soniqboom.core.repair as rp
    rp._game_only_ids = frozenset({"mp3"})
    try:
        ok, applied, err = repair._apply_re_extract("mp3", {"title": "Remix", "game": ""})
    finally:
        rp._game_only_ids = frozenset()
    assert (ok, applied, store.get_track("mp3")["game"]) == (True, True, "")


async def test_a_stale_mount_is_offline_not_final(tmp_path, store, monkeypatch):
    import errno
    from pathlib import Path
    store.upsert_tracks_batch([_t("m", path="/Volumes/Gone/x.spc", format="SPC")])

    def _stale(self, *a, **kw):
        raise OSError(errno.ENOTCONN, "Socket is not connected")
    monkeypatch.setattr(Path, "exists", _stale)
    ok, applied, err = await repair._process_local(store.get_track("m"))
    assert (ok, applied) == (False, False) and err.startswith("local-offline")
    assert "local-offline" in repair._UNREACHABLE_REASONS


def test_an_unreachable_file_is_given_up_after_a_few_runs(store):
    """Counted across restarts (an install without an enrichment index runs the
    one-time backfill only at startup); a read id leaves the count."""
    store.upsert_tracks_batch([_t("a"), _t("b"), _t("c")])
    repair._backfill_settled.clear()
    try:
        for run in range(1, repair._MAX_UNREACHABLE_RUNS):
            assert repair._settle(["a", "b"], {"a"}) == {"a"}
            assert "a" not in repair._backfill_settled and "b" in repair._backfill_settled
            repair._backfill_settled.clear()                    # a restart
        assert _counts(store) == {"a": repair._MAX_UNREACHABLE_RUNS - 1}
        assert repair._settle(["a"], {"a"}) == set()
        assert "a" in repair._backfill_settled
        repair._settle(["c"], {"c"})
        repair._settle(["c"], set())                             # read after all
        assert "c" not in _counts(store)
    finally:
        repair._backfill_settled.clear()


async def test_an_unplugged_drive_is_deferred_not_counted(tmp_path, store, monkeypatch):
    gone = tmp_path / "unplugged"
    store.upsert_scan_dir(str(gone))
    store.upsert_tracks_batch([_t("s", path=str(gone / "a.spc"), format="SPC")])
    repair._backfill_settled.clear()
    assert await repair.run_album_backfill_once() is False       # deferred, no run
    assert store.get_config(repair.ALBUM_BACKFILL_WAITING_CONFIG_KEY) == [str(gone)]
    assert not _counts(store)


async def test_io_errors_inside_archives_and_vanished_files(tmp_path, store, monkeypatch):
    import errno
    from soniqboom.core import scanner
    store.upsert_tracks_batch([_t("z", path=str(tmp_path / "a.zip") + "::t.spc", format="SPC")])

    def _stale(path, tid):
        raise OSError(errno.ESTALE, "Stale NFS file handle")
    monkeypatch.setattr(scanner, "_extract_from_zip", _stale)
    ok, applied, err = await repair._process_local(store.get_track("z"))
    assert (ok, err.split(":")[0]) == (False, "local-offline")
    assert repair._local_io_reason(FileNotFoundError(errno.ENOENT, "gone")) == "local-missing"
    assert repair._local_io_reason(PermissionError(errno.EACCES, "no")) == "local-unreadable"


def test_a_derived_album_completing_an_spc_cut_is_kept():
    old = {"album": "Street Fighter II - The World Warrior", "album_source": "folder",
           "format": "SPC", "file_md5": "m"}
    new = {"album": "Street Fighter II - The World Wa", "album_source": "tag", "file_md5": "m"}
    assert "album" not in repair._changed_fields(old, new)
    old.pop("file_md5")                           # a track stored without the checksum
    assert "album" not in repair._changed_fields(old, new)
    assert "album" in repair._changed_fields(dict(old, file_md5="x"), new)   # another file


def test_a_truncated_file_is_no_io_error(tmp_path):
    from soniqboom.core import metadata
    f = tmp_path / "t.mp3"
    _make(f, "libmp3lame")
    f.write_bytes(f.read_bytes()[:40])
    m = metadata.extract(f, "x", strict_io=True)           # the scan's stub, no raise
    assert m.title == "t"


# ── real files (internal/testdata/game — private, gitignored; skipped if absent) ──

_REAL = __import__("pathlib").Path(__file__).resolve().parent.parent / "internal" / "testdata" / "game"


def _counts(store):
    """The persisted attempt counts, without their times."""
    got = store.get_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY) or {}
    return {k: repair._attempt(v)[0] for k, v in got.items()}


def _real(name):
    f = _REAL / name
    if not f.is_file():
        pytest.skip(f"private test fixture not present: {name}")
    return f


async def test_real_rips_stored_by_an_older_version_get_their_games(store):
    """Real SPC / GBS / VGZ / minipsf rips stored as an older version stored
    them (no provenance; the PSF kept its game= album, the others had none):
    one header-game read gives each its game, and a second run has nothing."""
    from soniqboom.core import metadata
    names = ["mickey to minnie - magical adventure 2 (as1) (end boss 2).spc",
             "hello kitty no happy house.gbs", "09.vgz", "12 - kiss in the dark.minipsf"]
    rows = []
    for n in names:
        m = metadata.extract(_real(n), n).model_dump()
        m.pop("album_source", None)
        m["game"] = ""
        if m["format"] != "PSF":
            m["album"] = ""
        rows.append(m)
    store.upsert_tracks_batch(rows)
    cands = repair.find_album_backfill_candidates()
    assert len(cands) == 4
    for t in cands:
        assert await repair._process_local(t) == (True, True, None)
    games = {t["format"]: (t["game"], t["game_source"]) for t in store.all_tracks()}
    assert games == {"SPC": ("THE GREAT CIRCUS", "tag"),
                     "GBS": ("Hello Kitty no Happy House", "tag"),
                     "VGZ": ("Gargoyles", "tag"), "PSF": ("Metal Slug X", "tag")}
    assert store._candidate_ids(game="metal slug") == {names[3]}
    assert repair.find_album_backfill_candidates() == []


@pytest.mark.parametrize("name,limit", [("goldberg05.flac", 100_000),
                                        ("goldberg05.mp3", 150_000)])
def test_real_files_tag_window_matches_a_whole_read(tmp_path, name, limit):
    """A real FLAC (VORBIS_COMMENT + a PICTURE) and a real MP3 (ID3v2.3 with
    a cover), GAME-tagged: the tag window reads what a whole read does."""
    import os
    from soniqboom.core import metadata, tag_window, tagwriter
    f = tmp_path / name
    shutil.copy(_real("mainstream/" + name), f)
    tagwriter.write_tags(str(f), {"game": "Goldberg"})
    src = _CountingSource(f)
    w = tag_window.tag_window(src, name, os.path.splitext(name)[1], os.path.getsize(f))
    got, err = repair._re_extract_remote_sync(w, "ftp://h/s:/" + name, "x")
    full = metadata.extract(f, "x")
    assert err is None and src.full == 0 and src.read < limit
    for k in ("game", "title", "artist", "album", "year"):
        assert got[k] == getattr(full, k), k
    assert abs(got["duration"] - full.duration) < 0.5


# ── QA round 7 ───────────────────────────────────────────────────────────────

async def test_a_shares_offline_status_is_left_alone(store, monkeypatch):
    """The share monitor owns a network share's status: "Read game names"
    defers its files and never marks it online."""
    from soniqboom.api import admin
    store.upsert_scan_dir("ftp://nas/music", status="unavailable")
    h = store.list_scan_dirs()[0]["path_hash"]
    store.upsert_tracks_batch([_t("f", path="ftp://nas/music:/a.flac", format="FLAC",
                                  scan_root_hash=h)])
    seen = {}

    async def _start(cands, **kw):
        seen["ids"] = [t["id"] for t in cands]
        return True
    monkeypatch.setattr(repair, "start_repair", _start)
    r = await admin.metadata_backfill_game_albums(None, _tok="t")
    assert (r["started"], r["deferred"], seen) == (False, 1, {})
    assert store.list_scan_dirs()[0]["status"] == "unavailable"


async def test_an_empty_mount_point_is_deferred(tmp_path, store, monkeypatch):
    """An unplugged drive's mount point can stay as an empty directory."""
    from soniqboom.api import admin
    mnt = tmp_path / "ChipDrive"
    mnt.mkdir()
    store.upsert_scan_dir(str(mnt))
    store.upsert_tracks_batch([_t("s", path=str(mnt / "a.spc"), format="SPC"),
                               _t("m", path=str(mnt / "b.mp3"), format="MP3")])
    repair._backfill_settled.clear()
    assert await repair.run_album_backfill_once() is False
    assert not store.get_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY)
    assert store.list_scan_dirs()[0]["status"] == "ok"            # no status written

    async def _start(cands, **kw):
        raise AssertionError("no run for an absent folder")
    monkeypatch.setattr(repair, "start_repair", _start)
    r = await admin.metadata_backfill_game_albums({"include_remote": False}, _tok="t")
    assert (r["started"], r["deferred"]) == (False, 2)             # the rip and the MP3
    (mnt / "a.spc").write_bytes(b"x")                                # the drive is back
    assert await repair._offline_local_roots(store) == set()


async def test_the_one_time_backfill_waits_for_a_finishing_run(store, monkeypatch):
    import asyncio
    gate = asyncio.Event()

    async def _finishing():
        await gate.wait()
    task = asyncio.create_task(_finishing())
    monkeypatch.setattr(repair, "_task", task)
    monkeypatch.setattr(repair, "_progress", repair.RepairProgress(running=False))
    store.upsert_tracks_batch([_t("s", path="/nowhere/a.spc", format="SPC")])
    try:
        assert repair._busy() and not repair.is_running()
        assert await repair.run_album_backfill_once() is False
        assert await repair.run_remote_album_backfill("X") is None
    finally:
        gate.set()
        await task
    assert not repair._busy()


async def test_a_strict_reread_surfaces_an_io_error_a_helper_would_swallow(
        tmp_path, store, monkeypatch):
    import errno
    from pathlib import Path
    from soniqboom.core import metadata
    f = _psf(tmp_path / "a.minipsf", title="Prelude", game="Final Fantasy VII")
    real = Path.read_bytes

    def _eio(self):
        if self.name == "a.minipsf":
            raise OSError(errno.EIO, "Input/output error")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", _eio)
    assert metadata.extract(f, "x").title == "a"                   # a scan: the stub
    with pytest.raises(OSError):
        metadata.extract(f, "x", strict_io=True)
    store.upsert_tracks_batch([_t("p", path=str(f), format="PSF", title="a")])
    ok, applied, err = await repair._process_local(store.get_track("p"))
    assert (ok, err.split(":")[0]) == (False, "local-io")
    assert metadata._STRICT_IO.get() is False                      # reset after the call


def test_a_rescan_keeps_a_derived_spc_completion():
    from soniqboom.core.store import _carry_enrichment
    for src in ("folder", "songdb", "modland-filename", "modland"):
        old = {"album": "Street Fighter II - The World Warrior", "album_source": src,
               "format": "SPC", "file_md5": "m", "title": "t", "artist": ""}
        new = {"album": "Street Fighter II - The World Wa", "album_source": "tag",
               "format": "SPC", "file_md5": "m", "title": "t", "artist": ""}
        _carry_enrichment(old, new)
        assert (new["album"], new["album_source"]) == (old["album"], src), src


def test_an_m4a_game_in_another_case_is_read_and_replaced(tmp_path):
    from mutagen.mp4 import MP4, MP4FreeForm
    from soniqboom.core import metadata, tagwriter
    f = tmp_path / "t.m4a"
    _make(f, "aac")
    m = MP4(str(f))
    m["----:com.apple.iTunes:game"] = [MP4FreeForm(b"Old Game")]
    m.save()
    assert metadata.extract(f, "x").game == "Old Game"
    tagwriter.write_tags(str(f), {"game": "New Game"})
    assert [k for k in MP4(str(f)).tags if k.lower().endswith(":game")] == \
        ["----:com.apple.iTunes:GAME"]
    tagwriter.write_tags(str(f), {"game": ""})
    assert not [k for k in MP4(str(f)).tags if k.lower().endswith(":game")]
    assert metadata.extract(f, "x").game == ""


def test_attempt_counts_of_deleted_tracks_are_dropped(store):
    store.upsert_tracks_batch([_t("a"), _t("b")])
    repair._backfill_settled.clear()
    try:
        repair._settle(["a", "b"], {"a", "b"})
        store.delete_track("b")
        repair._settle(["a"], {"a"})
        assert _counts(store) == {"a": 2}
    finally:
        repair._backfill_settled.clear()


# ── QA round 8 ───────────────────────────────────────────────────────────────

class _EIOFile:
    """A file object whose reads fail with EIO from byte ``at`` on."""

    def __init__(self, path, at):
        self._f, self._at = open(path, "rb"), at

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._f.close()

    def seek(self, *a):
        return self._f.seek(*a)

    def tell(self):
        return self._f.tell()

    def read(self, n=-1):
        import errno
        if self._f.tell() >= self._at:
            raise OSError(errno.EIO, "Input/output error")
        return self._f.read(n if n >= 0 else self._at - self._f.tell())

    def close(self):
        self._f.close()


@pytest.mark.parametrize("helper,name,at", [
    ("_spc_xid6_game", "mickey to minnie - magical adventure 2 (as1) (end boss 2).spc", 0x10200),
    ("_vgm_gd3", "09.vgz", 0),
    ("_nsfe_auth", None, 8),
])
def test_each_helper_raises_an_io_error_only_when_strict(tmp_path, monkeypatch, helper, name, at):
    """The helpers that read an unreadable file as "no tags" let an I/O error
    through inside ``extract(strict_io=True)`` (``_swallow_io``)."""
    import builtins
    from soniqboom.core import metadata
    if name:
        f = _real(name)
    else:                                          # a minimal NSFe with an auth chunk
        f = tmp_path / "t.nsfe"
        body = b"Game\x00Artist\x00\x00\x00"
        f.write_bytes(b"NSFE" + len(body).to_bytes(4, "little") + b"auth" + body
                      + (0).to_bytes(4, "little") + b"NEND")
    real_open = builtins.open
    monkeypatch.setattr(metadata, "open",
                        lambda p, *a, **k: _EIOFile(p, at) if str(p) == str(f)
                        else real_open(p, *a, **k), raising=False)
    fn = getattr(metadata, helper)
    lax = fn(f)
    assert not (lax if isinstance(lax, (str, dict)) else any(lax))       # "no tags"
    tok = metadata._STRICT_IO.set(True)
    try:
        with pytest.raises(OSError):
            fn(f)
    finally:
        metadata._STRICT_IO.reset(tok)


def test_the_gme_header_and_aiff_raise_only_when_strict(tmp_path, monkeypatch):
    import builtins
    import errno
    from soniqboom.core import metadata
    nsf = _real("../gme/8bp028-b1-nullsleep-axel_f.nsf")
    real_open = builtins.open
    monkeypatch.setattr(metadata, "open",
                        lambda p, *a, **k: _EIOFile(p, 0) if str(p) == str(nsf)
                        else real_open(p, *a, **k), raising=False)
    assert metadata._extract_gme(nsf, "x")["title"] == nsf.stem     # lax: the stub
    tok = metadata._STRICT_IO.set(True)
    try:
        with pytest.raises(OSError):
            metadata._extract_gme(nsf, "x")
    finally:
        metadata._STRICT_IO.reset(tok)

    def _aiff_eio(path):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(metadata, "AIFF", _aiff_eio)
    f = tmp_path / "t.aiff"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=1",
                    str(f)], check=True)
    with pytest.raises(OSError):
        metadata.extract(f, "x", strict_io=True)


async def test_a_drive_mounted_inside_a_music_folder_is_counted(tmp_path, store):
    """The music folder is there (other drives in it); this drive's folder is
    an empty mount point: its files are "local-offline", retried and — like
    every unreachable file — given up after ``_MAX_UNREACHABLE_RUNS`` runs
    (a whole music folder that is gone is deferred uncounted instead)."""
    root = tmp_path / "mnt"
    (root / "other").mkdir(parents=True)
    (root / "other" / "x").write_bytes(b"x")
    (root / "chipdrive").mkdir()                              # empty mount point
    store.upsert_scan_dir(str(root))
    store.upsert_tracks_batch([_t("c", path=str(root / "chipdrive" / "a.spc"), format="SPC")])
    ok, applied, err = await repair._process_local(store.get_track("c"))
    assert err == "local-offline" and "local-offline" in repair._UNREACHABLE_REASONS
    repair._backfill_settled.clear()
    try:
        for _ in range(repair._MAX_UNREACHABLE_RUNS - 1):
            assert repair._settle(["c"], {"c"}) == {"c"}
        assert repair._settle(["c"], {"c"}) == set()
    finally:
        repair._backfill_settled.clear()


async def test_no_run_starts_while_another_is_settling(store, monkeypatch):
    """A one-time backfill run whose done-callback has not settled its ids
    yet holds every start (manual ones too)."""
    monkeypatch.setattr(repair, "_pending_settles", 1)
    assert repair._busy()
    assert await repair.start_repair([]) is False
    monkeypatch.setattr(repair, "_pending_settles", 0)
    assert await repair.start_repair([]) is True
    assert await repair.wait_idle(5)


async def test_a_hung_probe_times_out_on_its_own_pool(tmp_path, store, monkeypatch):
    import threading
    release = threading.Event()
    monkeypatch.setattr(repair, "_LOCAL_PROBE_TIMEOUT", 0.2)
    monkeypatch.setattr(repair, "_local_root_present", lambda p: release.wait(5) or False)
    monkeypatch.setattr(repair, "_probe_inflight", {})
    store.upsert_scan_dir(str(tmp_path))
    seen = []
    real = repair._probe_pool.submit
    monkeypatch.setattr(repair._probe_pool, "submit",
                        lambda *a, **k: seen.append(1) or real(*a, **k))
    try:
        assert await repair._offline_local_roots(store) == {str(tmp_path)}
        assert seen                                           # its own pool
    finally:
        release.set()


def test_a_rescan_keeps_an_spc_completion_stored_without_a_checksum():
    from soniqboom.core.store import _carry_enrichment
    old = {"album": "Street Fighter II - The World Warrior", "album_source": "folder",
           "format": "SPC", "title": "t", "artist": ""}
    new = {"album": "Street Fighter II - The World Wa", "album_source": "tag",
           "format": "SPC", "file_md5": "m", "title": "t", "artist": ""}
    _carry_enrichment(old, new)
    assert new["album_source"] == "folder"


def test_an_m4a_with_an_empty_and_a_filled_game_atom(tmp_path):
    from mutagen.mp4 import MP4, MP4FreeForm
    from soniqboom.core import metadata
    f = tmp_path / "t.m4a"
    _make(f, "aac")
    m = MP4(str(f))
    m["----:com.apple.iTunes:GAME"] = [MP4FreeForm(b"")]
    m["----:com.apple.iTunes:game"] = [MP4FreeForm(b"Real Game")]
    m.save()
    assert metadata.extract(f, "x").game == "Real Game"


async def test_a_run_started_during_the_probe_holds_the_backfill(tmp_path, store, monkeypatch):
    store.upsert_tracks_batch([_t("s", path=str(tmp_path / "a.spc"), format="SPC")])

    async def _probe_while_a_run_starts(st):
        monkeypatch.setattr(repair, "_pending_settles", 1)
        return set()
    monkeypatch.setattr(repair, "_offline_local_roots", _probe_while_a_run_starts)
    assert await repair.run_album_backfill_once() is False


async def test_the_runner_lets_the_local_backfill_finish_before_the_shares(monkeypatch):
    """The post-scan runner awaits the local one-time backfill (``wait_idle``)
    before the shares' own backfills — which would otherwise find the task
    busy and wait for their next scan, every scan."""
    import asyncio
    from soniqboom.core import demozoo, scanner, scene_metadata
    monkeypatch.setattr(scene_metadata, "has_index", lambda: False)
    monkeypatch.setattr(demozoo, "has_index", lambda: True)
    monkeypatch.setattr(demozoo, "auto_apply_enabled", lambda: True)

    async def _apply():
        return {"updated": 0}
    monkeypatch.setattr(demozoo, "apply_to_library", _apply)
    monkeypatch.setattr(scanner, "_SCENE_AUTOAPPLY_SETTLE_S", 0)
    monkeypatch.setattr(scanner, "_scene_autoapply_pending", False)
    monkeypatch.setattr(scanner, "_scene_autoapply_running", False)

    async def _local():                            # a run that settles 0.1 s later
        repair._pending_settles = 1
        asyncio.get_running_loop().call_later(0.1, setattr, repair, "_pending_settles", 0)
        return True
    monkeypatch.setattr(repair, "run_album_backfill_once", _local)
    seen = []

    async def _shares():
        seen.append(repair._busy())
    monkeypatch.setattr(scanner, "_run_remote_album_backfills", _shares)
    monkeypatch.setattr(scanner, "_schedule_folder_album_pass", lambda: None)
    scanner._spawn_scene_autoapply()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if seen and not scanner._scene_autoapply_running:
            break
    assert seen == [False]


# ── QA round 9 ───────────────────────────────────────────────────────────────

async def test_a_stuck_probe_holds_one_thread_per_folder(tmp_path, store, monkeypatch):
    """A folder whose last probe is still stuck is not probed again (at most
    one pool thread per stuck folder); the probe pool is the backfill's own."""
    import threading
    release = threading.Event()
    calls = []

    def _stuck(p):
        calls.append(p)
        release.wait(5)
        return True
    monkeypatch.setattr(repair, "_LOCAL_PROBE_TIMEOUT", 0.1)
    monkeypatch.setattr(repair, "_local_root_present", _stuck)
    monkeypatch.setattr(repair, "_probe_inflight", {})
    store.upsert_scan_dir(str(tmp_path))
    try:
        for _ in range(3):
            assert await repair._offline_local_roots(store) == {str(tmp_path)}
        assert calls == [str(tmp_path)]
    finally:
        release.set()
    from soniqboom.core import data
    assert repair._probe_pool is not data._probe_executor


async def test_the_runner_waits_for_the_local_backfill_only_so_long(monkeypatch):
    """A local backfill stuck on a hung read must not hold the post-scan pass
    (enrichment, folder pass, the shares' backfills)."""
    import asyncio
    from soniqboom.core import demozoo, scanner, scene_metadata
    monkeypatch.setattr(scene_metadata, "has_index", lambda: False)
    monkeypatch.setattr(demozoo, "has_index", lambda: True)
    monkeypatch.setattr(demozoo, "auto_apply_enabled", lambda: True)

    async def _apply():
        return {"updated": 0}
    monkeypatch.setattr(demozoo, "apply_to_library", _apply)
    monkeypatch.setattr(scanner, "_SCENE_AUTOAPPLY_SETTLE_S", 0)
    monkeypatch.setattr(scanner, "_BACKFILL_WAIT_S", 0.2)
    monkeypatch.setattr(scanner, "_scene_autoapply_pending", False)
    monkeypatch.setattr(scanner, "_scene_autoapply_running", False)

    async def _local():                            # never finishes
        monkeypatch.setattr(repair, "_pending_settles", 1)
        return True
    monkeypatch.setattr(repair, "run_album_backfill_once", _local)
    seen = []

    async def _shares():
        seen.append(1)
    monkeypatch.setattr(scanner, "_run_remote_album_backfills", _shares)
    folder = []
    monkeypatch.setattr(scanner, "_schedule_folder_album_pass", lambda: folder.append(1))
    scanner._spawn_scene_autoapply()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if seen and not scanner._scene_autoapply_running:
            break
    assert seen == [1] and folder == [1]


def test_a_finished_backfill_forgets_all_its_counts(store):
    store.upsert_tracks_batch([_t("a", path="/m/a.spc", format="SPC"),
                               _t("b", path="/m/b.spc", format="SPC"),
                               _t("r", path="ftp://h/s:/c.spc", format="SPC", scan_root_hash="R")])
    store.set_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY,
                     {"a": 3, "b": 1, "r": 2, "gone": 1})
    repair._forget_attempts(lambda t: not repair._is_remote(t.get("path") or ""))
    assert _counts(store) == {"r": 2}
    repair._forget_attempts(lambda t: t.get("scan_root_hash") == "R")
    assert _counts(store) == {}


def test_settle_does_not_count_a_track_deleted_meanwhile(store):
    store.upsert_tracks_batch([_t("a")])
    repair._backfill_settled.clear()
    try:
        assert repair._settle(["a", "gone"], {"a", "gone"}) == {"a"}
        assert _counts(store) == {"a": 1}
    finally:
        repair._backfill_settled.clear()


def test_an_eio_is_retried():
    import errno
    assert repair._local_io_reason(OSError(errno.EIO, "x")) == "local-io"
    assert "local-io" in repair._UNREACHABLE_REASONS


async def test_the_remote_backfill_counts_its_settle(store, monkeypatch):
    """A share's one-time backfill run holds ``_busy`` until its done-callback
    settled its ids."""
    import asyncio
    store.upsert_tracks_batch([_t("r", path="ftp://h/s:/c.spc", format="SPC",
                                  scan_root_hash="R")])
    gate = asyncio.Event()

    async def _start(cands, **kw):
        async def _run():
            await gate.wait()
        repair._task = asyncio.create_task(_run())
        return True
    monkeypatch.setattr(repair, "start_repair", _start)
    assert await repair.run_remote_album_backfill("R") is True
    assert repair._pending_settles == 1
    gate.set()
    await repair._task
    await asyncio.sleep(0)
    assert repair._pending_settles == 0


def test_a_strict_dsf_check_raises(tmp_path, monkeypatch):
    import builtins
    from soniqboom.core import metadata
    f = tmp_path / "a.dsf"
    f.write_bytes(b"PSF\x12" + b"\0" * 12 + b"[TAG]game=Shenmue\n")
    real_open = builtins.open
    monkeypatch.setattr(metadata, "open",
                        lambda p, *a, **k: _EIOFile(p, 0) if str(p) == str(f)
                        else real_open(p, *a, **k), raising=False)
    with pytest.raises(OSError):
        metadata.extract(f, "x", strict_io=True)


async def test_a_start_refused_while_finishing_says_so(store, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.api import admin
    store.upsert_tracks_batch([_t("m", path="/m/a.mp3", format="MP3")])
    monkeypatch.setattr(repair, "_pending_settles", 1)          # the last run finishing
    with pytest.raises(HTTPException) as e:
        await admin.metadata_backfill_game_albums(None, _tok="t")
    assert e.value.status_code == 409 and "still finishing" in e.value.detail


async def test_a_finished_local_backfill_clears_every_local_count(tmp_path, store):
    """The marker written, the counts of every local track go — also of one
    given up in an earlier run — and a share's stay."""
    f = _psf(tmp_path / "a.minipsf", title="T", game="G")
    store.upsert_scan_dir(str(tmp_path))
    store.upsert_tracks_batch([
        _t("a", path=str(f), format="PSF", title="T"),
        _t("old", path=str(tmp_path / "x.spc"), format="SPC", album="X", album_source="tag"),
        _t("r", path="ftp://h/s:/c.spc", format="SPC", scan_root_hash="R")])
    store.set_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY, {"old": 3, "r": 1})
    repair._backfill_settled.clear()
    assert await repair.run_album_backfill_once() is True
    assert await repair.wait_idle(5)
    assert store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY) is True
    assert _counts(store) == {"r": 1}


# ── QA round 10 ──────────────────────────────────────────────────────────────

def test_a_share_marked_done_forgets_only_its_counts(store):
    """Every way a share's backfill is marked done (a run, no candidates, a
    first full scan) drops that share's counts — and only that share's."""
    store.upsert_tracks_batch([
        _t("r1", path="ftp://h/s:/a.spc", format="SPC", scan_root_hash="R"),
        _t("q1", path="ftp://h/t:/b.spc", format="SPC", scan_root_hash="Q"),
        _t("l1", path="/m/c.spc", format="SPC")])
    store.set_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY,
                     {"r1": [1, 0.0], "q1": [2, 0.0], "l1": [1, 0.0]})
    repair.mark_remote_album_backfill_done("R")
    assert _counts(store) == {"q1": 2, "l1": 1}


async def test_a_backfill_with_nothing_left_forgets_its_counts(tmp_path, store):
    """The marker written without a run (no candidates left — e.g. the
    unreachable track was pruned by a scan) drops the local counts too."""
    store.upsert_tracks_batch([_t("x", path=str(tmp_path / "x.spc"), format="SPC",
                                  album="G", album_source="tag")])
    store.set_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY, {"x": [1, 0.0]})
    assert await repair.run_album_backfill_once() is False
    assert store.get_config(repair.ALBUM_BACKFILL_CONFIG_KEY) is True
    assert _counts(store) == {}


def test_a_failed_run_counts_at_most_once_an_hour(store, monkeypatch):
    """Runs come as often as scans end; giving up needs the failures spread
    over time (``_UNREACHABLE_INTERVAL_S``)."""
    monkeypatch.setattr(repair, "_UNREACHABLE_INTERVAL_S", 3600.0)
    store.upsert_tracks_batch([_t("a")])
    repair._backfill_settled.clear()
    try:
        for _ in range(10):                                   # ten quick passes
            assert repair._settle(["a"], {"a"}) == {"a"}
        assert _counts(store) == {"a": 1}
        # an hour later, twice more: given up
        got = store.get_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY)
        store.set_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY,
                         {"a": [got["a"][0], got["a"][1] - 3600]})
        assert repair._settle(["a"], {"a"}) == {"a"}
        got = store.get_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY)
        store.set_config(repair.ALBUM_BACKFILL_ATTEMPTS_CONFIG_KEY,
                         {"a": [got["a"][0], got["a"][1] - 3600]})
        assert repair._settle(["a"], {"a"}) == set()
        # an older version's bare count reads as long ago
        assert repair._attempt(2) == [2, 0.0]
    finally:
        repair._backfill_settled.clear()


async def test_two_callers_share_one_probe(tmp_path, store, monkeypatch):
    import asyncio
    import threading
    gate = threading.Event()
    calls = []

    def _slow(p):
        calls.append(p)
        gate.wait(2)
        return True
    monkeypatch.setattr(repair, "_local_root_present", _slow)
    store.upsert_scan_dir(str(tmp_path))
    a = asyncio.create_task(repair._offline_local_roots(store))
    await asyncio.sleep(0.05)
    b = asyncio.create_task(repair._offline_local_roots(store))
    await asyncio.sleep(0.05)
    gate.set()
    assert (await a, await b) == (set(), set())
    assert calls == [str(tmp_path)]


async def test_repair_start_refused_while_finishing_says_so(store, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.api import admin
    store.upsert_tracks_batch([_t("g", title="T\ufffdtle")])
    monkeypatch.setattr(repair, "_pending_settles", 1)
    with pytest.raises(HTTPException) as e:
        await admin.metadata_repair_start({"tracker_only": False}, _tok="t")
    assert e.value.status_code == 409 and "still finishing" in e.value.detail


def test_a_shift_jis_psf_tag_is_read(tmp_path):
    from soniqboom.core import metadata
    f = tmp_path / "a.minipsf"
    body = "title=\u5e8f\u66f2\ngame=\u30ed\u30c3\u30af\u30de\u30f38\n".encode("cp932")
    f.write_bytes(b"PSF\x01" + b"\0" * 12 + b"[TAG]" + body)
    m = metadata.extract(f, "x")
    assert (m.title, m.album) == ("\u5e8f\u66f2", "\u30ed\u30c3\u30af\u30de\u30f38")
    u = tmp_path / "u.minipsf"
    u.write_bytes(b"PSF\x01" + b"\0" * 12 + b"[TAG]utf8=1\ngame=" +
                  "\u00c9lan".encode("utf-8") + b"\n")
    assert metadata.extract(u, "x").album == "\u00c9lan"


def test_a_multi_value_game_frame_yields_its_first_value(tmp_path):
    from mutagen.id3 import ID3, TXXX
    from soniqboom.core import metadata
    f = tmp_path / "t.mp3"
    _make(f, "libmp3lame")
    tags = ID3(str(f))
    tags.add(TXXX(encoding=3, desc="GAME", text=["Contra", "Super C"]))
    tags.save(str(f))
    assert metadata.extract(f, "x").game == "Contra"


# ── game-name aliases ────────────────────────────────────────────────────────

def _modland_index(tmp_path, monkeypatch, rows):
    import sqlite3
    from soniqboom.core import scene_metadata as sm
    db = tmp_path / "modland.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.executemany("INSERT INTO mods VALUES (?,?)", rows)
    con.commit()
    con.close()
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    monkeypatch.setattr(sm, "_WITHDRAW_MIN_INDEX_ROWS", 1)


async def test_the_mickey_rip_is_found_by_every_name(tmp_path, store, monkeypatch):
    """The real SPC: its header calls the game "THE GREAT CIRCUS", Modland
    files it under "Mickey To Minnie - Magical Adventure 2".  After the
    Modland apply AND the header read, both find it; the header name is the
    game (the album), the Modland one an alias."""
    import hashlib
    from soniqboom.core import folder_album as fa
    from soniqboom.core import metadata, scene_metadata as sm

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    name = "mickey to minnie - magical adventure 2 (as1) (end boss 2).spc"
    f = tmp_path / name
    shutil.copy(_real(name), f)
    md5 = hashlib.md5(f.read_bytes()).hexdigest()
    _modland_index(tmp_path, monkeypatch, [
        (md5, f"Nintendo SPC/- unknown/Mickey To Minnie - Magical Adventure 2/{name}")])
    m = metadata.extract(f, "m").model_dump()
    # stored as the released version stored it: no album yet
    m.update(album="", game="", game_by_tag=None)
    m.pop("album_source", None)
    store.upsert_tracks_batch([m])
    await sm.apply_to_library()                         # Modland fills the empty album
    t = store.get_track("m")
    assert (t["album"], t["game"], t["game_by_modland"]) == (
        "Mickey To Minnie - Magical Adventure 2",) * 3
    assert await repair._process_local(store.get_track("m")) == (True, True, None)
    t = store.get_track("m")                            # the header's game wins the album
    assert (t["album"], t["album_source"], t["game"]) == ("THE GREAT CIRCUS", "tag",
                                                          "THE GREAT CIRCUS")
    assert t["game_aliases"] == ["Mickey To Minnie - Magical Adventure 2"]
    for q in ("mickey", "the great circus", "great circus", "mickey to minnie"):
        assert store._candidate_ids(game=q) == {"m"}, q
    assert "m" in store._candidate_ids(query="minnie")            # plain search too
    assert store.verify_indexes()["index_ok"]
    # a re-apply keeps the alias (the Modland name recorded, the album the header's)
    await sm.apply_to_library()
    assert store.get_track("m")["game_aliases"] == ["Mickey To Minnie - Magical Adventure 2"]


def test_a_withdrawn_album_takes_its_name_a_replaced_one_stays(store):
    store.upsert_tracks_batch([_t("a", album="Turrican", album_source="folder"),
                               _t("b", album="Katakis", album_source="modland")])
    assert store.get_track("a")["game_by_folder"] == "Turrican"      # recorded
    store.update_track_fields("a", {"album": "", "album_source": None})  # withdrawn
    a = store.get_track("a")
    assert (a["game"], a.get("game_by_folder"), a.get("game_aliases")) == ("", None, None)
    store.update_track_fields("b", {"album": "DENARIS", "album_source": "tag",
                                    "game_by_tag": "DENARIS"})   # replaced (the US title)
    b = store.get_track("b")
    assert (b["game"], b["game_aliases"]) == ("DENARIS", ["Katakis"])
    assert store._candidate_ids(game="katakis") == store._candidate_ids(game="denaris") == {"b"}
    assert "b" in store._candidate_ids(query="katakis")          # plain search: the alias
    assert store.verify_indexes()["index_ok"]


def test_aliases_are_distinct_and_a_cut_name_is_no_alias():
    t = {"album": "Street Fighter II - The World Warrior", "album_source": "modland",
         "game_by_tag": "Street Fighter II - The World Wa",
         "game_by_songdb": "street fighter ii - the world warrior",
         "game_by_folder": "SF2", "format": "SPC"}
    out = game_follow(t)
    assert out["game"] == "Street Fighter II - The World Warrior"
    assert out["game_aliases"] == ["SF2"]


def test_a_game_the_user_cleared_has_no_aliases(store):
    store.upsert_tracks_batch([_t("a", album="Turrican", album_source="modland",
                                  game_by_folder="Turrican Collection")])
    assert store.get_track("a")["game_aliases"] == ["Turrican Collection"]
    store.update_track_fields("a", {"game": "", "game_source": None, "user_edited": ["game"]})
    assert store.get_track("a").get("game_aliases") is None
    assert store._candidate_ids(game="turrican") == set()


def test_a_rescan_carries_the_names_of_the_same_file_only(store):
    store.upsert_tracks_batch([_t("a", game_by_folder="Folder Game", game_by_modland="ML",
                                  game_by_songdb="SDB", album="ML", album_source="modland")])
    store.upsert_tracks_batch([_t("a")])                              # same file
    a = store.get_track("a")
    assert (a["game_by_folder"], a["game_by_modland"], a["game_by_songdb"]) == \
        ("Folder Game", "ML", "SDB")
    store.upsert_tracks_batch([_t("a", file_md5="b" * 32)])           # another file
    a = store.get_track("a")
    assert (a.get("game_by_folder"), a.get("game_by_modland"), a.get("game_by_songdb")) == \
        ("Folder Game", None, None)
    assert (a["game"], a.get("game_aliases")) == ("Folder Game", None)


def test_load_records_the_name_of_an_older_album():
    s = TrackStore()
    s.bulk_load({"a": _t("a", album="Katakis", album_source="songdb")},
                {}, {}, {}, {}, [], {}, {}, {})
    assert s.get_track("a")["game_by_songdb"] == "Katakis"


async def test_options_off_clear_their_names(store, monkeypatch):
    from soniqboom.core import folder_album as fa

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    store.upsert_tracks_batch([
        _t("f", album="X", album_source="tag", game_by_tag="X", game_by_folder="Folder"),
        _t("g", album="X", album_source="tag", game_by_tag="X",
           game_by_modland_filename="Guess")])
    assert await fa.revert_album_source(fa.SOURCE_FOLDER) == 0       # no album cleared
    assert await fa.revert_album_source(fa.SOURCE_MODLAND_FILENAME) == 0
    assert store.get_track("f").get("game_by_folder") is None
    assert store.get_track("g").get("game_by_modland_filename") is None
    assert store.get_track("f").get("game_aliases") is None


def test_withdrawals_of_modland_and_the_song_database_clear_their_names():
    from soniqboom.core import scene_metadata as sm, songdb
    batch, expect = [], {}
    sm._withdraw_stale({"id": "a", "file_md5": "m", "game_by_modland": "ML",
                        "game_by_modland_filename": "G"}, batch, expect)
    assert batch == [("a", {"game_by_modland": None, "game_by_modland_filename": None})]
    assert songdb.reset_patch({"game_by_songdb": "S"}) == {"game_by_songdb": None}


def test_a_song_database_name_is_recorded_where_it_is_not_the_album():
    from soniqboom.core import songdb
    t = {"id": "a", "album": "Game", "album_source": "modland", "file_md5": "a" * 32,
         "genre": ["Amiga", "Module"], "format": "Amiga custom"}
    key = songdb._key(t)
    upd = songdb.patch_for(t, key=key, meta=("", "", "Megademo", ""), lengths=None,
                           withdraw_ok=True)
    assert upd["game_by_songdb"] == "Megademo" and "album" not in upd


async def test_modland_records_its_name_where_the_header_has_the_album(tmp_path, store, monkeypatch):
    """A fresh scan: the header game is the album first; the Modland apply
    that follows records its own name — an alias — and leaves the album."""
    from soniqboom.core import folder_album as fa
    from soniqboom.core import scene_metadata as sm

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    md5 = "c" * 32
    _modland_index(tmp_path, monkeypatch, [
        (md5, "Nintendo SPC/- unknown/Mickey To Minnie - Magical Adventure 2/x.spc")])
    store.upsert_tracks_batch([_t("m", path="/m/x.spc", format="SPC", file_md5=md5,
                                  album="THE GREAT CIRCUS", album_source="tag",
                                  game_by_tag="THE GREAT CIRCUS")])
    await sm.apply_to_library()
    m = store.get_track("m")
    assert (m["album"], m["game_by_modland"], m["game_aliases"]) == (
        "THE GREAT CIRCUS", "Mickey To Minnie - Magical Adventure 2",
        ["Mickey To Minnie - Magical Adventure 2"])
    assert store._candidate_ids(game="mickey") == {"m"}


# ── QA round 11 ──────────────────────────────────────────────────────────────

def test_a_game_change_logs_the_names_it_was_derived_from():
    """A name recorded only in memory at load reaches the AOF with the next
    game change, so a replay lands on the same aliases."""
    s = TrackStore()
    s.bulk_load({"h": _t("h", format="SPC", album="THE GREAT CIRCUS", album_source="tag")},
                {}, {}, {}, {}, [], {}, {}, {})
    records = []
    s._aof = lambda op, **kw: records.append(kw["data"])
    s.update_track_fields("h", {"album": "My Circus", "album_source": None,
                                "user_edited": ["album"]})
    assert records[-1]["game_by_tag"] == "THE GREAT CIRCUS"
    assert records[-1]["game_aliases"] == ["THE GREAT CIRCUS"]


def test_the_game_follows_precedence_not_just_the_album():
    t = {"album": "Megademo IV", "album_source": "songdb", "game_by_modland": "Turrican II",
         "format": "ProTracker"}
    out = game_follow(t)
    assert (out["game"], out["game_source"], out["game_aliases"]) == (
        "Turrican II", "modland", ["Megademo IV"])


def test_a_name_another_starts_with_is_dropped_wherever_it_stands():
    t = {"album": "Mine", "album_source": None, "user_edited": ["album"],
         "format": "ProTracker", "game_by_tag": "Contra", "game_by_modland": "Contra III"}
    assert game_follow(t)["game_aliases"] == ["Contra III"]


def test_a_cut_name_is_dropped_wherever_it_stands():
    t = {"album": "Mine", "album_source": None, "user_edited": ["album"], "format": "SPC",
         "game_by_tag": "Street Fighter II - The World Wa",
         "game_by_modland": "Street Fighter II - The World Warrior"}
    out = game_follow(t)
    assert out["game"] == "Mine"
    assert out["game_aliases"] == ["Street Fighter II - The World Warrior"]


@pytest.mark.parametrize("name,header", [
    ("mickey to minnie - magical adventure 2 (as1) (end boss 2).spc", "THE GREAT CIRCUS"),
    ("12 - kiss in the dark.minipsf", "Metal Slug X"),
])
def test_a_rescan_keeps_the_header_name_of_a_typed_album(tmp_path, store, name, header):
    """The extractor's own header name (GME / PSF): a rescan of a rip whose
    album the user typed keeps it as an alias."""
    from soniqboom.core import metadata
    f = tmp_path / name
    shutil.copy(_real(name), f)
    m = metadata.extract(f, "m").model_dump()
    store.upsert_tracks_batch([m])
    store.update_track_fields("m", {"album": "Mickey", "album_source": None,
                                    "user_edited": ["album"]})
    store.upsert_tracks_batch([metadata.extract(f, "m").model_dump()])     # rescan
    assert store.get_track("m")["game_aliases"] == [header]


async def test_a_typed_album_from_before_the_upgrade_gets_its_header_name(tmp_path, store):
    from soniqboom.core import metadata
    name = "mickey to minnie - magical adventure 2 (as1) (end boss 2).spc"
    f = tmp_path / name
    shutil.copy(_real(name), f)
    old = metadata.extract(f, "m").model_dump()
    old.update(album="Mickey", user_edited=["album"])
    for k in ("album_source", "game_by_tag"):
        old.pop(k, None)
    store.upsert_tracks_batch([old])
    assert [t["id"] for t in repair.find_album_backfill_candidates()] == ["m"]
    assert await repair._process_local(store.get_track("m")) == (True, True, None)
    m = store.get_track("m")
    assert (m["album"], m["game"], m["game_aliases"]) == ("Mickey", "Mickey", ["THE GREAT CIRCUS"])
    assert repair.find_album_backfill_candidates() == []


def test_a_modland_withdrawal_gives_the_header_album_back():
    from soniqboom.core import scene_metadata as sm
    batch, expect = [], {}
    sm._withdraw_stale({"id": "a", "file_md5": "m", "album": "Street Fighter II - The World Warrior",
                        "album_source": "modland", "game_by_modland": "x",
                        "game_by_tag": "Street Fighter II - The World Wa"}, batch, expect)
    assert batch[0][1]["album"] == "Street Fighter II - The World Wa"
    assert batch[0][1]["album_source"] == "tag"


async def test_the_meta_edit_answer_carries_the_aliases(store):
    from soniqboom.api.tracks import _MetaUpdate, update_meta
    store.upsert_tracks_batch([_t("a", format="SPC", album="THE GREAT CIRCUS", album_source="tag",
                                  game_by_tag="THE GREAT CIRCUS")])
    res = await update_meta("a", _MetaUpdate(album="My Circus"), user=None)
    assert res["applied"]["game_aliases"] == ["THE GREAT CIRCUS"]


def test_a_latin1_psf_tag_is_not_read_as_kanji(tmp_path):
    from soniqboom.core import metadata
    f = tmp_path / "a.minipsf"
    f.write_bytes(b"PSF\x01" + b"\0" * 12 + b"[TAG]" +
                  "artist=Frédéric Motte\ncopyright=© 1990\n".encode("latin-1"))
    assert metadata.extract(f, "x").artist == "Frédéric Motte"


def test_platform_and_publisher_folders_are_no_names():
    from soniqboom.core import folder_album as fa
    for n in ("Super Nintendo", "Nintendo Entertainment System", "Konami", "Capcom",
              "Soundtracks", "snesmusic org"):
        assert n.lower() in fa._GENERIC_NAMES or all(
            w in fa._GENERIC_NAMES for w in n.lower().split()), n


async def test_a_track_the_album_pass_refuses_gets_no_folder_game(store, monkeypatch):
    """The folder names of the tracks the album pass judges are its own
    verdicts: the every-track judging (where other albums' tracks vote too)
    must not give a refused track a game — a composer under an alias."""
    from soniqboom.core import folder_album as fa
    from soniqboom.core import scene_metadata as sm

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    monkeypatch.setattr(sm, "modland_name_keys", lambda: (frozenset(), frozenset()))

    def T(tid, path, artist, album="", src=None):
        return {"id": tid, "path": path, "artist": artist, "album": album, "album_source": src,
                "format": "ProTracker", "title": tid, "genre": [], "file_md5": tid.ljust(32, "0")}
    rows = [T(f"p{i}{j}", f"/music/Chip/{who}/tune{j}.mod", who)
            for i, who in enumerate(["Tim Follin", "Martin Galway", "Allister Brimble",
                                     "Matt Furniss"]) for j in range(2)]
    rows += [T("x1", "/music/Chip/Mr Tickle/a.mod", "Xeron"),
             T("x2", "/music/Chip/Mr Tickle/b.mod", "Xeron")]
    rows += [T(f"g{i}{j}", f"/music/Chip/{g}/t{j}.mod", "Chris Huelsbeck", album=g, src="modland")
             for i, g in enumerate(["Turrican", "Katakis", "Apidya", "Lionheart"]) for j in range(2)]
    store.upsert_tracks_batch(rows)
    store.set_config(fa.CONFIG_KEY, True)
    store.upsert_scan_dir("/music")
    await fa.apply_folder_albums(force=True)
    x1 = store.get_track("x1")
    assert (x1["album"], x1.get("game") or "", x1.get("game_by_folder")) == ("", "", None)
    assert store._candidate_ids(game="mr tickle") == set()


# ── QA round 12 ──────────────────────────────────────────────────────────────

def test_the_browse_cache_carries_the_game_fields():
    """Rows saved by a build without the game fields are not reused."""
    from soniqboom.api import fstree
    assert fstree._BROWSE_CACHE_VERSION >= 2


async def test_edits_refresh_the_folder_browse_row(store, monkeypatch):
    from soniqboom.api import tracks as api
    from soniqboom.core import folder_album as fa
    seen = []

    async def _refresh(ids):
        seen.append(list(ids))
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    store.upsert_tracks_batch([_t("a", album="Turrican", album_source="modland")])
    await api.update_meta("a", api._MetaUpdate(game="Typed Game"), user=None)
    await api.update_year("a", api._YearUpdate(year=1991), user=None)
    assert seen == [["a"], ["a"]]


async def test_a_withdrawn_folder_album_gives_the_header_name_back(store, monkeypatch):
    from soniqboom.core import folder_album as fa

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    store.upsert_tracks_batch([_t("s", format="SPC", album="Street Fighter II - The World Warrior",
                                  album_source="folder",
                                  game_by_tag="Street Fighter II - The World Wa")])
    assert await fa.revert_album_source(fa.SOURCE_FOLDER) == 1
    s = store.get_track("s")
    assert (s["album"], s["album_source"], s["game"]) == (
        "Street Fighter II - The World Wa", "tag", "Street Fighter II - The World Wa")


def test_a_files_own_album_replacing_a_derived_one_keeps_its_name(store):
    store.upsert_tracks_batch([_t("m", album="Turrican", album_source="modland")])
    store.update_track_fields("m", {"album": "Turrican OST", "album_source": None})
    m = store.get_track("m")
    assert (m["game"], m["game_source"], m["game_by_modland"]) == ("Turrican", "modland", "Turrican")


async def test_a_folder_the_album_pass_refused_names_none_of_its_tracks(store, monkeypatch):
    from soniqboom.core import folder_album as fa
    from soniqboom.core import scene_metadata as sm

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    monkeypatch.setattr(sm, "modland_name_keys", lambda: (frozenset(), frozenset()))

    def T(tid, path, artist, album="", src=None):
        return {"id": tid, "path": path, "artist": artist, "album": album, "album_source": src,
                "format": "ProTracker", "title": tid, "genre": [], "file_md5": tid.ljust(32, "0")}
    rows = [T(f"p{i}{j}", f"/music/Chip/{who}/tune{j}.mod", who)
            for i, who in enumerate(["Tim Follin", "Martin Galway", "Allister Brimble",
                                     "Matt Furniss"]) for j in range(2)]
    rows += [T("x1", "/music/Chip/Mr Tickle/a.mod", "Xeron"),
             T("x2", "/music/Chip/Mr Tickle/b.mod", "Xeron", album="Some Demo", src="songdb")]
    rows += [T(f"g{i}{j}", f"/music/Chip/{g}/t{j}.mod", "Chris Huelsbeck", album=g, src="modland")
             for i, g in enumerate(["Turrican", "Katakis", "Apidya", "Lionheart"]) for j in range(2)]
    store.upsert_tracks_batch(rows)
    store.set_config(fa.CONFIG_KEY, True)
    store.upsert_scan_dir("/music")
    await fa.apply_folder_albums(force=True)
    assert store.get_track("x2").get("game_by_folder") is None
    assert store._candidate_ids(game="mr tickle") == set()
    assert store.get_track("g00")["game_by_folder"] == "Turrican"


@pytest.mark.parametrize("text,enc", [("Don’t Stop", "cp1252"), ("Game – Title", "cp1252"),
                                      ("Frédéric", "latin-1"), ("序曲", "cp932"),
                                      ("ロックマン8", "cp932")])
def test_psf_tag_text_encodings(text, enc):
    from soniqboom.core.metadata import _psf_tag_text
    assert _psf_tag_text(text.encode(enc)) == text


async def test_a_folder_album_no_longer_derived_gives_the_header_name_back(store, monkeypatch):
    """The folder pass re-judges an album it stamped: withdrawn (the folder
    is a platform folder now), the header's own name is the album again."""
    from soniqboom.core import folder_album as fa
    from soniqboom.core import scene_metadata as sm

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    monkeypatch.setattr(sm, "modland_name_keys", lambda: (frozenset(), frozenset()))
    store.upsert_tracks_batch([_t("s", path="/music/Super Nintendo/a.spc", format="SPC",
                                  album="Super Nintendo", album_source="folder",
                                  game_by_tag="Street Fighter II")])
    store.set_config(fa.CONFIG_KEY, True)
    store.upsert_scan_dir("/music")
    await fa.apply_folder_albums(force=True)
    s = store.get_track("s")
    assert (s["album"], s["album_source"], s.get("game_by_folder")) == (
        "Street Fighter II", "tag", None)


def test_a_song_database_reset_gives_the_header_name_back():
    from soniqboom.core import songdb
    upd = songdb.reset_patch({"album": "Megademo", "album_source": "songdb",
                              "game_by_tag": "Header Game", "game_by_songdb": "Megademo"})
    assert (upd["album"], upd["album_source"], upd["game_by_songdb"]) == ("Header Game", "tag", None)


# ── QA round 13 ──────────────────────────────────────────────────────────────

def _folder_pass_env(store, monkeypatch):
    from soniqboom.core import folder_album as fa
    from soniqboom.core import scene_metadata as sm

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    monkeypatch.setattr(sm, "modland_name_keys", lambda: (frozenset(), frozenset()))
    store.set_config(fa.CONFIG_KEY, True)
    store.upsert_scan_dir("/music")
    return fa


def _mod(tid, path, artist, album="", src=None, title=None):
    return {"id": tid, "path": path, "artist": artist, "album": album, "album_source": src,
            "format": "ProTracker", "title": title or tid, "genre": [],
            "file_md5": tid.ljust(32, "0")}


async def test_the_folder_pass_counts_albums_apart_from_game_names(store, monkeypatch):
    from soniqboom.api import admin
    _folder_pass_env(store, monkeypatch)
    for target in ("soniqboom.core.data.get_store",):
        monkeypatch.setattr(target, lambda: store)
    store.set_config("retro_album_from_folder", False)
    store.upsert_tracks_batch([
        _mod("a", "/music/Games/Turrican/intro.mod", "Chris Huelsbeck"),
        _mod("b", "/music/Games/Turrican/ingame.mod", "Chris Huelsbeck"),
        _mod("c", "/music/Games/Turrican/outro.mod", "Chris Huelsbeck",
             album="Turrican", src="modland"),
    ])
    res = await admin.update_settings({"retro_album_from_folder": True})
    assert (res["folder_albums_filled"], res["folder_tracks_updated"]) == (2, 3)
    assert store.get_track("c")["game_by_folder"] == "Turrican"
    res = await admin.update_settings({"retro_album_from_folder": False})
    assert (res["folder_albums_reverted"], res["folder_tracks_updated"]) == (2, 3)
    assert store.get_track("c").get("game_by_folder") is None


async def test_the_file_name_option_off_counts_the_names_it_removes(store, monkeypatch):
    from soniqboom.api import admin
    _folder_pass_env(store, monkeypatch)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    store.set_config("modland_filename_game", True)
    store.upsert_tracks_batch([
        _mod("g", "/music/a/gold.mod", "David Whittaker", album="Gold of the Aztecs",
             src="modland-filename"),
        _mod("h", "/music/b/hdr.mod", "David Whittaker", album="Header Game", src="tag"),
    ])
    store.update_track_fields("h", {"game_by_modland_filename": "Aztecs"})
    res = await admin.update_settings({"modland_filename_game": False})
    assert (res["modland_albums_reverted"], res["modland_tracks_updated"]) == (1, 2)


async def test_the_file_name_option_off_counts_the_folder_pass_after_it(store, monkeypatch):
    """Nothing to revert, but the folder pass that follows fills an album:
    the open view must still refresh."""
    from soniqboom.api import admin
    _folder_pass_env(store, monkeypatch)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: store)
    store.set_config("modland_filename_game", True)
    store.upsert_tracks_batch([_mod("a", "/music/Games/Turrican/intro.mod", "Chris Huelsbeck")])
    res = await admin.update_settings({"modland_filename_game": False})
    assert (res["modland_albums_reverted"], res["modland_tracks_updated"]) == (0, 1)
    assert store.get_track("a")["album"] == "Turrican"


async def test_a_title_tune_takes_its_folders_album(store, monkeypatch):
    """A game folder's title tune (named after the folder) takes the album
    its siblings get — also when the only sibling has another album."""
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_tracks_batch([
        _mod("l1", "/music/Games/Lotus Turbo Challenge/lotus turbo challenge.mod", "Barry Leitch",
             title="Lotus Turbo Challenge"),
        _mod("l2", "/music/Games/Lotus Turbo Challenge/ingame.mod", "Barry Leitch"),
        _mod("t1", "/music/Games/Turrican/turrican.mod", "Chris Huelsbeck", title="Turrican"),
        _mod("t2", "/music/Games/Turrican/level1.mod", "Chris Huelsbeck",
             album="Turrican", src="modland"),
        # one tune named after its folder, alone: still no album
        _mod("e1", "/music/Games/Enigma/enigma.mod", "Someone", title="Enigma"),
    ])
    await fa.apply_folder_albums(force=True)
    for tid, album in (("l1", "Lotus Turbo Challenge"), ("l2", "Lotus Turbo Challenge"),
                       ("t1", "Turrican")):
        t = store.get_track(tid)
        assert (t["album"], t["album_source"], t["game_by_folder"]) == (album, "folder", album), tid
    assert store.get_track("t2")["game_by_folder"] == "Turrican"
    e1 = store.get_track("e1")
    assert (e1["album"], e1.get("game_by_folder")) == ("", None)
    assert (await fa.apply_folder_albums(force=True))["updated"] == 0


def test_a_compo_pack_of_one_tune_folders_stays_the_archives_release():
    """Folders each named after their only tune (a compo pack) are not
    albums, and no title-tune rule makes them one."""
    from soniqboom.core import folder_album as fa
    tracks = [_mod(f"c{i}", f"/music/bcompo5.zip::{n}/{n}.mod", f"Artist {i}", title=n)
              for i, n in enumerate(["weird", "happy", "sad", "fast"])]
    out = fa.collect_folder_updates(tracks, roots=frozenset({"/music"}), format_keys=frozenset(),
                                    author_keys=frozenset(), artist_keys=frozenset())
    assert {upd["album"] for _tid, upd, _e in out} == {"bcompo5"}


async def test_a_wrapper_archive_in_a_refused_folder_names_nothing(store, monkeypatch):
    """The refused-folder rule keys on the folder the album pass judged: a
    tune in a single-file wrapper archive belongs to the folder around it."""
    fa = _folder_pass_env(store, monkeypatch)
    rows = [_mod(f"p{i}{j}", f"/music/Chip/{who}/tune{j}.mod", who)
            for i, who in enumerate(["Tim Follin", "Martin Galway", "Allister Brimble",
                                     "Matt Furniss"]) for j in range(2)]
    rows += [_mod("x1", "/music/Chip/Mr Tickle/a.mod.zip::a.mod", "Xeron"),
             _mod("x2", "/music/Chip/Mr Tickle/b.mod", "Xeron", album="Some Demo", src="songdb")]
    rows += [_mod(f"g{i}{j}", f"/music/Chip/{g}/t{j}.mod", "Chris Huelsbeck", album=g, src="modland")
             for i, g in enumerate(["Turrican", "Katakis", "Apidya", "Lionheart"]) for j in range(2)]
    store.upsert_tracks_batch(rows)
    await fa.apply_folder_albums(force=True)
    assert store.get_track("x2").get("game_by_folder") is None
    assert store._candidate_ids(game="mr tickle") == set()


def _collect(tracks):
    from soniqboom.core import folder_album as fa
    out = fa.collect_folder_updates(tracks, roots=frozenset({"/music"}), format_keys=frozenset(),
                                    author_keys=frozenset(), artist_keys=frozenset())
    return {tid: upd["album"] for tid, upd, _e in out}


def test_a_title_tune_takes_what_its_siblings_get():
    """In a release archive the archive's name; under an index of composers
    (a composer under an alias) nothing."""
    z5 = "/music/compos/bcompo5.zip::"
    got = _collect([
        _mod("w", z5 + "weird/FF_2MASG.XM", "Funky Fish"),
        _mod("w2", z5 + "weird/vibe-ishould.it", "Vibe"),
        _mod("w3", z5 + "weird/weird.xm", "Vibe", title="weird"),
        _mod("h", z5 + "happy/FF_JOTR.XM", "Funky Fish"),
        _mod("h2", z5 + "happy/dl-unity.xm", "Dodging Liquid"),
        _mod("r", z5 + "remix/cerror.mod", "Cerror"),
        _mod("x", z5 + "Xeron/tune.mod", "Xeron"),
    ])
    assert got == {i: "bcompo5" for i in ("w", "w2", "w3", "h", "h2", "r", "x")}
    got = _collect([_mod(f"p{i}{j}", f"/music/Chip/{who}/tune{j}.mod", who)
                    for i, who in enumerate(["Tim Follin", "Martin Galway", "Allister Brimble",
                                             "Matt Furniss"]) for j in range(2)]
                   + [_mod("x1", "/music/Chip/Mr Tickle/a.mod", "Xeron"),
                      _mod("x2", "/music/Chip/Mr Tickle/mr tickle.mod", "Xeron", title="Mr Tickle")])
    assert "x1" not in got and "x2" not in got


async def test_a_folder_the_album_pass_took_for_one_track_names_its_others(store, monkeypatch):
    """The album pass gives one track of a folder the release archive's name
    and refuses another (its composer names the archive), while the every-
    track judging — where games of other albums vote too — names both after
    the folder: the folder was accepted, so its other-album track keeps the
    folder's name."""
    fa = _folder_pass_env(store, monkeypatch)
    z = "/music/compos/bcompo5.zip::"
    store.upsert_tracks_batch([
        _mod("a", z + "weird/a.xm", "Funky Fish"),
        _mod("b", z + "weird/b.xm", "Bcompo5 Crew"),
        _mod("c", z + "weird/c.xm", "Vibe", album="Weird Dreams", src="modland"),
        _mod("h", z + "happy/h.xm", "Funky Fish"),
        _mod("h2", z + "happy/h2.xm", "Dodging Liquid"),
        _mod("r", z + "remix/r.mod", "Cerror"),
        _mod("x", z + "Xeron/x.mod", "Xeron"),
        *(_mod(f"g{i}", z + f"{g}/t.mod", who, album=g, src="modland")
          for i, (g, who) in enumerate([("Turrican", "Chris Huelsbeck"), ("Lotus", "Barry Leitch"),
                                        ("Apidya", "Chris Huelsbeck")])),
    ])
    await fa.apply_folder_albums(force=True)
    assert (store.get_track("a")["album"], store.get_track("b")["album"]) == ("bcompo5", "")
    assert store.get_track("c")["game_by_folder"] == "weird"


async def test_a_folder_accepted_for_one_track_names_its_others(store, monkeypatch):
    """A folder the album pass accepts for one track and refuses for another
    (whose composer it names) still names its other-album tracks."""
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_tracks_batch([
        _mod("a", "/music/Games/Katakis/intro.mod", "Chris Huelsbeck"),
        _mod("k", "/music/Games/Katakis/remix.mod", "Katakis Crew"),
        _mod("c", "/music/Games/Katakis/outro.mod", "Chris Huelsbeck",
             album="Katakis OST", src="modland"),
    ])
    await fa.apply_folder_albums(force=True)
    assert (store.get_track("a")["album"], store.get_track("k")["album"]) == ("Katakis", "")
    assert store.get_track("c")["game_by_folder"] == "Katakis"


async def test_a_withdrawal_to_the_header_name_settles_in_one_pass(store, monkeypatch):
    fa = _folder_pass_env(store, monkeypatch)
    cut, full = "Street Fighter II - The World Wa", "Street Fighter II - The World Warrior"
    rows = [{**_mod("sf", f"/music/SNES/{full}/sf2-01.spc", "Yoko Shimomura", album=full,
                    src="folder", title="Opening"), "format": "SPC", "game_by_tag": cut}]
    rows += [_mod(f"p{i}", f"/music/SNES/{who}/tune.mod", who)
             for i, who in enumerate(["Jogeir Liljedahl", "Martin Galway", "Allister Brimble"])]
    rows += [{**_mod(f"g{i}", f"/music/SNES/{g}/01.spc", "Someone", album=g, src="tag",
                     title="Title"), "format": "SPC", "game_by_tag": g}
             for i, g in enumerate(["Chrono Trigger", "Secret of Mana", "Super Metroid",
                                    "F-Zero", "Pilotwings"])]
    store.upsert_tracks_batch(rows)
    await fa.apply_folder_albums(force=True)
    sf = store.get_track("sf")
    assert (sf["album"], sf["album_source"], sf["game_by_folder"], sf["game"]) == (
        cut, "tag", full, full)
    assert (await fa.apply_folder_albums(force=True))["updated"] == 0


@pytest.mark.parametrize("text", ["サガ", "ガイア外伝", "海", "格闘", "カオス", "ガイア (1995)",
                                  "ロックマンX", "ギガWing"])
def test_a_short_japanese_psf_tag_is_read(text):
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=Opening\ngame=" + text.encode("cp932") + b"\nartist=Kenji Ito\n"
    assert _psf_tag_text(blob).split("game=")[1].split("\n")[0] == text


@pytest.mark.parametrize("text", ["Škoda", "Dušan Maleš", "Œuvre", "L’Été", "“Live”",
                                  "Rock ‘n’ Roll", "ŠKODA", "Chrono Trigger™", "€5 Bonus"])
def test_western_psf_tags_with_windows_letters_stay_western(text):
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=Opening\ngame=" + text.encode("cp1252") + b"\n"
    assert _psf_tag_text(blob).split("game=")[1].split("\n")[0] == text


async def test_a_tag_edit_refreshes_the_folder_browse_row(tmp_path, store, monkeypatch):
    from soniqboom.api import tracks as api
    from soniqboom.core import folder_album as fa
    f = tmp_path / "g.mp3"
    shutil.copy(_real("mainstream/goldberg05.mp3"), f)
    seen = []

    async def _refresh(ids):
        seen.append(list(ids))
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    store.upsert_tracks_batch([_t("a", path=str(f), format="MP3")])

    class _U:
        role = "admin"
    await api.update_tags("a", api._TagUpdate(game="Aria Game"), user=_U())
    assert seen == [["a"]] and store.get_track("a")["game"] == "Aria Game"


def test_a_song_database_row_without_a_name_gives_the_header_name_back():
    from soniqboom.core import songdb
    t = {"id": "a", "album": "Megademo", "album_source": "songdb", "game_by_songdb": "Megademo",
         "game_by_tag": "Header Game", "file_md5": "a" * 32, "genre": ["Amiga", "Module"],
         "format": "Amiga custom", "songdb_fields": []}
    upd = songdb.patch_for(t, key=songdb._key(t), meta=("", "", "", ""), lengths=None,
                           withdraw_ok=True)
    assert (upd["album"], upd["album_source"], upd["game_by_songdb"]) == ("Header Game", "tag", None)
    # a missing row withdraws only when the index is complete
    assert songdb.patch_for(t, key=songdb._key(t), meta=None, lengths=None,
                            withdraw_ok=False) in (None, {})


def test_a_modland_file_name_album_switched_off_gives_the_header_name_back():
    from soniqboom.core import scene_metadata as sm
    t = {"id": "a", "album": "Gold of the Aztecs", "album_source": "modland-filename",
         "game_by_modland_filename": "Gold of the Aztecs", "game_by_tag": "Aztecs",
         "format": "ProTracker", "title": "intro"}
    mp = sm.ModlandPath(("Protracker",), "David Whittaker", (), None, "gold of the aztecs-intro.mod")
    upd = sm.album_update_for(t, mp, use_filename=False, want=(None, None))
    assert (upd["album"], upd["album_source"]) == ("Aztecs", "tag")


# ── QA round 14 ──────────────────────────────────────────────────────────────

def _boot(tracks: dict) -> TrackStore:
    import copy
    s = TrackStore()
    s.bulk_load(copy.deepcopy(tracks), {}, {}, {}, {}, [], {}, {}, {})
    s.rebuild_indexes()
    return s


def _restart(snapshot: dict, records: list) -> TrackStore:
    """A restart: the AOF records folded into the snapshot, then a load."""
    import copy
    from soniqboom.core import merger
    state = {"tracks": copy.deepcopy(snapshot)}
    for e in records:
        merger._apply_entry(state, e)
    return _boot(state["tracks"])


def _logged(s: TrackStore) -> list:
    import json
    recs: list = []
    s._aof = lambda op, **kw: recs.append({"op": op, **json.loads(json.dumps(kw))})
    return recs


_GAME_STATE = ("album", "album_source", "game", "game_source", "game_aliases", "game_by_tag",
               "game_by_modland", "game_by_folder", "user_edited")


def _same_after_restart(live: TrackStore, snapshot: dict, recs: list, tid: str) -> TrackStore:
    reb = _restart(snapshot, recs)
    assert ({f: live.get_track(tid).get(f) or None for f in _GAME_STATE}
            == {f: reb.get_track(tid).get(f) or None for f in _GAME_STATE})
    return reb


def test_a_renamed_derived_game_keeps_its_source_across_a_restart():
    """The snapshot holds no game (derived at load): a record changing the
    game but not its source must still carry the source, or the game loads as
    the file's own GAME tag and is never replaced again."""
    snap = {"a": _t("a", album="Turrican", album_source="modland")}
    live = _boot(snap)
    recs = _logged(live)
    live.update_track_fields("a", {"album": "Turrican II", "album_source": "modland"})
    reb = _same_after_restart(live, snap, recs, "a")
    for s in (live, reb):
        s._aof = lambda op, **kw: None
        s.update_track_fields("a", {"album": "", "album_source": None})
        assert (s.get_track("a")["game"], s._candidate_ids(game="turrican")) == ("", set())


def test_a_typed_game_equal_to_the_derived_one_survives_a_restart():
    snap = {"a": _t("a", album="Turrican", album_source="modland")}
    live = _boot(snap)
    recs = _logged(live)
    live.update_track_fields("a", {"game": "Turrican", "user_edited": ["game"]})
    reb = _same_after_restart(live, snap, recs, "a")
    assert reb.get_track("a")["game"] == "Turrican"


def test_game_state_survives_a_restart_after_random_edits():
    import random
    names = ["Turrican", "Turrican II", "Street Fighter II - The World Wa",
             "Street Fighter II - The World Warrior", "Pokémon", "The Last Ninja", ""]
    srcs = ["tag", "modland", "songdb", "modland-filename", "folder", None]
    for seed in range(60):
        rnd = random.Random(seed)
        snap = {}
        for i in range(4):
            t = _t(f"t{i}", format=rnd.choice(["ProTracker", "SPC", "MP3", "SID"]))
            if rnd.random() < .6:
                t["album"] = rnd.choice(names)
                t["album_source"] = rnd.choice(srcs) if t["album"] else None
            if rnd.random() < .3:
                t["game_by_tag"] = rnd.choice(names) or None
            snap[t["id"]] = t
        live = _boot(snap)
        recs = _logged(live)
        for _ in range(10):
            tid = rnd.choice(list(snap))
            k = rnd.random()
            if k < .4:
                a = rnd.choice(names)
                p = {"album": a, "album_source": rnd.choice(srcs) if a else None}
            elif k < .6:
                p = {"album": "", "album_source": None}
            elif k < .75:
                p = {rnd.choice(["game_by_tag", "game_by_modland", "game_by_folder"]):
                     rnd.choice(names) or None}
            elif k < .9:
                p = {"game": rnd.choice(names), "game_source": None, "user_edited": ["game"]}
            else:
                p = {"album": rnd.choice(names), "album_source": None, "user_edited": ["album"]}
            live.update_track_fields(tid, p)
        for tid in snap:
            _same_after_restart(live, snap, recs, tid)


def test_a_sid_with_a_folder_game_is_still_found_by_its_title(store):
    store.upsert_tracks_batch([
        _t("s", format="SID", title="Comic Bakery", album="c64 convertions", album_source="folder"),
        _t("m", format="SID", title="Central Park Loader", album="Last Ninja 2",
           album_source="modland")])
    assert store._candidate_ids(game="comic bakery") == {"s"}
    assert store._candidate_ids(game="c64 conv") == {"s"}
    # a game from a surer source: the title is no game name
    assert store._candidate_ids(game="central park") == set()


async def test_a_tune_named_after_its_game_archive_stays_in_it(store, monkeypatch):
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_tracks_batch([
        _mod("r1", "/music/Games/Turrican.lha::turrican.mod", "Chris Huelsbeck", title="Turrican"),
        _mod("r2", "/music/Games/Turrican.lha::level1.mod", "Chris Huelsbeck"),
        # a single-file wrapper is still skipped outward, and so is an
        # archive named after a module holding two versions of it
        _mod("w1", "/music/Games/Lotus/lotus.mod.zip::lotus.mod", "Barry Leitch"),
        _mod("w2", "/music/Games/Lotus/ingame.mod", "Barry Leitch"),
        _mod("v1", "/music/Games/Apidya/apidya.xm.zip::apidya.xm", "Chris Huelsbeck"),
        _mod("v2", "/music/Games/Apidya/apidya.xm.zip::apidya2.xm", "Chris Huelsbeck"),
    ])
    await fa.apply_folder_albums(force=True)
    got = {tid: store.get_track(tid)["album"] for tid in ("r1", "r2", "w1", "w2", "v1", "v2")}
    assert got == {"r1": "Turrican", "r2": "Turrican", "w1": "Lotus", "w2": "Lotus",
                   "v1": "Apidya", "v2": "Apidya"}
    assert (await fa.apply_folder_albums(force=True))["updated"] == 0


async def test_withdrawals_to_the_header_name_settle_the_vote_in_one_pass(store, monkeypatch):
    """A stamped album withdrawn to the header's name leaves the vote at
    once, as the next pass sees it: the composer index it made (Mr Tickle is
    Xeron) holds in the same pass."""
    fa = _folder_pass_env(store, monkeypatch)
    rows = [_mod(f"p{i}", f"/music/Chip/{who}/tune.mod", who)
            for i, who in enumerate(["Jogeir Liljedahl", "Martin Galway", "Allister Brimble"])]
    rows += [_mod("x1", "/music/Chip/Mr Tickle/a.mod", "Xeron"),
             _mod("e1", "/music/Chip/misc/e.mod", "Someone"),
             _mod("f1", "/music/Chip/unsorted/f.mod", "Someone Else"),
             {**_mod("g1", "/music/Chip/other/g.spc", "Composer G", album="Old Folder Name",
                     src="folder"), "format": "SPC", "game_by_tag": "Header Game"}]
    store.upsert_tracks_batch(rows)
    await fa.apply_folder_albums(force=True)
    assert (store.get_track("g1")["album"], store.get_track("x1")["album"]) == ("Header Game", "")
    assert (await fa.apply_folder_albums(force=True))["updated"] == 0


async def test_a_withdrawn_folder_album_leaves_no_folder_name(store, monkeypatch):
    """Withdrawn to nothing (no header name), the album pass's refusal
    stands for the game name too — the every-track vote names the folder."""
    fa = _folder_pass_env(store, monkeypatch)
    rows = [_mod(f"p{i}{j}", f"/music/Chip/{who}/tune{j}.mod", who)
            for i, who in enumerate(["Tim Follin", "Martin Galway", "Allister Brimble",
                                     "Matt Furniss"]) for j in range(2)]
    # an album an earlier pass stamped under an older folder name
    rows += [_mod("x1", "/music/Chip/Mr Tickle/a.mod", "Xeron", album="Tickle", src="folder"),
             _mod("x2", "/music/Chip/Mr Tickle/b.mod", "Xeron", album="Some Demo", src="songdb")]
    rows += [_mod(f"g{i}{j}", f"/music/Chip/{g}/t{j}.mod", "Chris Huelsbeck", album=g, src="modland")
             for i, g in enumerate(["Turrican", "Katakis", "Apidya", "Lionheart"]) for j in range(2)]
    store.upsert_tracks_batch(rows)
    await fa.apply_folder_albums(force=True)
    x1 = store.get_track("x1")
    assert (x1["album"], x1.get("game_by_folder")) == ("", None)
    assert store.get_track("x2").get("game_by_folder") is None
    assert store._candidate_ids(game="mr tickle") == set()
    assert store._candidate_ids(game="tickle") == set()


async def test_title_tunes_in_archive_folders_and_amiga_names(store, monkeypatch):
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_tracks_batch([
        _mod("a1", "/music/Games/Pack.zip::Turrican/turrican.mod", "Chris Huelsbeck",
             title="Turrican"),
        _mod("a2", "/music/Games/Pack.zip::Turrican/level1.mod", "Chris Huelsbeck"),
        {**_mod("m1", "/music/Games/Apidya/mdat.apidya", "Chris Huelsbeck", title="mdat.apidya"),
         "format": "TFMX"},
        {**_mod("m2", "/music/Games/Apidya/mdat.level1", "Chris Huelsbeck", title="mdat.level1"),
         "format": "TFMX"},
        # alone in its folder, a tune named after it stays album-less
        {**_mod("k1", "/music/Games/Katakis/mdat.katakis", "Chris Huelsbeck",
                title="mdat.katakis"), "format": "TFMX"},
    ])
    await fa.apply_folder_albums(force=True)
    assert {tid: store.get_track(tid)["album"] for tid in ("a1", "a2", "m1", "m2", "k1")} == {
        "a1": "Turrican", "a2": "Turrican", "m1": "Apidya", "m2": "Apidya", "k1": ""}


async def test_a_users_album_gets_the_folder_name_as_an_alias(store, monkeypatch):
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_tracks_batch([
        {**_mod("u1", "/music/Games/Turrican/level1.mod", "Chris Huelsbeck", album="My Turrican"),
         "user_edited": ["album"]},
        _mod("u2", "/music/Games/Turrican/level2.mod", "Chris Huelsbeck"),
    ])
    await fa.apply_folder_albums(force=True)
    u1 = store.get_track("u1")
    assert (u1["album"], u1["game"], u1["game_by_folder"], u1["game_aliases"]) == (
        "My Turrican", "My Turrican", "Turrican", ["Turrican"])


async def test_a_refused_folder_inside_an_archive_names_none_of_its_tracks(store, monkeypatch):
    fa = _folder_pass_env(store, monkeypatch)
    rows = [_mod(f"p{i}{j}", f"/music/Chip.zip::{who}/tune{j}.mod", who)
            for i, who in enumerate(["Jogeir Liljedahl", "Martin Galway", "Allister Brimble",
                                     "Matt Furniss"]) for j in range(2)]
    rows += [_mod("x1", "/music/Chip.zip::Mr Tickle/a.mod", "Xeron"),
             _mod("x2", "/music/Chip.zip::Mr Tickle/b.mod", "Xeron", album="Some Demo",
                  src="songdb")]
    rows += [_mod(f"g{i}{j}", f"/music/Chip.zip::{g}/t{j}.mod", "Chris Huelsbeck", album=g,
                  src="modland")
             for i, g in enumerate(["Turrican", "Katakis", "Apidya", "Lionheart"]) for j in range(2)]
    store.upsert_tracks_batch(rows)
    await fa.apply_folder_albums(force=True)
    assert store.get_track("x2").get("game_by_folder") is None
    assert store.get_track("g00")["game_by_folder"] == "Turrican"


@pytest.mark.parametrize("text", [
    "Štúr", "Riku Ö", "Ø. Jergan", "À bout de souffle", "Café\xa0Noir", "After Dark –prologue-",
    "Škoda", "Dušan Maleš", "Œuvre", "L’Été", "“Live”", "Rock ‘n’ Roll", "ŠKODA", "Chrono Trigger™",
    "€5 Bonus", "Don’t Stop", "Game – Title", "Frédéric", "Bjørn Lynne", "© 1990 Konami", "Zoë",
    "french cookies and milk [poksti] - oµµS"])
def test_western_psf_tags(text):
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=Opening\ngame=" + text.encode("cp1252") + b"\nartist=Someone\n"
    assert _psf_tag_text(blob).split("game=")[1].split("\n")[0] == text


@pytest.mark.parametrize("text", [
    "Official髭男dism", "R-TYPE外伝", "(梓Ver.)", "Come with Me!! (澪Ver.)", "Deinei - 泥濘",
    "02. 天樂", "サガ", "ガイア外伝", "海", "格闘", "カオス", "ガイア (1995)", "ロックマンX", "ギガWing",
    "序曲", "外伝", "斑鳩", "田中", "天", "Kizuna - 姉弟", "光", "NO-口", "試練END",
    "03. RADWIMPS - 05410-(ん)"])
def test_japanese_psf_tags(text):
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=Opening\ngame=" + text.encode("cp932") + b"\nartist=Someone\n"
    assert _psf_tag_text(blob).split("game=")[1].split("\n")[0] == text


def test_a_reading_with_user_defined_characters_is_no_japanese():
    """cp932 reads bytes 0xF0–0xF9 as user-defined characters, which no
    Japanese tag holds: even a poor Western reading beats that."""
    from soniqboom.core.metadata import _psf_tag_text
    assert _psf_tag_text(b"game=\xf1\xa6\n") == "game=ñ¦\n"


# ── QA round 15 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "ﾛｯｸﾏﾝ", "ﾛｯｸﾏﾝ2 ﾜｲﾘｰ", "ｸﾞﾗﾃﾞｨｳｽ", "ﾀｲﾄﾙ", "ｵｰﾌﾟﾆﾝｸﾞ", "ｴﾝﾃﾞｨﾝｸﾞ", "ｹﾞｰﾑｵｰﾊﾞｰ",
    "BGM ｽﾃｰｼﾞ1", "ｽﾃｰｼﾞ 1", "ｽﾄﾘｰﾄﾌｧｲﾀｰII", "MORE･･･", "R･I･O･T", "澤野 弘之･和田 貴史",
    "廃校ｷﾀ――(ﾟ∀ﾟ)――!!", "ANNIE LAURIE - アニー･ローリー -"])
def test_half_width_katakana_psf_tags(text):
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=" + text.encode("cp932") + b"\ngame=X\n"
    assert _psf_tag_text(blob).split("title=")[1].split("\n")[0] == text


@pytest.mark.parametrize("text", ["Castles II (CD³²)", "·° FIELDS OF HONOR °·", "°°°°°°°°",
                                  "ÇÇÇÇÇÇÇÇ", "¥500"])
def test_western_symbols_are_no_half_width_katakana(text):
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=" + text.encode("cp1252") + b"\ngame=X\n"
    assert _psf_tag_text(blob).split("title=")[1].split("\n")[0] == text


async def test_a_module_wrapped_with_its_copies_is_still_wrapped(store, monkeypatch):
    """A second version of the wrapped module, or a non-retro copy, doesn't
    make its archive a release of several tunes."""
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_tracks_batch([
        _mod("l1", "/music/Games/Lotus/lotus.zip::lotus.mod", "Barry Leitch", title="lotus"),
        {**_mod("l2", "/music/Games/Lotus/lotus.zip::Lotus Remix.mp3", "Barry Leitch"),
         "format": "MP3"},
        _mod("l3", "/music/Games/Lotus/ingame.mod", "Barry Leitch"),
        _mod("a1", "/music/Games/Apidya/apidya.zip::apidya.mod", "Chris Huelsbeck", title="apidya"),
        {**_mod("a2", "/music/Games/Apidya/apidya.zip::apidya.xm", "Chris Huelsbeck",
                title="apidya"), "format": "FastTracker 2"},
        _mod("a3", "/music/Games/Apidya/ingame.mod", "Chris Huelsbeck"),
    ])
    await fa.apply_folder_albums(force=True)
    assert {t: store.get_track(t)["album"] for t in ("l1", "l3", "a1", "a2", "a3")} == {
        "l1": "Lotus", "l3": "Lotus", "a1": "Apidya", "a2": "Apidya", "a3": "Apidya"}


def test_several_tunes_of_a_nested_archive_are_counted_in_it():
    from soniqboom.core import folder_album as fa
    # two versions of one module in a nested archive: still its wrapper
    assert fa.multi_member_archives([
        _mod("v1", "/music/Pack.zip::lotus.zip::lotus.mod", "Barry Leitch"),
        {**_mod("v2", "/music/Pack.zip::lotus.zip::lotus.xm", "Barry Leitch"),
         "format": "FastTracker 2"}]) == frozenset()
    tracks = [_mod("n1", "/music/Pack.zip::Turrican.lha::turrican.mod", "Chris Huelsbeck",
                   title="Turrican"),
              _mod("n2", "/music/Pack.zip::Turrican.lha::level1.mod", "Chris Huelsbeck")]
    assert fa.multi_member_archives(tracks) == {"/music/Pack.zip::Turrican.lha"}
    out = fa.collect_folder_updates(tracks, roots=frozenset({"/music"}), format_keys=frozenset(),
                                    author_keys=frozenset(), artist_keys=frozenset())
    assert {tid: upd["album"] for tid, upd, _e in out} == {"n1": "Turrican", "n2": "Turrican"}


def test_a_game_the_user_cleared_is_not_matched_by_title(store):
    store.upsert_tracks_batch([_t("s", format="SID", title="Commando", album="SIDs",
                                  album_source="folder")])
    assert store._candidate_ids(game="commando") == {"s"}
    store.update_track_fields("s", {"game": "", "user_edited": ["game"]})
    assert store._candidate_ids(game="commando") == set()


def test_a_modland_file_name_game_is_a_guess_too(store):
    store.upsert_tracks_batch([_t("a", format="SNDH", title="Sidewinder", album="Arcade",
                                  album_source="modland-filename")])
    assert store._candidate_ids(game="sidewinder") == {"a"}


async def test_the_pass_counts_tracks_not_patches(store, monkeypatch):
    """A track whose album goes back to its header name and whose folder name
    changes gets two patches — and counts once."""
    fa = _folder_pass_env(store, monkeypatch)
    cut, full = "Street Fighter II - The World Wa", "Street Fighter II - The World Warrior"
    rows = [{**_mod("sf", f"/music/SNES/{full}/sf2-01.spc", "Yoko Shimomura", album="Old Name",
                    src="folder", title="Opening"), "format": "SPC", "game_by_tag": cut}]
    rows += [_mod(f"p{i}", f"/music/SNES/{who}/tune.mod", who)
             for i, who in enumerate(["Jogeir Liljedahl", "Martin Galway", "Allister Brimble"])]
    rows += [{**_mod(f"g{i}", f"/music/SNES/{g}/01.spc", "Someone", album=g, src="tag",
                     title="Title"), "format": "SPC", "game_by_tag": g}
             for i, g in enumerate(["Chrono Trigger", "Secret of Mana", "Super Metroid",
                                    "F-Zero", "Pilotwings"])]
    store.upsert_tracks_batch(rows)
    before = {t["id"]: dict(t) for t in store.all_tracks()}
    res = await fa.apply_folder_albums(force=True)
    changed = {tid for tid, t in before.items() if store.get_track(tid) != t}
    assert store.get_track("sf")["game_by_folder"] == full
    assert res["updated"] == len(changed)


def _random_library(rnd) -> list[dict]:
    """Stamped folder albums (some over a header name), composer indexes,
    archives, other sources' albums — the mix the settle loop and the
    refused / accepted rules meet."""
    people = ["Chris Huelsbeck", "Barry Leitch", "Jogeir Liljedahl", "Martin Galway",
              "Allister Brimble", "Matt Furniss", "Xeron", "Funky Fish", "Tim Follin"]
    games = ["Turrican", "Katakis", "Apidya", "Lionheart", "Enigma", "Mr Tickle", "Big Demo",
             "weird", "misc", "unsorted", "happy", "Tunes"]
    rows, n = [], 0
    for pi in range(rnd.randint(1, 3)):
        parent = rnd.choice(["/music/Chip", f"/music/Idx{pi}", f"/music/Pack{pi}.zip::",
                             "/music/Games"])
        for g in rnd.sample(people + games, rnd.randint(3, 8)):
            base = (parent + g) if parent.endswith("::") else f"{parent}/{g}"
            if rnd.random() < .25:
                base += ".zip::"
            for j in range(rnd.randint(1, 3)):
                n += 1
                who = g if (g in people and rnd.random() < .8) else rnd.choice(people + [""])
                title = g if rnd.random() < .2 else f"tune {j}"
                fname = (g.lower() if title == g else f"tune{j}") + ".mod"
                r = _mod(f"t{n:03}", base + ("" if base.endswith("::") else "/") + fname, who,
                         title=title)
                k = rnd.random()
                if k < .45:
                    r.update(album=rnd.choice(games + [g]), album_source="folder")
                    if rnd.random() < .6:
                        r.update(format="SPC", game_by_tag=rnd.choice(["Header " + g, "Hdr X", g]))
                elif k < .55:
                    r.update(album=rnd.choice(games),
                             album_source=rnd.choice(["modland", "songdb", "tag"]))
                rows.append(r)
    return rows


async def test_random_libraries_settle_in_one_pass(monkeypatch):
    """Whatever the mix: a second pass writes nothing, and the result does
    not depend on the collect's chunk size."""
    import copy
    import random
    from soniqboom.core import folder_album as fa
    fields = ("album", "album_source", "game_by_folder", "game")

    async def run(rows):
        s = TrackStore()
        monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
        fa._last_seq = None
        _folder_pass_env(s, monkeypatch)
        s.upsert_tracks_batch(copy.deepcopy(rows))
        await fa.apply_folder_albums(force=True)
        return s

    for seed in range(120):
        rows = _random_library(random.Random(seed))
        s = await run(rows)
        assert (await fa.apply_folder_albums(force=True))["updated"] == 0, seed
        monkeypatch.setattr(fa, "_SCAN_CHUNK", 1)
        s1 = await run(rows)
        monkeypatch.setattr(fa, "_SCAN_CHUNK", 500)
        assert ({t["id"]: [t.get(f) for f in fields] for t in s.all_tracks()}
                == {t["id"]: [t.get(f) for f in fields] for t in s1.all_tracks()}), seed


# Generated libraries (a random-library differential found them) where the
# refused-folder rule meets nested archives holding several tunes: the key
# a folder is refused under and the key it is looked up by must agree.
_NESTED_REFUSED_LIBS = [
    ([  # (id, path, artist, title, album, album_source, user_edited, format, game_by_tag)
        ("t001", "/music/Games/Outer.lha::Xeron.zip::xeron.mod", "Tim Follin", "Xeron", "", None, None, "ProTracker", None),
        ("t002", "/music/Games/Outer.lha::Xeron.zip::tune1.mod", "Allister Brimble", "tune 1", "Tunes", "folder", None, "SPC", "Tunes"),
        ("t003", "/music/Games/Outer.lha::Martin Galway.zip::martin galway.mod", "Chris Huelsbeck", "Martin Galway", "Mine", None, ["album"], "ProTracker", None),
        ("t004", "/music/Games/Outer.lha::Martin Galway.zip::tune1.mod", "Matt Furniss", "tune 1", "", None, None, "ProTracker", None),
        ("t006", "/music/Chip/weird.zip::weird.mod", "Matt Furniss", "weird", "", None, None, "ProTracker", None),
        ("t007", "/music/Chip/weird.zip::tune1.mod", "Jogeir Liljedahl", "tune 1", "", None, None, "ProTracker", None),
        ("t009", "/music/Pack.zip::Allister Brimble/allister brimble.mod", "Xeron", "Allister Brimble", "", None, None, "SPC", "Tunes"),
        ("t010", "/music/Pack.zip::Tim Follin/tim follin.mod", "Chris Huelsbeck", "Tim Follin", "Mine", None, ["album"], "SPC", "Apidya"),
        ("t014", "/music/Chip/Jogeir Liljedahl/jogeir liljedahl.mod", "Barry Leitch", "Jogeir Liljedahl", "", None, None, "ProTracker", None),
        ("t015", "/music/Chip/Jogeir Liljedahl/tune1.mod", "Tim Follin", "tune 1", "", None, None, "ProTracker", None),
        ("t016", "/music/Chip/Jogeir Liljedahl/tune2.mod", "", "tune 2", "Mine", None, ["album"], "SPC", "Turrican"),
    ], "t002"),
    ([
        ("t001", "/music/Games/Matt Furniss/tune0.mod", "Chris Huelsbeck", "tune 0", "Mine", None, ["album"], "ProTracker", None),
        ("t002", "/music/Games/Matt Furniss/matt furniss.mod", "Chris Huelsbeck", "Matt Furniss", "", None, None, "ProTracker", None),
        ("t003", "/music/Games/Matt Furniss/tune2.mod", "Chris Huelsbeck", "tune 2", "", None, None, "ProTracker", None),
        ("t004", "/music/Games/Matt Furniss/tune3.mod", "Jogeir Liljedahl", "tune 3", "", None, None, "SPC", "Lotus"),
        ("t005", "/music/Chip/Big Demo/tune0.mod", "Martin Galway", "tune 0", "Big Demo", "folder", None, "ProTracker", None),
        ("t006", "/music/Chip/Big Demo/big demo.mod", "Jogeir Liljedahl", "Big Demo", "Mr Tickle", "modland", None, "SPC", "weird"),
        ("t007", "/music/Chip/Big Demo/tune2.mod", "Martin Galway", "tune 2", "", None, None, "ProTracker", None),
        ("t008", "/music/Chip/Jogeir Liljedahl/tune0.mod", "", "tune 0", "", None, None, "ProTracker", None),
        ("t009", "/music/Games/Outer.lha::Katakis.zip::katakis.mod", "Chris Huelsbeck", "Katakis", "Katakis", "tag", None, "ProTracker", None),
        ("t010", "/music/Games/Outer.lha::Katakis.zip::tune1.mod", "Matt Furniss", "tune 1", "Turrican", "folder", None, "ProTracker", None),
        ("t011", "/music/Games/Outer.lha::Xeron/tune0.mod", "Xeron", "tune 0", "happy", "folder", None, "ProTracker", None),
        ("t012", "/music/Games/Outer.lha::Xeron/tune1.mod", "Chris Huelsbeck", "tune 1", "", None, None, "ProTracker", None),
        ("t013", "/music/Games/Outer.lha::Allister Brimble.zip::tune0.mod.zip::tune0.mod", "Jogeir Liljedahl", "tune 0", "", None, None, "ProTracker", None),
        ("t014", "/music/Games/Outer.lha::Allister Brimble.zip::tune1.mod", "Allister Brimble", "tune 1", "weird", "folder", None, "ProTracker", None),
        ("t015", "/music/Games/Outer.lha::Allister Brimble.zip::allister brimble.mod", "Barry Leitch", "Allister Brimble", "Tunes", "modland", None, "ProTracker", None),
        ("t016", "/music/Games/Outer.lha::Allister Brimble.zip::tune3.mod", "", "tune 3", "Lotus", "tag", None, "SPC", "Big Demo"),
        ("t017", "/music/Pack.zip::music.zip::music.mod", "Chris Huelsbeck", "music", "", None, None, "ProTracker", None),
        ("t018", "/music/Pack.zip::music.zip::tune1.mod", "Martin Galway", "tune 1", "Apidya", "modland", None, "ProTracker", None),
        ("t019", "/music/Pack.zip::music.zip::tune2.mod", "", "tune 2", "", None, None, "ProTracker", None),
        ("t020", "/music/Pack.zip::music.zip::tune3.mod", "music", "tune 3", "", None, None, "ProTracker", None),
        ("t021", "/music/Chip/Katakis/katakis.mod", "", "Katakis", "", None, None, "ProTracker", None),
    ], "t015"),
]


@pytest.mark.parametrize("rows,tid", _NESTED_REFUSED_LIBS)
async def test_a_refused_folder_in_a_nested_archive_names_nothing(store, monkeypatch, rows, tid):
    fa = _folder_pass_env(store, monkeypatch)
    lib = []
    for i, path, artist, title, album, src, ue, fmt, gbt in rows:
        r = {**_mod(i, path, artist, album=album, src=src, title=title), "format": fmt}
        if ue:
            r["user_edited"] = ue
        if gbt:
            r["game_by_tag"] = gbt
        lib.append(r)
    store.upsert_tracks_batch(lib)
    await fa.apply_folder_albums(force=True)
    assert store.get_track(tid).get("game_by_folder") is None


# ── QA round 16 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "ｼﾞ", "ﾃﾞ", "ｼｮ", "ｹｰｷ", "ﾒｰﾙ", "ﾕﾒ", "ｿﾗ", "Stage ｲﾁ", "ﾏﾘｵ", "ﾎﾞｽ ｾﾝ", "眩", "皓"])
def test_short_japanese_psf_tags(text):
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=Opening\ngame=" + text.encode("cp932") + b"\nartist=Someone\n"
    assert _psf_tag_text(blob).split("game=")[1].split("\n")[0] == text


@pytest.mark.parametrize("text", [
    "GRÜßE AUS BERLIN", "GRÖßE", "MÄßIG", "·°· Intro ·°·", ".·°·.", "°º¤", "°±°", "©®", "Game©®",
    "»Intro«", "« Intro »", "¥500", "500¥", "¥ 500", "CAFÉ¹", "Dann Schließ´ Ich Meine Augen",
    "Îñ±éñÐº¬"])
def test_western_symbol_clusters_stay_western(text):
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=Opening\ngame=" + text.encode("cp1252") + b"\nartist=Someone\n"
    assert _psf_tag_text(blob).split("game=")[1].split("\n")[0] == text


def test_a_utf8_tag_is_read_as_such():
    from soniqboom.core.metadata import _psf_tag_text
    for text in ("Pokémon", "ロックマン", "Ωmega", "Москва", "е́"):
        assert _psf_tag_text(("game=" + text + "\n").encode("utf-8")) == "game=" + text + "\n"
    # utf8=1 is trusted even for an unlikely script
    blob = "utf8=1\ngame=ߴ\n".encode("utf-8")
    assert _psf_tag_text(blob) == "utf8=1\ngame=ߴ\n"


async def test_wrapped_modules_named_like_their_archive(store, monkeypatch):
    """A single-module archive is skipped for the folder around it however the
    member spells the archive's name — without its extension, Amiga-style,
    in a generic folder, or inside a .tar.gz."""
    fa = _folder_pass_env(store, monkeypatch)
    g = "/music/Games"
    store.upsert_tracks_batch([
        _mod("g1", f"{g}/Gold of the Aztecs/Gold of the Aztecs.zip::gold_of_the_aztecs.mod", "Dave Lowe",
             title="gold of the aztecs"),
        {**_mod("g2", f"{g}/Gold of the Aztecs/Gold of the Aztecs.zip::gold_of_the_aztecs.xm", "Dave Lowe",
                title="gold of the aztecs"), "format": "FastTracker 2"},
        _mod("g3", f"{g}/Gold of the Aztecs/ingame.mod", "Dave Lowe"),
        _mod("l1", f"{g}/Lotus Turbo/lotus.mod.tar.gz::lotus.mod", "Barry Leitch", title="lotus"),
        _mod("l2", f"{g}/Lotus Turbo/ingame.mod", "Barry Leitch"),
        _mod("p1", f"{g}/Pinball Dreams/pinball.lha::mod.pinball", "Olof Gustafsson", title="pinball"),
        _mod("p2", f"{g}/Pinball Dreams/ingame.mod", "Olof Gustafsson"),
        _mod("s1", f"{g}/Stunt Car/stunt.zip::music/stunt.mod", "Ben Daglish", title="stunt"),
        {**_mod("s2", f"{g}/Stunt Car/stunt.zip::music/stunt.xm", "Ben Daglish", title="stunt"),
         "format": "FastTracker 2"},
        _mod("s3", f"{g}/Stunt Car/ingame.mod", "Ben Daglish"),
    ])
    await fa.apply_folder_albums(force=True)
    assert {t["id"]: t["album"] for t in store.all_tracks()} == {
        "g1": "Gold of the Aztecs", "g2": "Gold of the Aztecs", "g3": "Gold of the Aztecs",
        "l1": "Lotus Turbo", "l2": "Lotus Turbo", "p1": "Pinball Dreams", "p2": "Pinball Dreams",
        "s1": "Stunt Car", "s2": "Stunt Car", "s3": "Stunt Car"}


# ── QA round 17 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Λsʜᴇs", "ətˈæk 0N tάɪtn", "Aɴɢᴇʟ", "לילה טוב", "لعبة", "Əfsanə", "Ɔdɔ", "ʃ", "Москва", "Ωmega",
    "Tiếng Việt", "Pokémon", "Straße ß", "REVIVƎЯ", "30°С", "Tetris «Т»", "Кино·Группа", "Hawaiʻi",
    "Garc\u0327on", "S\u030ckoda", "O\u0304kami", "Dvor\u030ca\u0301k", "ku\u0304t\u0323astha jiva",
    "Part ב",                                        # a lone letter nothing reads better
    "α=\u0338a (feat. Mika Kobayashi)", "A =\u0338 B"])   # a decomposed "≠" is "=" + a mark
def test_genuine_utf8_psf_tags_without_the_flag(text):
    from soniqboom.core.metadata import _psf_tag_text
    blob = ("title=Opening\ngame=" + text + "\nartist=Someone\n").encode("utf-8")
    assert _psf_tag_text(blob).split("game=")[1].split("\n")[0] == text


@pytest.mark.parametrize("text,enc", [
    ("Â’", "cp1252"), ("Ü™", "cp1252"), ("Ö°", "cp1252"),          # valid UTF-8 for C1 / Syriac / a Hebrew point
    ("Â†", "cp1252"),                                              # a C1 control even when legacy scores low
    ("ﾕｷ", "cp932"),                                               # valid UTF-8 for a lone Armenian letter
    ("ﾌｸﾛｳ", "cp932"),                                             # … for a mark on no letter
    ("恋ｽﾙVOC@LOID", "cp932"), ("EDﾒﾛ (ｴﾌｪｸﾄVer.)", "cp932")])
def test_legacy_tags_that_happen_to_be_valid_utf8(text, enc):
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=Opening\ngame=" + text.encode(enc) + b"\nartist=Someone\n"
    assert _psf_tag_text(blob).split("game=")[1].split("\n")[0] == text


def test_a_member_in_a_named_folder_is_another_tune():
    from soniqboom.core import folder_album as fa
    assert fa.multi_member_archives([
        _mod("a", "/m/lotus.zip::lotus.mod", "Barry Leitch"),
        {**_mod("b", "/m/lotus.zip::Lotus II/lotus.xm", "Barry Leitch"), "format": "FastTracker 2"},
    ]) == {"/m/lotus.zip"}


def test_one_odd_field_does_not_redecode_the_whole_tag_block():
    from soniqboom.core.metadata import _psf_tag_text
    text = "title=Ωж\ngame=Pokémon\nartist=Frédéric Motte\ncopyright=Garc\u0327on\n"
    assert _psf_tag_text(text.encode("utf-8")) == text


# ── QA round 18 ──────────────────────────────────────────────────────────────

# ── QA round 19 ──────────────────────────────────────────────────────────────

async def test_a_category_folder_is_no_game_alias(store, monkeypatch):
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_tracks_batch([
        {**_mod("v1", "/music/Arcade/Area 88/01.vgz", "Toshio Kajino", album="Area 88", src="tag"),
         "format": "VGM", "game_by_tag": "Area 88"},
        _mod("v2", "/music/Arcade/Rainbow Islands/ri.mod", "Someone"),
    ])
    await fa.apply_folder_albums(force=True)
    assert store.get_track("v1").get("game_aliases") is None
    assert store._candidate_ids(game="arcade") == set()


@pytest.mark.parametrize("text,enc", [("ﾃｽﾂ　", "cp932"), ("Ã¼berÂ†", "cp1252"), ("x\u0092y", "utf-8")])
def test_a_c1_control_anywhere_is_legacy(text, enc):
    from soniqboom.core.metadata import _psf_tag_text, _unlikely_utf8
    blob = ("game=" + text + "\n").encode(enc)
    if enc == "utf-8":
        assert _unlikely_utf8(text)
    else:
        assert _psf_tag_text(blob) == "game=" + text + "\n"


def test_a_lone_cyrillic_letter_is_real_utf8():
    from soniqboom.core.metadata import _psf_tag_text
    assert _psf_tag_text("game=Mr Ж\n".encode("utf-8")) == "game=Mr Ж\n"


# ── QA round 20 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("part", ["Lvl 3", "Act 2", "Level-1", "Stage_2", "Menus", "Loaders",
                                  "Bosses", "Jingles", "Credits", "SFX", "Sound FX", "Hi-Scores",
                                  "Ending", "Title Screen", "In Game", "Ingame 2", "Title 2",
                                  "Boss 1", "Menu 1", "Loader 2", "Jingle 3", "Game Over 2",
                                  "BGM01", "Ingame 1b", "Level 1a", "Level 1-A", "Levels", "Stages"])
def test_part_names(part):
    from soniqboom.core import folder_album as fa
    assert fa._part_name(part)


@pytest.mark.parametrize("name", ["Zone 66", "Area 88", "Mission 1", "Round 42", "Intros",
                                  "Outros", "Bossa Nova", "Endurance", "Titles of Fame", "Menu Maker",
                                  "Level Up", "Levels of Fear", "Title Fight", "Intro 1999",
                                  "Stage Fright", "Game Overdrive", "Level", "Stage"])
def test_names_that_are_no_parts(name):
    from soniqboom.core import folder_album as fa
    assert not fa._part_name(name)


@pytest.mark.parametrize("text", ["Z0\u0338NE", "1\u03369\u03369\u03361\u0336", "2\u03320\u03320\u03320\u0332 Remix",
                                  "Ngu\u031bo\u031b\u0300i", "a\u0301\u0302\u0303"])
def test_marks_on_digits_and_stacked_marks_are_real_utf8(text):
    from soniqboom.core.metadata import _psf_tag_text
    assert _psf_tag_text(("game=" + text + "\n").encode("utf-8")) == "game=" + text + "\n"


def test_a_stray_mark_is_found_anywhere_in_the_block():
    from soniqboom.core.metadata import _psf_tag_text
    for text in ("artist=ﾃｽ\ngame=ﾌｸﾛｳ\n", "game=ﾌｸﾛｳ\nartist=ﾃｽ\n",
                 "title=Test\nﾌｱ\n"):              # a line starting with a mark
        assert _psf_tag_text(text.encode("cp932")) == text


@pytest.mark.parametrize("text", ["Mr ﾌｱ", "(ﾌｱ)", "$ﾌｱ", "^ﾌｱ"])
def test_a_mark_after_a_space_bracket_or_non_math_symbol_is_stray(text):
    """"ﾌｱ" is valid UTF-8 for a combining mark (U+0331) — on a space, a
    bracket, "$" or "^" it marks nothing, so the bytes are Shift-JIS."""
    from soniqboom.core.metadata import _psf_tag_text
    blob = b"title=Opening\ngame=" + text.encode("cp932") + b"\nartist=Someone\n"
    assert _psf_tag_text(blob).split("game=")[1].split("\n")[0] == text


def test_an_archive_is_several_tunes_whatever_its_members_order():
    from soniqboom.core import folder_album as fa
    a = _mod("a", "/m/G/Turrican.lha::level1.mod", "Chris Huelsbeck")
    b = _mod("b", "/m/G/Turrican.lha::turrican.mod", "Chris Huelsbeck")
    assert fa.multi_member_archives([a, b]) == fa.multi_member_archives([b, a]) == {"/m/G/Turrican.lha"}



# ── part folders (QA rounds 17–21): refused, never given way ────────────────

async def test_part_named_folders_are_no_album(store, monkeypatch):
    """A folder or archive named like a part of a game's music is no album
    — never the name of the folder above it (a category's, a composer's) —
    unless it sits in a release index, where the name is a game."""
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_scan_dir("ftp://h/Games")
    store.upsert_tracks_batch([
        _mod("p1", "/music/Games/Turrican/Level 1/a.mod", "Chris Huelsbeck"),
        _mod("p2", "/music/Games/Turrican/Level 2/b.mod", "Chris Huelsbeck"),
        _mod("p3", "/music/Games/Apidya.lha::Ingame/w1.mod", "Chris Huelsbeck"),
        _mod("p4", "/music/Games/Lotus/Level 1.lha::l1.mod", "Barry Leitch"),
        _mod("p5", "/music/Shmupz.zip::Game Over/go.mod", "Snatcho"),
        _mod("p6", "/music/Arcade/Game Over/go.mod", "Snatcho"),
        _mod("p7", "/music/Arcade/Rainbow Islands/ri.mod", "Someone"),
        _mod("p8", "/music/Dinamic/Game Over/go.mod", "Snatcho"),
        _mod("p9", "/music/Games/Game Over/Ingame/i.mod", "Snatcho"),
        _mod("p10", "/music/Games/Turrikan Soundtrack/Ingame/i.mod", "Someone"),
        _mod("p11", "/music/Games/Snorkel.lha::music/Level 1/a.mod", "Someone"),
        # real games named like parts, in a release index
        _mod("g1", "/music/Games/Game Over/theme.mod", "Snatcho"),
        _mod("g2", "/music/Games/G/Boss/boss-title.mod", "Someone"),
        _mod("g3", "/music/Games.zip::Game Over/go2.mod", "Snatcho"),
        _mod("g4", "ftp://h/Games:/Game Over/go3.mod", "Snatcho"),
        # no parts at all
        _mod("n1", "/music/Games/Zone 66/z.mod", "Purple Motion"),
        _mod("n2", "/music/Games/Bossa Nova/samba.mod", "Someone"),
    ])
    await fa.apply_folder_albums(force=True)
    got = {t["id"]: t["album"] for t in store.all_tracks()}
    assert got == {
        "p1": "", "p2": "", "p3": "", "p4": "", "p5": "", "p6": "", "p7": "Rainbow Islands",
        "p8": "", "p9": "", "p10": "", "p11": "",
        "g1": "Game Over", "g2": "Boss", "g3": "Game Over", "g4": "Game Over",
        "n1": "Zone 66", "n2": "Bossa Nova"}
    assert store._candidate_ids(game="shmupz") == set()
    assert store._candidate_ids(game="arcade") == set()


async def test_part_named_folders_by_scan_root_spelling_and_release_pack(store, monkeypatch):
    """The scan root's name counts only when the folders walked reach it
    (nested roots: the innermost); names are judged cleaned ("Game_Over",
    "Game_Rips"); and a part folder in a release archive (a compo pack of
    category folders) doesn't take the archive's name."""
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_scan_dir("/music/Games")
    store.upsert_tracks_batch([
        _mod("r1", "/music/Games/Game Over/go.mod", "Snatcho"),
        _mod("r2", "/music/Games/Zorblax/Music/Mods/Game Over/x.mod", "Snatcho"),
        _mod("r3", "/music/Arcade/Game_Over/go.mod", "Snatcho"),
        _mod("r4", "/music/Arcade/In_Game/ig.mod", "Snatcho"),
        _mod("r5", "/music/Game_Rips/Game Over/go.mod", "Snatcho"),
        _mod("k1", "/music/compos/bcompo5.zip::weird/a.xm", "Funky Fish"),
        _mod("k2", "/music/compos/bcompo5.zip::weird/b.it", "Vibe"),
        _mod("k3", "/music/compos/bcompo5.zip::happy/c.xm", "Funky Fish"),
        _mod("k4", "/music/compos/bcompo5.zip::happy/d.xm", "Dodging Liquid"),
        _mod("k5", "/music/compos/bcompo5.zip::remix/r.mod", "Cerror"),
        _mod("k6", "/music/compos/bcompo5.zip::Xeron/x.mod", "Xeron"),
        _mod("k7", "/music/compos/bcompo5.zip::Game Over/g.mod", "Snatcho"),
    ])
    await fa.apply_folder_albums(force=True)
    got = {t["id"]: t["album"] for t in store.all_tracks()}
    assert got == {"r1": "Game Over", "r2": "", "r3": "", "r4": "", "r5": "Game Over",
                   "k1": "bcompo5", "k2": "bcompo5", "k3": "bcompo5", "k4": "bcompo5",
                   "k5": "bcompo5", "k6": "bcompo5", "k7": ""}


async def test_a_part_named_archive_gives_its_members_no_name(store, monkeypatch):
    """Members in composer folders of a part-named archive don't take the
    archive's name (a release archive's) — unless it sits in a release
    index, where the name is a game; another archive still names them."""
    fa = _folder_pass_env(store, monkeypatch)
    rows = []
    for tag, base in (("part", "/music/Games/Zorblax/Ingame.lha::"),
                      ("lvl", "/music/Games/Zorblax/Level 1a.zip::"),
                      ("idx", "/music/Games/Ingame.lha::"),
                      ("ctrl", "/music/Games/Qwerty Pack.lha::")):
        rows += [_mod(f"{tag}{i}", base + who + "/x.mod", who)
                 for i, who in enumerate(("Rob Hubbart", "Ben Dagliss", "Tim Follin"))]
    store.upsert_tracks_batch(rows)
    await fa.apply_folder_albums(force=True)
    got = {t["id"]: (t["album"], t.get("game_by_folder")) for t in store.all_tracks()}
    for i in range(3):
        assert got[f"part{i}"] == got[f"lvl{i}"] == ("", None)
        assert got[f"idx{i}"] == ("Ingame", "Ingame")
        assert got[f"ctrl{i}"] == ("Qwerty Pack", "Qwerty Pack")
    assert store._candidate_ids(game="ingame") == {"idx0", "idx1", "idx2"}


async def test_a_part_named_folder_gives_no_folder_game_name(store, monkeypatch):
    fa = _folder_pass_env(store, monkeypatch)
    store.upsert_tracks_batch([
        {**_mod("v1", "/music/Arcade/Level 1/01.vgz", "Someone", album="Area 88", src="tag"),
         "format": "VGM", "game_by_tag": "Area 88"},
        _mod("v2", "/music/Arcade/Rainbow Islands/ri.mod", "Someone"),
    ])
    await fa.apply_folder_albums(force=True)
    v1 = store.get_track("v1")
    assert (v1.get("game_by_folder"), v1.get("game_aliases")) == (None, None)


def test_part_names_are_no_generic_words():
    """Judged by where they sit — never a plain container word (the Subsonic
    folder index names folders by ``_generic`` too)."""
    from soniqboom.core import folder_album as fa
    for name in ("Title", "Game Over", "Level 1", "Boss"):
        assert not fa._generic(name) and fa._part_name(name)
    assert not fa._part_album_refused("Game Over", ["G", "Games"])
    assert fa._part_album_refused("Game Over", ["Misc", "Collection"])
    assert fa._part_album_refused("Ingame", ["Game Over", "Games"])
