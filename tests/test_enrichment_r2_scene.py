# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-2 review fixes in the Modland enrichment (core/scene_metadata.py,
core/folder_album.py):

* a title-less custom replayer's ``<game>-<part>`` file-name guess needs a
  sibling file with the same game prefix in the same Modland dir, and is
  refused when the dash sits inside the name (``ice-runner tune1/tune2``);
  stale guesses are withdrawn by the version-bumped re-apply;
* a dash inside brackets never splits a game (``Music History (Commando -2)``);
* ``<name>.alt`` per-song dirs are not game albums;
* a Modland patch computed for one file is never written onto a different
  file that a rescan swapped in meanwhile (``file_md5`` precondition)."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import scene_metadata as sm
from soniqboom.core.store import TrackStore


async def _no_refresh(_ids):
    """Stand-in for ``folder_album.refresh_album_caches`` (which would touch
    the real data dir's browse cache file)."""
    return None


def _index(tmp_path, rows, name="modland.sqlite"):
    db = tmp_path / name
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.executemany("INSERT INTO mods VALUES (?,?)", rows)
    con.commit()
    con.close()
    return db


_SIB_ROWS = [
    ("1" * 32, "TFMX/Chris Huelsbeck/mdat.denny-level4"),
    ("2" * 32, "TFMX/Chris Huelsbeck/mdat.denny-title"),
    ("3" * 32, "TFMX/Chris Huelsbeck/smpl.denny-level4"),     # companion: not counted
    ("4" * 32, "SidMon 2/Florian Strauch/ice-runner tune1.sid2"),
    ("5" * 32, "SidMon 2/Florian Strauch/ice-runner tune2.sid2"),
    ("6" * 32, "Delitracker Custom/Someone/cust.slip-stream"),
    ("7" * 32, "Richard Joseph/Richard Joseph/cannon fodder-ingame 1.sng"),
    ("8" * 32, "Richard Joseph/Richard Joseph/cannon fodder-ingame 2.sng"),
    ("9" * 32, "OctaMED MMD1/XTC/zero-g demo 1.mmd1"),
    ("a" * 32, "OctaMED MMD1/XTC/zero-g demo 2.mmd1"),
    ("b" * 32, "David Whittaker/David Whittaker/turrican - level 1.dw"),
    ("c" * 32, "David Whittaker/David Whittaker/turrican - level 2.dw"),
    ("d" * 32, "TFMX/Other/mdat.denny-level4"),               # other dir: separate
]


@pytest.fixture
def sibs(tmp_path, monkeypatch):
    db = _index(tmp_path, _SIB_ROWS)
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    monkeypatch.setattr(sm, "_siblings_cache", None)
    return sm.modland_filename_siblings()


def test_sibling_index_keeps_only_multi_file_groups(sibs):
    key = sm._sibling_key
    assert sibs[key("TFMX/Chris Huelsbeck", "denny")] is False       # parts differ
    assert sibs[key("SidMon 2/Florian Strauch", "ice")] is True      # "runner …"
    assert sibs[key("Richard Joseph/Richard Joseph", "cannon fodder")] is True
    assert key("Delitracker Custom/Someone", "slip") not in sibs      # a lone file
    assert key("TFMX/Other", "denny") not in sibs                     # per dir


def test_sibling_index_is_cached_per_index_file(sibs, monkeypatch):
    calls = []
    real = sm._connect_ro
    monkeypatch.setattr(sm, "_connect_ro", lambda db: calls.append(1) or real(db))
    assert sm.modland_filename_siblings() is sibs
    assert calls == []


@pytest.mark.parametrize("path,title,expect", [
    ("TFMX/Chris Huelsbeck/mdat.denny-level4", "denny-level4", "Denny"),
    ("Delitracker Custom/Someone/cust.slip-stream", "slip-stream", None),       # singleton
    ("SidMon 2/Florian Strauch/ice-runner tune2.sid2", "ice-runner tune2", None),
    ("OctaMED MMD1/XTC/zero-g demo 1.mmd1", "zero-g demo 1", None),
    ("Richard Joseph/Richard Joseph/cannon fodder-ingame 2.sng",
     "cannon fodder-ingame 2", "Cannon Fodder"),                             # part word
    ("David Whittaker/David Whittaker/turrican - level 1.dw",
     "turrican - level 1", "Turrican"),                                       # spaced dash
    ("TFMX/Other/mdat.denny-level4", "denny-level4", None),                   # alone there
])
def test_title_from_file_name_needs_sibling_evidence(sibs, path, title, expect):
    d, _, fn = path.rpartition("/")
    assert sm.filename_game(fn, title, title_is_filename=True,
                            siblings=sibs, modland_dir=d) == expect


def test_without_the_sibling_index_the_relaxed_path_refuses():
    assert sm.filename_game("mdat.denny-level4", "denny-level4", title_is_filename=True) is None
    # The strict gate (title == part) needs no siblings.
    assert sm.filename_game("mdat.denny-level4", "level4") == "Denny"


@pytest.mark.parametrize("filename,title,flag,expect", [
    ("Music History (Commando -2).smus", "Music History (Commando -2)", True, None),
    ("Bill's Tomato Game (Psych-Out).mod", "Out)", False, None),
    ("Stoned[big-mix].xm", "mix]", False, None),
    ("Emergency (Uh-Oh).mod", "Oh)", False, None),
    ("turrican (v2)-intro.mod", "intro", False, "Turrican (V2)"),
])
def test_a_dash_inside_brackets_never_splits_a_game(filename, title, flag, expect):
    split = sm._game_part(filename)
    sib = {sm._sibling_key("D", split[0]): False}               # evidence present
    assert sm.filename_game(filename, title, title_is_filename=flag,
                            siblings=sib, modland_dir="D") == expect


