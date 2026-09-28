# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-1 review fixes in the Modland enrichment (core/scene_metadata.py):

* the ``<game>-<part>`` file-name game also for the Amiga PREFIX form
  (``mdat.denny-level4``) and for title-less custom replayers, never for a
  ``smpl.*`` companion;
* SNDH replay-tool dirs (DMA, X32, …) are not games; dotted acronyms are;
* the batched md5 join returns exactly what the per-row join did;
* a track whose md5 left a (sane) index loses its Modland album + scene_path;
* switching the file-name option while an apply runs never leaves a guess;
* a cut-off 32-byte SPC tag game is completed by the Modland game dir."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import scene_metadata as sm
from soniqboom.core.store import TrackStore


# ── file-name game: prefix form + title-less custom formats ─────────────────

@pytest.mark.parametrize("filename,title,flag,expect", [
    ("mdat.denny-level4", "level4", False, "Denny"),            # prefix form, title = part
    ("mdat.denny-level4", "denny-level4", True, "Denny"),       # title from the file name
    ("mdat.denny-level4", "denny-level4", False, None),         # …only for custom formats
    ("smpl.denny-level4", "level4", False, None),               # sample companion
    ("SMPL.denny-level4", "denny-level4", True, None),
    ("gold of the aztecs-intro.dw", "gold of the aztecs-intro", True, "Gold of the Aztecs"),
    ("jpn.virocop-09", "virocop-09", True, "Virocop"),
    ("ice.mod", "mod", False, None),                             # "ice." is not a prefix here
    ("music-intro.mod", "intro", False, None),                   # generic "game"
    ("disk 2-intro.mod", "intro", False, None),                  # an album part
    ("4-mat.mod", "4-mat", True, None),                          # still refused (game "4")
])
def test_filename_game_prefix_form_and_title_from_file_name(filename, title, flag, expect):
    # The title-is-the-file-name path needs a sibling file with the same game
    # prefix in the same Modland dir (round 2): every case gets one.
    sibs = _siblings_with_a_second_part(filename, "Fmt/Author")
    assert sm.filename_game(filename, title, title_is_filename=flag,
                            siblings=sibs, modland_dir="Fmt/Author") == expect


def _siblings_with_a_second_part(filename: str, d: str) -> dict:
    """The ``modland_filename_siblings`` entry a dir holding ``filename`` and
    one more file of the same game (a different first word in its part)."""
    split = sm._game_part(filename)
    return {sm._sibling_key(d, split[0]): False} if split else {}


def _t(**kw):
    t = {"id": "a", "title": "denny-level4", "format": "TFMX", "genre": ["Amiga"],
         "album": "", "album_source": None, "artist": ""}
    t.update(kw)
    return t


def test_album_update_accepts_file_name_title_only_for_custom_replayers(monkeypatch):
    mp = sm.parse_modland_path("TFMX/Chris Huelsbeck/mdat.denny-level4")
    monkeypatch.setattr(sm, "chip_family", lambda fmt: "paula" if fmt == "TFMX" else "tracker")
    sibs = {sm._sibling_key("TFMX/Chris Huelsbeck", "denny"): False,
            sm._sibling_key("Protracker/Someone", "pop"): False,
            sm._sibling_key("TFMX/Jochen Hippel", "hippel"): False}
    assert sm.album_update_for(_t(), mp, siblings=sibs,
                               modland_dir="TFMX/Chris Huelsbeck") == \
        {"album": "Denny", "album_source": "modland-filename"}
    # Without sibling evidence the file-name title proves nothing (round 2).
    assert sm.album_update_for(_t(), mp) is None
    # A tracker module names itself: a file-name title proves nothing there.
    mp2 = sm.parse_modland_path("Protracker/Someone/pop-corn.mod")
    assert sm.album_update_for(_t(title="pop-corn", format="ProTracker"), mp2,
                               siblings=sibs, modland_dir="Protracker/Someone") is None
    # The composer guard still applies to the loosened path.
    mp3 = sm.parse_modland_path("TFMX/Jochen Hippel/mdat.hippel-intro")
    assert sm.album_update_for(_t(title="hippel-intro"), mp3, siblings=sibs,
                               modland_dir="TFMX/Jochen Hippel") is None


def test_per_module_dir_check_understands_the_prefix_form():
    # "TFMX/<Author>/denny-level4/mdat.denny-level4" — the dir IS the tune.
    assert sm.parse_modland_path("TFMX/X/denny-level4/mdat.denny-level4").game is None


# ── SNDH tool dirs / dotted acronyms ────────────────────────────────────────

