# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""UADE song-database enrichment (core/songdb.py, audacious-uade-tools data).

* the index build: joined names, placeholder authors dropped, validated years,
  a shared production name qualified by its group, implausible downloads
  refused (the current index is kept);
* the apply: exact matches fill EMPTY fields only (a guessed album is
  upgraded), never a user edit; a refreshed index updates / withdraws what it
  filled; the patch is computed at write time (an edit landing meanwhile wins);
* Reset withdraws exactly the fills (lengths stay);
* the fills survive a same-file rescan / re-extract and yield to the file's
  own values; the other year writers keep the provenance straight;
* the post-scan runner order and the admin endpoints.

Every index is a throw-away sqlite file; the store is an in-memory TrackStore
and prefs go to a temp file (nothing touches the real data dir)."""
from __future__ import annotations

import asyncio

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import songdb
from soniqboom.core.store import TrackStore

A, B, C, D, E = ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd",
                 "eeeeeeeeeeee")


def _tsvs(tmp_path, meta_rows, len_rows, *, filler=0):
    """Write metadata / songlengths TSVs (+ ``filler`` unrelated rows)."""
    meta = [f"{k}\t{a}\t{p}\t{al}\t{y}" for k, a, p, al, y in meta_rows]
    lens = [f"{k}\t{mn}\t{ss}" for k, mn, ss in len_rows]
    for i in range(filler):
        k = f"f{i:011x}"
        meta.append(f"{k}\tFiller {i}\t\t\t")
        lens.append(f"{k}\t1\t1000,p")
    m = tmp_path / "metadata.tsv"
    s = tmp_path / "songlengths.tsv"
    m.write_text("\n".join(meta) + "\n", encoding="utf-8")
    s.write_text("\n".join(lens) + "\n", encoding="utf-8")
    return m, s


META = [
    (A, "David Whittaker", "Rainbird~Realtime Games", "Carrier Command", "1988"),
    (B, "Jon Hare~Richard Joseph", "Sensible Software", "Cannon Fodder 2", "1994"),
    (C, "Unknown", "The Silents", "Megademo", "1990"),
    (D, "4-Mat", "Anarchy", "Megademo", "1991"),
    (E, "Anonymous~Heatbeat", "", "Megademo", ""),
    # same game, publisher lists that overlap → one production, plain name
    ("111111111111", "Richard Joseph", "Sensible Software~Virgin Interactive",
     "Cannon Fodder 2", "1994"),
    # overlaps The Silents → their production; shown by the most frequent group
    ("222222222222", "Bex", "The Silents~Channel 42", "Megademo", "1990"),
    ("333333333333", "Mr. Man", "The Silents", "Megademo", "1990"),
]
LENS = [
    (A, "1", "245000,p"),
    (B, "0", "0,p 30000,p+s"),
    (C, "1", "181000,t"),
    (D, "1", "95500,p,!"),
]


@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 1)
    monkeypatch.setattr(songdb, "_MIN_LENGTH_ROWS", 1)


@pytest.fixture
def env(tmp_path, monkeypatch, small_limits):
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    db = tmp_path / "scene" / "songdb.sqlite"
    db.parent.mkdir()
    monkeypatch.setattr(songdb, "_db_path", lambda: db)
    m, s = _tsvs(tmp_path, META, LENS)
    songdb.build_index(m, s, db)
    calls = []

    async def _refresh(ids):
        calls.append(sorted(ids))
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    monkeypatch.setattr(fa, "enabled", lambda: False)
    monkeypatch.setattr("soniqboom.config.PREFS_PATH", tmp_path / "prefs.json")
    songdb._status.update(applying=False, refreshing=False, error=None,
                          meta_rows=0, length_rows=0, last_apply=None, last_reset=None)
    songdb._last_auto_sig = None
    return store, db, calls, tmp_path


def _add(store, tid, key, **kw):
    t = {"id": tid, "path": f"/m/{tid}", "title": tid, "artist": "", "album": "",
         "format": "Amiga custom", "genre": ["Amiga", "Module"], "file_md5": key + "0" * 20,
         "duration": 0.0}
    t.update(kw)
    store.upsert_track(t)


# ── index build ──────────────────────────────────────────────────────────────

def test_build_index_values(env):
    store, db, _calls, _tmp = env
    import sqlite3
    con = sqlite3.connect(db)
    rows = {k: v for k, *v in con.execute("SELECT key, artist, label, album, year FROM meta")}
    info = dict(con.execute("SELECT k, v FROM info"))
    con.close()
    assert rows[A] == ["David Whittaker", "Rainbird, Realtime Games", "Carrier Command", 1988]
    assert rows[B] == ["Jon Hare & Richard Joseph", "Sensible Software", "Cannon Fodder 2", 1994]
    # "Megademo" is used by several groups → qualified; placeholder author dropped
    assert rows[C] == [None, "The Silents", "Megademo (The Silents)", 1990]
    assert rows[D] == ["4-Mat", "Anarchy", "Megademo (Anarchy)", 1991]
    # shared name without a group: no album; Anonymous dropped
    assert rows[E] == ["Heatbeat", None, None, None]
    assert rows["111111111111"][2] == "Cannon Fodder 2"
    assert rows["222222222222"][2] == "Megademo (The Silents)"
    assert info["meta_rows"] == "8" and info["length_rows"] == "4"


@pytest.mark.parametrize("row,track,expect", [
    ((1, "245000,p"), {}, 245.0),
    ((0, "95500,p,!"), {}, 95.5),                           # duplicate marker
    ((0, "95500,p"), {"subsongs": 1}, 95.5),
    ((1, "181000,t"), {}, None),                            # timeout
    ((0, "95500,l"), {}, None),                             # loop point: not our length
    ((0, "95500,p+s"), {}, None),                           # silence end
    ((0, "95500,p+v"), {}, None),
    ((0, "5000,n"), {}, None),                              # no sound
    ((0, "5000,e"), {}, None),                              # error
    ((0, "0,p"), {}, None),                                 # zero length
    ((0, "511000,p"), {}, 511.0),
    ((0, "512500,p"), {}, None),                            # past uade's render timeout
    ((0, "828460,p"), {}, None),
    # more than one tune (the file's own default tune isn't known) → no length
    ((1, "245000,p 12000,p"), {"subsongs": 2, "subsong_base": 1}, None),
    ((0, "5000,p 6000,p"), {"subsongs": 2}, None),
    ((0, "5000,p 6000,p"), {}, None),
    ((0, "5000,p"), {"subsongs": 3}, None),
    ((0, "5000,p"), {"start_subsong": 1}, None),
    ((0, ""), {}, None),
    (None, {}, None),
    ((0, "x,p"), {}, None),
])
def test_default_tune_seconds(row, track, expect):
    assert songdb.default_tune_seconds(row, track) == expect


def test_build_rejects_implausible_input(tmp_path, monkeypatch):
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 10)
    monkeypatch.setattr(songdb, "_MIN_LENGTH_ROWS", 1)
    m, s = _tsvs(tmp_path, META, LENS)
    with pytest.raises(RuntimeError, match="only 8 entries"):
        songdb.build_index(m, s, tmp_path / "x.sqlite")
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 1)
    m.write_text(m.read_text() + "".join(f"garbage line {i}\n" for i in range(3)))
    with pytest.raises(RuntimeError, match="not in the expected format"):
        songdb.build_index(m, s, tmp_path / "x.sqlite")


def test_build_rejects_a_reformatted_file(tmp_path, small_limits):
    """A column inserted upstream (or a changed year column) fails the row
    format check instead of filling artists with the wrong column."""
    m, s = _tsvs(tmp_path, META, LENS)
    shifted = "\n".join(line.split("\t", 1)[0] + "\tProtracker\t" + line.split("\t", 1)[1]
                        for line in m.read_text().splitlines()) + "\n"
    m.write_text(shifted)
    with pytest.raises(RuntimeError, match="only 0 entries"):
        songdb.build_index(m, s, tmp_path / "x.sqlite")
    m2, s2 = _tsvs(tmp_path, [(k, a, p, al, "199x") for k, a, p, al, _y in META], LENS)
    with pytest.raises(RuntimeError, match="only 0 entries"):
        songdb.build_index(m2, s2, tmp_path / "x.sqlite")
    m3, s3 = _tsvs(tmp_path, META, [(k, mn, ss.replace(",", ";")) for k, mn, ss in LENS])
    with pytest.raises(RuntimeError, match="songlengths.tsv has only 0 entries"):
        songdb.build_index(m3, s3, tmp_path / "x.sqlite")


def test_refresh_keeps_the_old_index_on_a_bad_download(env, monkeypatch):
    store, db, _calls, tmp = env
    before = db.read_bytes()

    def bad_download(name, dest):
        dest.write_text("<html>moved</html>\n")
    monkeypatch.setattr(songdb, "_download", bad_download)
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 3)
    st = songdb.refresh_index()
    assert "index refresh failed" in st["error"]
    assert db.read_bytes() == before, "a failed refresh must keep the current index"
    assert not list(db.parent.glob("*.part")) and not db.with_suffix(".building").exists()

    def good_download(name, dest):
        dest.write_bytes((tmp / name).read_bytes())
    monkeypatch.setattr(songdb, "_download", good_download)
    st = songdb.refresh_index()
    assert st["error"] is None and st["meta_rows"] == 8 and st["length_rows"] == 4
    assert st["exists"] and not st["refreshing"]


def test_refresh_rejects_a_second_concurrent_run(env):
    songdb._status["refreshing"] = True
    try:
        assert songdb.refresh_index()["error"] == "refresh already running"
    finally:
        songdb._status["refreshing"] = False


# ── apply ────────────────────────────────────────────────────────────────────

async def test_apply_fills_empty_fields_only(env):
    store, _db, calls, _tmp = env
    _add(store, "a", A)                                         # all empty
    _add(store, "b", B, artist="<?>", album="Folder Name", album_source="folder",
         year=None, duration=180.0)
    _add(store, "c", C, artist="Real Artist", label="Tag Label",
         album="Tag Album", year=1989, duration=0.0)
    _add(store, "d", D, album="Game", album_source="modland",
         year=1990, year_source="demozoo", year_file=1995)
    _add(store, "e", E, album="Guess", album_source="modland-filename")
    _add(store, "x", "ffffffffffff")                           # no match
    _add(store, "trk", D, format="ProTracker", genre=["Tracker", "Module"])
    _add(store, "nomd5", A, file_md5=None)
    res = await songdb.apply_to_library(force=True)
    assert res["error"] is None
    t = store.get_track
    a = t("a")
    assert (a["artist"], a["label"], a["album"], a["album_source"], a["year"],
            a["year_source"], a["duration"]) == (
        "David Whittaker", "Rainbird, Realtime Games", "Carrier Command", "songdb",
        1988, "songdb", 245.0)
    assert a["songdb_fields"] == ["artist", "label"]
    b = t("b")
    assert b["artist"] == "Jon Hare & Richard Joseph"          # placeholder replaced
    assert (b["album"], b["album_source"]) == ("Cannon Fodder 2", "songdb")  # guess upgraded
    assert b["duration"] == 180.0                               # a known length is kept
    c = t("c")
    assert (c["artist"], c["label"], c["album"], c["year"]) == (
        "Real Artist", "Tag Label", "Tag Album", 1989)          # file values kept
    assert c["duration"] == 0.0                                 # timeout end: no length
    assert not c.get("songdb_fields")
    d = t("d")
    assert (d["album"], d["album_source"]) == ("Game", "modland")   # exact Modland kept
    # the song database's own name for it is recorded — an alias ``game:`` finds
    assert d["game_by_songdb"] == "Megademo (Anarchy)" and d["game_aliases"] == ["Megademo (Anarchy)"]
    assert (d["year"], d["year_source"]) == (1990, "demozoo")
    assert d["artist"] == "4-Mat" and d["duration"] == 95.5
    e = t("e")
    assert (e["album"], e["album_source"]) == ("Guess", "modland-filename")  # no db album
    assert e["artist"] == "Heatbeat"
    assert t("x")["artist"] == "" and t("nomd5")["artist"] == ""
    # not played through uade: its length is never re-measured → not filled
    assert t("trk")["duration"] == 0.0 and t("trk")["artist"] == "4-Mat"
    la = res["last_apply"]
    assert la["matched"] == 6 and la["updated"] == 6 and la["albums"] == 3
    assert len(calls) == 1
    # the album index sees the new album
    assert "a" in store.filter_track_ids(album="Carrier Command")
    # idempotent
    res2 = await songdb.apply_to_library(force=True)
    assert res2["last_apply"]["updated"] == 0 and len(calls) == 1


async def test_user_edits_are_never_touched(env):
    store, *_ = env
    _add(store, "a", A, user_edited=["artist", "album", "label"])
    store.update_track_fields("a", {"year": None, "year_source": "user"})
    await songdb.apply_to_library(force=True)
    a = store.get_track("a")
    assert (a["artist"], a["album"], a.get("label", ""), a["year"]) == ("", "", "", None)
    assert a["duration"] == 245.0                   # the length is not a user field
    assert not a.get("songdb_fields")


async def test_auto_apply_skips_an_unchanged_library(env):
    store, *_ = env
    _add(store, "a", A)
    await songdb.apply_to_library()
    assert store.get_track("a")["artist"] == "David Whittaker"
    res = await songdb.apply_to_library()
    assert res.get("skipped") == "unchanged"
    _add(store, "b", B)                             # the library changed
    res = await songdb.apply_to_library()
    assert not res.get("skipped") and store.get_track("b")["artist"]


async def test_concurrent_apply_is_refused(env):
    songdb._status["applying"] = True
    try:
        res = await songdb.apply_to_library(force=True)
        assert res["error"] == "apply already running"
    finally:
        songdb._status["applying"] = False


async def test_patch_is_computed_at_write_time(env, monkeypatch):
    """An artist / album set between the off-loop join and the write wins."""
    store, *_ = env
    _add(store, "a", A)
    _add(store, "b", B)
    real = songdb.collect

    def racing(tracks, **kw):
        out = real(tracks, **kw)
        store.update_track_fields("a", {"artist": "Rescan Artist", "album": "Tag"})
        # a rescan swapped in a different file for "b"
        store.update_track_fields("b", {"file_md5": "9" * 32})
        return out
    monkeypatch.setattr(songdb, "collect", racing)
    await songdb.apply_to_library(force=True)
    a = store.get_track("a")
    assert (a["artist"], a["album"]) == ("Rescan Artist", "Tag")
    assert a["label"] == "Rainbird, Realtime Games"      # still-empty fields filled
    assert a["songdb_fields"] == ["label"]
    assert store.get_track("b")["artist"] == ""          # different file: untouched


async def test_refreshed_index_updates_and_withdraws(env, monkeypatch):
    store, db, _calls, tmp = env
    _add(store, "a", A)
    _add(store, "b", B)
    _add(store, "c", C)
    await songdb.apply_to_library(force=True)
    assert store.get_track("c")["album"] == "Megademo (The Silents)"
    # New index: A's credit changed, B lost its album + year, C left the index.
    meta = [(A, "Dave Whittaker", "Rainbird", "Carrier Command", "1988"),
            (B, "Jon Hare~Richard Joseph", "Sensible Software", "", "")]
    sub = tmp / "v2"
    sub.mkdir()
    m, s = _tsvs(sub, meta, LENS[:2])
    songdb.build_index(m, s, db)
    # an incomplete index never withdraws a missing row
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 3)
    await songdb.apply_to_library(force=True)
    assert store.get_track("c")["album"] == "Megademo (The Silents)"
    assert store.get_track("a")["artist"] == "Dave Whittaker"   # present rows follow
    assert store.get_track("b")["album"] == ""                 # present row: withdrawn
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 1)
    await songdb.apply_to_library(force=True)
    a, b, c = (store.get_track(x) for x in "abc")
    assert (a["artist"], a["label"]) == ("Dave Whittaker", "Rainbird")
    assert (b["album"], b["album_source"], b["year"], b["year_source"]) == ("", None, None, None)
    assert b["artist"] == "Jon Hare & Richard Joseph"
    assert (c["label"], c["album"], c["album_source"], c["year"]) == ("", "", None, None)
    assert not c.get("songdb_fields")
    assert c["duration"] == 0.0


async def test_withdrawn_album_runs_the_folder_pass(env, monkeypatch):
    store, db, _calls, tmp = env
    _add(store, "b", B)
    await songdb.apply_to_library(force=True)
    ran = []

    async def folder(force=False):
        ran.append(force)
    monkeypatch.setattr(fa, "enabled", lambda: True)
    monkeypatch.setattr(fa, "apply_folder_albums", folder)
    sub = tmp / "v2"
    sub.mkdir()
    m, s = _tsvs(sub, [(B, "Jon Hare", "", "", "")], LENS[:1])
    songdb.build_index(m, s, db)
    await songdb.apply_to_library(force=True)
    assert store.get_track("b")["album"] == "" and ran == [True]


async def test_reset_withdraws_exactly_the_fills(env):
    store, *_ = env
    _add(store, "a", A)
    _add(store, "b", B, album="Tag Album", year=1993)
    await songdb.apply_to_library(force=True)
    # the user edits the label afterwards: it is theirs now
    store.update_track_fields("a", {"label": "My Label", "user_edited": ["label"]})
    res = await songdb.reset_to_file_state()
    assert res["error"] is None and res["cleared"] == 2
    a, b = store.get_track("a"), store.get_track("b")
    assert (a["artist"], a["album"], a["album_source"], a["year"], a["year_source"]) == (
        "", "", None, None, None)
    assert a["label"] == "My Label"
    assert a["duration"] == 245.0                   # lengths stay
    assert not a.get("songdb_fields")
    assert (b["artist"], b["album"], b["year"]) == ("", "Tag Album", 1993)
    res = await songdb.reset_to_file_state()
    assert res["cleared"] == 0


# ── provenance through rescans / re-extracts / other writers ─────────────────

async def test_same_file_rescan_keeps_the_fills_without_churn(env):
    from soniqboom.core.scanner import _same_track_content
    store, *_ = env
    _add(store, "a", A)
    await songdb.apply_to_library(force=True)
    old = dict(store.get_track("a"))
    fresh = {"id": "a", "path": "/m/a", "title": "a", "artist": "", "album": "",
             "label": "", "year": None, "format": "Amiga custom", "genre": ["Amiga", "Module"],
             "file_md5": A + "0" * 20, "duration": 0.0}
    assert _same_track_content(old, dict(fresh)), "a same-file rescan must not re-upsert"
    store.upsert_tracks_batch([dict(fresh)])       # the scanner's write path
    a = store.get_track("a")
    assert (a["artist"], a["label"], a["album"], a["album_source"], a["year"],
            a["year_source"]) == ("David Whittaker", "Rainbird, Realtime Games",
                                  "Carrier Command", "songdb", 1988, "songdb")
    assert a["songdb_fields"] == ["artist", "label"]


async def test_file_values_and_new_files_win_on_rescan(env):
    store, *_ = env
    _add(store, "a", A)
    _add(store, "b", B)
    await songdb.apply_to_library(force=True)
    # "a": the file now names an artist and a year; "b": a different file
    store.upsert_tracks_batch([            # the scanner's write path
        {"id": "a", "path": "/m/a", "title": "a", "artist": "File Artist",
         "album": "", "year": 1987, "format": "Amiga custom", "genre": ["Amiga", "Module"],
         "file_md5": A + "0" * 20, "duration": 0.0},
        {"id": "b", "path": "/m/b", "title": "b", "artist": "",
         "album": "", "format": "Amiga custom", "genre": ["Amiga", "Module"],
         "file_md5": "9" * 32, "duration": 0.0}])
    a, b = store.get_track("a"), store.get_track("b")
    assert (a["artist"], a["year"], a.get("year_source")) == ("File Artist", 1987, None)
    assert a["label"] == "Rainbird, Realtime Games" and a["songdb_fields"] == ["label"]
    assert a["album"] == "Carrier Command"
    assert (b["artist"], b["album"], b.get("songdb_fields")) == ("", "", None)
    # the next apply does not overwrite the file's own artist
    await songdb.apply_to_library(force=True)
    assert store.get_track("a")["artist"] == "File Artist"


def test_repair_reextract_keeps_fills_and_lets_the_file_win():
    from soniqboom.core.repair import _changed_fields
    old = {"artist": "David Whittaker", "label": "Rainbird", "album": "Carrier Command",
           "album_source": "songdb", "year": 1988, "year_source": "songdb",
           "songdb_fields": ["artist", "label"], "file_md5": A + "0" * 20}
    same = {"artist": "", "label": "", "album": "", "year": None, "file_md5": None}
    assert _changed_fields(old, same) == {}
    placeholder = dict(same, artist="<?>")
    assert _changed_fields(old, placeholder) == {}
    real = {"artist": "File Artist", "label": "", "album": "", "year": 1987,
            "file_md5": A + "0" * 20}
    out = _changed_fields(old, real)
    assert out["artist"] == "File Artist" and out["year"] == 1987
    assert out["year_source"] is None and out["year_file"] is None
    assert out["songdb_fields"] == ["label"]
    assert "label" not in out and "album" not in out


async def test_meta_edit_takes_the_field_out_of_the_fills(env):
    from soniqboom.api.tracks import _MetaUpdate, update_meta
    store, *_ = env
    _add(store, "a", A)
    await songdb.apply_to_library(force=True)
    res = await update_meta("a", _MetaUpdate(artist="Me"), user=None)
    assert res["applied"]["songdb_fields"] == ["label"]
    a = store.get_track("a")
    assert a["artist"] == "Me" and a["user_edited"] == ["artist"]
    await songdb.reset_to_file_state()
    assert store.get_track("a")["artist"] == "Me"


async def test_year_edit_over_a_songdb_year_keeps_year_file_empty(env):
    from soniqboom.api.tracks import _YearUpdate, update_year
    store, *_ = env
    _add(store, "a", A)
    await songdb.apply_to_library(force=True)
    res = await update_year("a", _YearUpdate(year=1989), user=None)
    assert res["applied"] == {"year": 1989, "year_source": "user"}
    assert store.get_track("a").get("year_file") is None


# ── runner / admin ───────────────────────────────────────────────────────────

async def test_runner_order_modland_songdb_demozoo(monkeypatch):
    from soniqboom.core import demozoo, repair, scanner, scene_metadata
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(scanner, "_SCENE_AUTOAPPLY_SETTLE_S", 0)
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: False)
    scanner._scene_autoapply_pending = False
    scanner._scene_autoapply_running = False
    order = []
    state = {"auto": True}
    monkeypatch.setattr(scene_metadata, "has_index", lambda: True)
    monkeypatch.setattr(songdb, "has_index", lambda: True)
    monkeypatch.setattr(songdb, "auto_apply_enabled", lambda: state["auto"])
    monkeypatch.setattr(demozoo, "has_index", lambda: True)
    monkeypatch.setattr(demozoo, "auto_apply_enabled", lambda: True)

    async def modland(*, auto=False):
        order.append("modland")
        return {"last_apply": {"updated": 0}}

    async def sdb(*, force=False):
        order.append(("songdb", force))
        return {"last_apply": {"updated": 1}}

    async def dz():
        order.append("demozoo")
        return {"updated": 0}

    async def backfill():
        return False
    monkeypatch.setattr(scene_metadata, "apply_to_library", modland)
    monkeypatch.setattr(songdb, "apply_to_library", sdb)
    monkeypatch.setattr(demozoo, "apply_to_library", dz)
    monkeypatch.setattr(repair, "run_album_backfill_once", backfill)
    monkeypatch.setattr(fa, "schedule_after_scan", lambda: order.append("folder"))
    scanner._spawn_scene_autoapply()
    for _ in range(50):
        await asyncio.sleep(0)
        if not scanner._scene_autoapply_running:
            break
    assert order == ["modland", ("songdb", False), "demozoo", "folder"]
    # auto-apply off: skipped (only the song database)
    order.clear()
    state["auto"] = False
    scanner._spawn_scene_autoapply()
    for _ in range(50):
        await asyncio.sleep(0)
        if not scanner._scene_autoapply_running:
            break
    assert order == ["modland", "demozoo", "folder"]
    # with only a song-database index the runner is still wanted
    monkeypatch.setattr(scene_metadata, "has_index", lambda: False)
    monkeypatch.setattr(demozoo, "has_index", lambda: False)
    state["auto"] = True
    assert scanner._scene_enrichment_wanted()
    state["auto"] = False
    assert not scanner._scene_enrichment_wanted()


async def test_admin_endpoints(env, monkeypatch):
    from soniqboom.api import admin
    from soniqboom.core import scanner
    store, *_ = env
    _add(store, "a", A)
    spawned = []
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda: spawned.append(1))
    st = await admin.admin_songdb_status(_tok="t")
    assert st["exists"] and st["meta_rows"] == 8 and st["auto_apply"] is True
    st = await admin.admin_songdb_auto_apply({"enabled": "false"}, _tok="t")
    assert st["auto_apply"] is False
    res = await admin.admin_songdb_apply(_tok="t")
    assert res["last_apply"]["updated"] == 1 and songdb.auto_apply_enabled()
    res = await admin.admin_songdb_reset(_tok="t")
    assert res["cleared"] == 1 and not songdb.auto_apply_enabled()
    assert store.get_track("a")["artist"] == ""

    def fake_refresh():
        return songdb.status()
    monkeypatch.setattr(songdb, "refresh_index", fake_refresh)
    await admin.admin_songdb_refresh(_tok="t")
    assert spawned == []                        # auto-apply is off after the reset
    songdb.set_auto_apply(True)
    await admin.admin_songdb_refresh(_tok="t")
    assert spawned == [1]


async def test_modland_credit_equal_to_the_fill_keeps_the_provenance(env):
    """A same-file rescan of a track whose Modland credit is the very name the
    song database filled keeps ``songdb_fields`` (no re-upsert), so Reset still
    clears the artist (the next Modland apply fills its credit again)."""
    from soniqboom.core import scene_metadata as sm
    from soniqboom.core.scanner import _same_track_content
    store, *_ = env
    _add(store, "a", A)
    await songdb.apply_to_library(force=True)
    path = "David Whittaker/David Whittaker/carrier command.dw"
    assert sm.author_credit(sm.parse_modland_path(path)) == "David Whittaker"
    store.update_track_fields("a", {"scene_path": path})
    old = dict(store.get_track("a"))
    fresh = {"id": "a", "path": "/m/a", "title": "a", "artist": "", "album": "", "label": "",
             "year": None, "format": "Amiga custom", "genre": ["Amiga", "Module"],
             "file_md5": A + "0" * 20, "duration": 0.0}
    assert _same_track_content(old, dict(fresh))
    store.upsert_tracks_batch([dict(fresh)])
    assert store.get_track("a")["songdb_fields"] == ["artist", "label"]
    await songdb.reset_to_file_state()
    assert store.get_track("a")["artist"] == ""


def test_demozoo_withdraws_a_group_left_by_a_cleared_artist(tmp_path, monkeypatch):
    """A song-database Reset / withdrawal clears an artist Demozoo had resolved
    a scene group from: with no artist and no composer, the group names
    nobody's crew, so the next Demozoo join withdraws it — unless the user
    typed it; a group next to a composer (the title-first match) stays."""
    from soniqboom.core import demozoo
    import soniqboom.core.store as store_mod
    from test_demozoo_scene import _write_dump
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "demozoo.sqlite")
    assert not demozoo.refresh_index(
        dump_path=_write_dump(tmp_path, with_supertype=True)).get("error")
    tracks = [
        {"id": "orphan", "format": "ProTracker", "artist": "", "title": "x",
         "scene_group": "Future Crew"},
        {"id": "typed", "format": "ProTracker", "artist": "", "title": "x",
         "scene_group": "My Crew", "user_edited": ["scene_group"]},
        {"id": "composer", "format": "ProTracker", "artist": "", "title": "x",
         "composer": "Purple Motion", "scene_group": "Future Crew"},
    ]

    class _Store:
        def all_tracks(self):
            return tracks
    monkeypatch.setattr(store_mod, "get_store", lambda: _Store())
    _matched, batch = demozoo.collect_updates()
    assert dict(batch) == {"orphan": {"scene_group": None}}


def test_publisher_spellings_of_one_production_stay_one_album():
    rows = [
        ["a", "x", "Thalion", "Amberstar", ""], ["b", "x", "Thalion Software", "Amberstar", ""],
        ["c", "x", "Team17", "Full Contact", ""], ["d", "x", "Team 17", "Full Contact", ""],
        ["e", "x", "Kaktus", "His Master's Noise", ""],
        ["f", "x", "Mahoney & Kaktus", "His Master's Noise", ""],
        ["g", "x", "ORIGIN Systems", "Crusader", ""], ["h", "x", "Origin", "Crusader", ""],
        ["m", "x", "System 3", "Flimbo's Quest", ""],
        ["n", "x", "System 3 Software", "Flimbo's Quest", ""],
        # different groups: still qualified, each by its most frequent spelling
        ["i", "x", "The Silents", "Megademo", ""], ["j", "x", "Silents", "Megademo", ""],
        ["k", "x", "The Silents", "Megademo", ""], ["l", "x", "Anarchy", "Megademo", ""],
    ]
    q = songdb._album_qualifiers(rows)
    assert set(q) == {"megademo"}
    assert q["megademo"] == {"the silents": "The Silents", "silents": "The Silents",
                             "anarchy": "Anarchy"}
    assert songdb._publisher_keys("Mahoney & Kaktus") == ["mahoneykaktus", "mahoney", "kaktus"]
    assert songdb._publisher_keys("Team 17") == songdb._publisher_keys("Team17") == ["team17"]
    # a name of filler words only stays whole — not merged with its first word
    assert songdb._publisher_keys("Interactive Design") == ["interactivedesign"]
    assert songdb._publisher_keys("Interactive") == ["interactive"]



async def test_every_uade_played_copy_gets_the_same_length(env):
    """A prefix-named AHX in an LHA pack (uade-extracted) and the same file
    named ``.ahx`` (tracker-extracted, still played through uade) get the same
    length — duration is part of the duplicate key; a HivelyTracker ``.hvl``
    (played by hvl2wav) and an ordinary tracker module get none."""
    store, db, _calls, tmp = env
    lens = [("333333333333", "0", "48720,p"), ("444444444444", "0", "138000,p")]
    sub = tmp / "v3"
    sub.mkdir()
    m, s_ = _tsvs(sub, META, LENS + lens)
    songdb.build_index(m, s_, db)
    _add(store, "in_lha", "333333333333", path="/m/pack.lha::AHX.tune.ahx")
    _add(store, "in_zip", "333333333333", path="/m/pack.zip::tune.ahx",
         genre=["Tracker", "Module"], format="AHX")
    _add(store, "hvl", "333333333333", path="/m/tune.hvl", genre=["Tracker", "Module"],
         format="HivelyTracker")
    _add(store, "mod", "444444444444", path="/m/tune.mod", genre=["Tracker", "Module"],
         format="ProTracker")
    await songdb.apply_to_library(force=True)
    d = {x: store.get_track(x)["duration"] for x in ("in_lha", "in_zip", "hvl", "mod")}
    assert d == {"in_lha": 48.72, "in_zip": 48.72, "hvl": 0.0, "mod": 0.0}


async def test_a_label_can_be_corrected_per_track(env):
    from soniqboom.api.tracks import _MetaUpdate, update_meta
    store, *_ = env
    _add(store, "a", A)
    await songdb.apply_to_library(force=True)
    res = await update_meta("a", _MetaUpdate(label="Rainbird"), user=None)
    assert res["applied"]["label"] == "Rainbird"
    assert res["applied"]["songdb_fields"] == ["artist"]
    a = store.get_track("a")
    assert a["label"] == "Rainbird" and "label" in a["user_edited"]
    await songdb.apply_to_library(force=True)
    await songdb.reset_to_file_state()
    assert store.get_track("a")["label"] == "Rainbird"