def test_a_stamped_unbalanced_guess_is_withdrawn():
    mp = sm.parse_modland_path("Protracker/Someone/Emergency (Uh-Oh).mod")
    t = {"id": "e", "title": "Oh)", "format": "ProTracker", "genre": [],
         "album": "Emergency (Uh", "album_source": fa.SOURCE_MODLAND_FILENAME}
    assert sm.album_update_for(t, mp) == {"album": "", "album_source": None}


@pytest.mark.parametrize("path,game", [
    ("IFF-SMUS/- unknown/WELCOME.alt/WELCOME.smus", None),
    ("IFF-SMUS/- unknown/welcome.alt2/welcome.smus", None),
    ("IFF-SMUS/- unknown/cameotune.alt/cameotune.smus", None),
    ("IFF-SMUS/- unknown/The Sign of the Death/the sign of the death.alt.smus", None),
    ("IFF-SMUS/Someone/Street Sports Basketball/sports.smus", "Street Sports Basketball"),
    ("IFF-SMUS/Someone/Running Man/title.smus", "Running Man"),
    ("Protracker/Author/Turrican/turrican.mod", None),
    ("Nintendo SPC/Koji Kondo/Super Mario World/01 - title.spc", "Super Mario World"),
])
def test_alternate_version_dirs_are_not_games(path, game):
    assert sm.parse_modland_path(path).game == game


# ── apply: sibling gate end to end + the md5 identity precondition ───────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    db = _index(tmp_path, _SIB_ROWS + [
        ("e" * 32, "Protracker/Jester/coop-Dalezy/Stardust Memories/tune.mod")])
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    monkeypatch.setattr(sm, "_siblings_cache", None)
    monkeypatch.setattr(sm, "_WITHDRAW_MIN_INDEX_ROWS", 1)
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    sm._status["applying"] = False
    sm._last_auto_sig = None
    return store, monkeypatch


def _add(store, tid, md5, **kw):
    t = {"id": tid, "path": f"/m/{tid}", "title": "", "artist": "", "album": "",
         "format": "TFMX", "genre": ["Amiga"], "file_md5": md5}
    t.update(kw)
    store.upsert_track(t)


async def test_apply_uses_siblings_and_withdraws_an_old_lone_guess(env):
    store, _mp = env
    _add(store, "denny", "1" * 32, title="denny-level4")
    # Stamped by the pre-round-2 gate (no sibling evidence): withdrawn now.
    _add(store, "slip", "6" * 32, title="slip-stream", album="Slip",
         album_source=fa.SOURCE_MODLAND_FILENAME)
    # A user-edited album is never touched.
    _add(store, "mine", "6" * 32 + "", title="slip-stream")
    store.update_track_fields("mine", {"file_md5": "6" * 32, "album": "Slip",
                                       "album_source": fa.SOURCE_MODLAND_FILENAME,
                                       "user_edited": ["album"]})
    await sm.apply_to_library()
    d = store.get_track("denny")
    assert (d["album"], d["album_source"]) == ("Denny", fa.SOURCE_MODLAND_FILENAME)
    s = store.get_track("slip")
    assert (s["album"], s["album_source"]) == ("", None)
    assert store.get_track("mine")["album"] == "Slip"
    assert store.get_config(sm.APPLY_VERSION_CONFIG_KEY) == sm.MODLAND_APPLY_VERSION >= 3


async def test_a_file_swapped_in_during_the_join_gets_none_of_the_old_patch(env):
    store, monkeypatch = env
    _add(store, "t", "e" * 32, title="tune", format="ProTracker", genre=[])
    real = sm.collect_updates

    def collect_then_rescan(**kw):
        out = real(**kw)
        # A rescan swaps in a different tune (new md5, empty tags) meanwhile.
        store.upsert_tracks_batch([{"id": "t", "path": "/m/t", "title": "other",
                                    "artist": "", "album": "", "format": "ProTracker",
                                    "genre": [], "file_md5": "f" * 32}])
        return out
    monkeypatch.setattr(sm, "collect_updates", collect_then_rescan)
    await sm.apply_to_library()
    t = store.get_track("t")
    assert (t["artist"], t["album"], t.get("scene_path")) == ("", "", None)
    # The next apply joins the NEW md5 (no index row → nothing to write).
    joined = []

    def spy(**kw):
        joined.extend(t["id"] for t in kw["tracks"])
        return real(**kw)
    monkeypatch.setattr(sm, "collect_updates", spy)
    assert sm._last_auto_sig[0] != store.enrich_cursor()    # the rescan is still to join
    await sm.apply_to_library(auto=True)
    assert joined == ["t"]
    assert store.get_track("t")["artist"] == ""


async def test_an_unchanged_file_still_gets_the_whole_patch(env):
    store, _mp = env
    _add(store, "t", "e" * 32, title="tune", format="ProTracker", genre=[])
    await sm.apply_to_library()
    t = store.get_track("t")
    assert t["artist"] == "Jester & Dalezy"
    assert t["album"] == "Stardust Memories"
    assert t["scene_path"].endswith("tune.mod")


def test_the_md5_precondition_drops_the_whole_patch():
    store = TrackStore()
    store.upsert_track({"id": "x", "path": "/m/x", "file_md5": "b" * 32, "album": ""})
    upd = {"scene_path": "P/a/x.mod", "artist": "A", "album": "G",
           "album_source": fa.SOURCE_MODLAND}
    exp = {"album": "", "album_source": None, "artist": None, "file_md5": "a" * 32}
    assert fa._guard_item(store, "x", upd, exp) == (None, False)
    exp["file_md5"] = "b" * 32
    patch, album = fa._guard_item(store, "x", upd, exp)
    assert patch == upd and album is True