@pytest.mark.parametrize("path,game", [
    ("SNDH/Excellence_In_Art/DMA/Summer_Delights.sndh", None),
    ("SNDH/Unknown_Composer/X32/Titel.sndh", None),
    ("SNDH/Someone/Music_Studio/x.sndh", None),
    ("SNDH/Someone/TSD_STe/x.sndh", None),
    ("SNDH/Someone/STOS/x.sndh", None),
    ("SNDH/Someone/Quartet/x.sndh", None),
    ("MDX/Someone/Quartet/x.mdx", "Quartet"),          # a real game elsewhere
    ("SNDH/505/Relix/Sherlock.sndh", "Relix"),
    ("Nintendo SPC/Someone/B.O.B/01 x.spc", "B.O.B"),
    ("Nintendo SPC/Someone/W.I.T.C.H/01 x.spc", "W.I.T.C.H"),
])
def test_sndh_tool_dirs_and_dotted_acronyms(path, game):
    assert sm.parse_modland_path(path).game == game


# ── collect / apply against a temp index ─────────────────────────────────────

def _index(tmp_path, rows, name="modland.sqlite"):
    db = tmp_path / name
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.executemany("INSERT INTO mods VALUES (?,?)", rows)
    con.commit()
    con.close()
    return db


_ROWS = [
    ("a" * 32, "David Whittaker/David Whittaker/gold of the aztecs-intro.dw"),
    ("b" * 32, "Nintendo SPC/Koji Kondo/Super Mario World/01 - title.spc"),
    ("c" * 32, "Ad Lib/EdLib Packed/Drax/coop-Metal/lollypop - title1.edl"),
    ("d" * 32, "Protracker/4-Mat/4-mat.mod"),
    ("e" * 32, "Nintendo SPC/Koji Kondo/Super Mario World/02 - overworld.spc"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    db = _index(tmp_path, _ROWS)
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    calls = []
    async def _refresh(ids):
        calls.append(1)
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    sm._status["applying"] = False
    sm._last_auto_sig = None
    return store, tmp_path, monkeypatch


def _add(store, tid, md5, **kw):
    t = {"id": tid, "path": f"/m/{tid}", "title": "", "artist": "", "album": "",
         "format": "ProTracker", "genre": [], "file_md5": md5}
    t.update(kw)
    store.upsert_track(t)


def _collect_per_row(tracks, db, use_filename):
    """The pre-batching join (one SELECT per track), as the parity oracle."""
    con = sqlite3.connect(db)
    out = []
    try:
        for t in tracks:
            md5 = t.get("file_md5")
            if not md5:
                continue
            row = con.execute("SELECT path FROM mods WHERE md5 = ?", (md5.lower(),)).fetchone()
            if row:
                out.append((t["id"], row[0]))
    finally:
        con.close()
    return out


def test_batched_md5_join_matches_the_per_row_join(env):
    store, tmp_path, _mp = env
    _add(store, "dw", "A" * 32, title="intro", format="David Whittaker", genre=["Amiga"])
    _add(store, "spc", "b" * 32, title="Title", format="SPC")
    _add(store, "dup", "B" * 32, title="Title", format="SPC")      # same md5, other case
    _add(store, "edl", "c" * 32, title="title1", format="EdLib")
    _add(store, "miss", "f" * 32)
    _add(store, "none", "")
    matched, batch, _exp = sm.collect_updates(use_filename=True)
    oracle = _collect_per_row(store.all_tracks(), sm._db_path(), True)
    assert matched == len(oracle) == 4
    by_id = dict(batch)
    for tid, path in oracle:
        assert by_id[tid]["scene_path"] == path
    assert "miss" not in by_id and "none" not in by_id


async def test_stale_match_is_withdrawn_only_with_a_sane_index(env):
    store, tmp_path, monkeypatch = env
    _add(store, "spc", "b" * 32, title="Title", format="SPC")
    _add(store, "mine", "e" * 32, title="Overworld", format="SPC")
    await sm.apply_to_library()
    assert store.get_track("spc")["album"] == "Super Mario World"
    store.update_track_fields("mine", {"album": "My Name", "user_edited": ["album"]})
    # The index is refreshed without these md5s …
    db2 = _index(tmp_path, [("x" * 32, "Protracker/X/y.mod")], "modland2.sqlite")
    monkeypatch.setattr(sm, "_db_path", lambda: db2)
    # … but a tiny (truncated-looking) index never wipes enrichment.
    await sm.apply_to_library()
    assert store.get_track("spc")["album"] == "Super Mario World"
    assert store.get_track("spc")["scene_path"]
    monkeypatch.setattr(sm, "_WITHDRAW_MIN_INDEX_ROWS", 1)
    res = await sm.apply_to_library()
    assert res["last_apply"]["matched"] == 0
    spc = store.get_track("spc")
    assert (spc["album"], spc["album_source"], spc["scene_path"]) == ("", None, None)
    mine = store.get_track("mine")
    assert mine["album"] == "My Name" and mine["scene_path"] is None
    assert store.verify_indexes()["index_ok"] is True


async def test_switching_the_file_name_option_off_mid_apply_leaves_no_guess(env):
    store, _tmp, monkeypatch = env
    _add(store, "dw", "a" * 32, title="intro", format="David Whittaker", genre=["Amiga"])
    real = sm.collect_updates

    def collect_then_switch_off(**kw):
        out = real(**kw)
        store.set_config(sm.MODLAND_FILENAME_CONFIG_KEY, False)   # the admin toggle …
        return out
    monkeypatch.setattr(sm, "collect_updates", collect_then_switch_off)
    await sm.apply_to_library()
    t = store.get_track("dw")
    assert (t["album"], t.get("album_source")) == ("", None)
    assert t["scene_path"] and t["artist"] == "David Whittaker"   # other fields still written


async def test_switching_the_option_on_mid_apply_keeps_existing_guesses(env):
    store, _tmp, monkeypatch = env
    _add(store, "dw", "a" * 32, title="intro", format="David Whittaker", genre=["Amiga"])
    await sm.apply_to_library()
    assert store.get_track("dw")["album_source"] == "modland-filename"
    store.set_config(sm.MODLAND_FILENAME_CONFIG_KEY, False)
    real = sm.collect_updates

    def collect_then_switch_on(**kw):
        out = real(**kw)                     # computed the withdrawal …
        store.set_config(sm.MODLAND_FILENAME_CONFIG_KEY, True)
        return out
    monkeypatch.setattr(sm, "collect_updates", collect_then_switch_on)
    from soniqboom.core import scanner
    queued = []
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda: queued.append(1))
    await sm.apply_to_library()
    assert store.get_track("dw")["album"] == "The Gold of the Aztecs"    # the lists' spelling
    assert queued == [1]              # one more apply writes what this join skipped


async def test_commit_waits_for_the_folder_album_lock(env):
    """The Modland write takes the folder-album lock, so it can't interleave
    with a revert of the file-name guesses."""
    store, _tmp, _mp = env
    _add(store, "spc", "b" * 32, title="Title", format="SPC")
    lock = fa._get_lock()
    await lock.acquire()
    task = asyncio.ensure_future(sm.apply_to_library())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if sm._status["applying"] and store.get_track("spc")["album"] == "":
            break
    assert store.get_track("spc")["album"] == ""      # blocked on the lock
    lock.release()
    await task
    assert store.get_track("spc")["album"] == "Super Mario World"


async def test_auto_apply_skips_an_unchanged_library_and_records_the_version(env):
    store, _tmp, monkeypatch = env
    _add(store, "spc", "b" * 32, title="Title", format="SPC")
    calls = []
    real = sm.collect_updates
    monkeypatch.setattr(sm, "collect_updates", lambda **kw: calls.append(1) or real(**kw))
    await sm.apply_to_library(auto=True)
    assert store.get_config(sm.APPLY_VERSION_CONFIG_KEY) == sm.MODLAND_APPLY_VERSION
    assert (await sm.apply_to_library(auto=True)).get("skipped") == "unchanged"
    assert calls == [1]
    await sm.apply_to_library()                     # the Admin button always runs
    assert calls == [1, 1]
    _add(store, "new", "e" * 32, title="Overworld", format="SPC")
    await sm.apply_to_library(auto=True)            # a library change → runs
    assert calls == [1, 1, 1]
    assert store.get_track("new")["album"] == "Super Mario World"


# ── SPC: a cut-off tag game is completed by the Modland dir ──────────────────

def test_truncated_spc_tag_game_is_completed_by_the_modland_dir():
    mp = sm.parse_modland_path(
        "Nintendo SPC/Capcom/Street Fighter 2 - The World Warrior/01 x.spc")
    cut = "Street Fighter 2 - The World War"                      # 32 chars
    t = {"id": "s", "title": "x", "format": "SPC", "album": cut, "album_source": "tag"}
    assert sm.album_update_for(t, mp) == {
        "album": "Street Fighter 2 - The World Warrior", "album_source": "modland"}
    # a short tag game is a real (different) name — kept
    assert sm.album_update_for(dict(t, album="Street Fighter 2"), mp) is None
    # a user edit always wins
    assert sm.album_update_for(dict(t, user_edited=["album"]), mp) is None
