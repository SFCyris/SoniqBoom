# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub #13 — Modland game → album (exact-MD5 matches only).

* the game dir of ``Format/Author/<Game>/file`` → ``album_source="modland"``;
* the two-level format-dir author bug (``Ad Lib/EdLib Packed/Drax/…``);
* the gated ``<game>-<part>`` file name for tracker/Amiga formats →
  ``"modland-filename"`` (only when the local title equals ``<part>``);
* fill-only (a tag / user edit is never overwritten; a folder album is
  upgraded), CAS on write, one cache invalidation, the ``modland_filename_game``
  switch.

Uses a throw-away sqlite index + an in-memory TrackStore; nothing touches the
real data dir (``_db_path`` and the cache invalidation are monkeypatched)."""
from __future__ import annotations

import sqlite3

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import scene_metadata as sm
from soniqboom.core.store import TrackStore


# ── path grammar ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,fmt_dirs,credit,game", [
    ("Nintendo SPC/Yuka Tsujiyoko/Fire Emblem - Monshou No Nazo/132 - conspiracy.spc",
     ("Nintendo SPC",), "Yuka Tsujiyoko", "Fire Emblem - Monshou No Nazo"),
    ("Playstation Sound Format/Motoi Sakuraba/coop-Shinji Tamura/Tales Of Destiny/23 x.minipsf",
     ("Playstation Sound Format",), "Motoi Sakuraba & Shinji Tamura", "Tales Of Destiny"),
    # Two-level format dir: the author is one level deeper (the old parser
    # credited "EdLib Packed & Metal").
    ("Ad Lib/EdLib Packed/Drax/coop-Metal/lollypop - title1.edl",
     ("Ad Lib", "EdLib Packed"), "Drax & Metal", None),
    ("Spectrum/Sound Tracker Pro 2/- unknown/spacedoc.stp2",
     ("Spectrum", "Sound Tracker Pro 2"), None, None),
    ("Video Game Music/Sega Megadrive/Chris Huelsbeck/Turrican/09 stage 5.vgz",
     ("Video Game Music", "Sega Megadrive"), "Chris Huelsbeck", "Turrican"),
    # "coop X" / "coop - X" spellings are credits, not games.
    ("Protracker/Ian Howe/coop Alastair Lindsay/tempest 2000 - temp20.mod",
     ("Protracker",), "Ian Howe & Alastair Lindsay", None),
    ("Quartet ST/DNA/coop - Exarch/Necromunda/P5 Cybernetik.4v",
     ("Quartet ST",), "DNA & Exarch", "Necromunda"),
    # Disclaimers, containers, per-module dirs, mirrors — never a game.
    ("Protracker/- unknown/not by Heatbeat/m energy.mod", ("Protracker",), None, None),
    ("IFF-SMUS/Mark Riley/Instruments/Sharp-Strings-PP.instr", ("IFF-SMUS",), "Mark Riley", None),
    ("MDX/- unknown/Mixed/ts17.mdx", ("MDX",), None, None),
    ("Delitracker Custom/TSM/melody/cust.melody", ("Delitracker Custom",), "TSM", None),
    ("HVSC/DEMOS/UNKNOWN/Sanctus.sid", ("HVSC",), None, None),
    # A deeper tail is ambiguous (category / game / part) → refused.
    ("SNDH/Mad_Max/Demos/Best_In_Galaxy/Gremlin_Music_Demo.sndh", ("SNDH",), "Mad_Max", None),
    # Unknown author, game still valid.
    ("Nintendo SPC/- unknown/On The Ball/on the ball (07).spc", ("Nintendo SPC",), None, "On The Ball"),
])
def test_parse_modland_path(path, fmt_dirs, credit, game):
    mp = sm.parse_modland_path(path)
    assert mp.fmt_dirs == fmt_dirs
    assert sm.author_credit(mp) == credit
    assert mp.game == game


def test_legacy_parse_helper_keeps_its_shape():
    assert sm._parse_modland_path("Ad Lib/EdLib Packed/Drax/coop-Metal/x.edl") == \
        ("Ad Lib/EdLib Packed", "Drax & Metal")
    assert sm._parse_modland_path("Protracker/- unknown/x.mod") == ("Protracker", None)


def test_game_dir_display_name():
    assert sm.game_album_name("Songs_That_Make_U_Go_Mmh2") == "Songs That Make U Go Mmh2"
    assert sm.game_album_name("Fire Emblem - Monshou No Nazo") == "Fire Emblem - Monshou No Nazo"
    assert sm.game_album_name("blue monday") == "Blue Monday"


# ── the gated file-name game ─────────────────────────────────────────────────

@pytest.mark.parametrize("filename,title,expect", [
    ("gold of the aztecs-intro.dw", "intro", "Gold of the Aztecs"),     # the #13 case
    ("gold of the aztecs-intro.dw", " Intro ", "Gold of the Aztecs"),
    ("gold of the aztecs-ingame 2.dw", "ingame2", "Gold of the Aztecs"),
    ("blockhead ii - actualblockingame.mod", "actualblockingame", "Blockhead II"),
    # Negative: the title is not the part after the dash → refuse.
    ("4-mat.mod", "4-mat", None),
    ("pop-corn.mod", "Pop-Corn", None),
    ("totalrecall-ingame 2.mod", "Total Recall", None),
    ("gold of the aztecs-intro.dw", "gold of the aztecs-intro", None),
    # Structural refusals.
    ("4-mat.mod", "mat", None),                     # game "4" — too short
    ("1990-1991.mod", "1991", None),                # numeric game
    ("a-b-c.mod", "c", None),                       # more than one dash
    ("tune.mod", "tune", None),                     # no dash
])
def test_filename_game_gate(filename, title, expect):
    assert sm.filename_game(filename, title) == expect


def test_smart_title():
    assert sm.smart_title("gold of the aztecs") == "Gold of the Aztecs"
    assert sm.smart_title("the lost vikings") == "The Lost Vikings"
    assert sm.smart_title("mortal kombat ii - the end") == "Mortal Kombat II - The End"
    assert sm.smart_title("2ndsamurai") == "2ndsamurai"          # no "2Ndsamurai"
    assert sm.smart_title("R-Type") == "R-Type"                  # has capitals → kept


# ── fill-only decision ───────────────────────────────────────────────────────

_WHITTAKER = sm.parse_modland_path(
    "David Whittaker/David Whittaker/gold of the aztecs-intro.dw")
_SPC = sm.parse_modland_path("Nintendo SPC/Koji Kondo/Super Mario World/01 - title.spc")


def _amiga(**kw):
    t = {"id": "a", "title": "intro", "format": "David Whittaker",
         "genre": ["Amiga", "Module"], "album": "", "album_source": None}
    t.update(kw)
    return t


def test_album_update_fills_empty_album():
    # the file-name guess "Gold of the Aztecs", spelled as the title lists spell it
    assert sm.album_update_for(_amiga(), _WHITTAKER) == {
        "album": "The Gold of the Aztecs", "album_source": "modland-filename"}
    assert sm.album_update_for({"id": "s", "title": "x", "format": "SPC", "album": ""}, _SPC) == {
        "album": "Super Mario World", "album_source": "modland"}


def test_album_update_never_overwrites_tag_or_user_edit():
    assert sm.album_update_for(_amiga(album="Real Album"), _WHITTAKER) is None
    assert sm.album_update_for(_amiga(album="Header Game", album_source="tag"), _WHITTAKER) is None
    assert sm.album_update_for(_amiga(album="Mine", user_edited=["album"]), _WHITTAKER) is None
    # user-edited EMPTY album (the user cleared it on purpose) stays empty too
    assert sm.album_update_for(_amiga(album="", user_edited=["album"]), _WHITTAKER) is None


def test_album_update_upgrades_folder_and_withdraws_own_stamp():
    assert sm.album_update_for(_amiga(album="Gold_of_the_Aztecs dir", album_source="folder"),
                               _WHITTAKER)["album_source"] == "modland-filename"
    # switched off → our own stamp is withdrawn, a folder album is left alone
    assert sm.album_update_for(_amiga(album="Gold of the Aztecs", album_source="modland-filename"),
                               _WHITTAKER, use_filename=False) == {"album": "", "album_source": None}
    assert sm.album_update_for(_amiga(album="Some Dir", album_source="folder"),
                               _WHITTAKER, use_filename=False) is None


def test_filename_rule_only_for_tracker_and_amiga_formats():
    t = {"id": "x", "title": "intro", "format": "SNDH", "genre": ["Chiptune"], "album": ""}
    assert sm.album_update_for(t, _WHITTAKER) is None


# ── collect + apply against a temp index and an in-memory store ──────────────

def _index(tmp_path, rows):
    db = tmp_path / "modland.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.executemany("INSERT INTO mods VALUES (?,?)", rows)
    con.commit()
    con.close()
    return db


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    db = _index(tmp_path, [
        ("a" * 32, "David Whittaker/David Whittaker/gold of the aztecs-intro.dw"),
        ("b" * 32, "Nintendo SPC/Koji Kondo/Super Mario World/01 - title.spc"),
        ("c" * 32, "Ad Lib/EdLib Packed/Drax/coop-Metal/lollypop - title1.edl"),
        ("d" * 32, "Protracker/4-Mat/4-mat.mod"),
        ("e" * 32, "Nintendo SPC/Koji Kondo/Super Mario World/02 - overworld.spc"),
    ])
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    calls = []
    async def _refresh(ids):
        calls.append(1)
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    sm._status["applying"] = False
    return store, calls


def _add(store, tid, md5, **kw):
    t = {"id": tid, "path": f"/m/{tid}", "title": "", "artist": "", "album": "",
         "format": "ProTracker", "genre": [], "file_md5": md5}
    t.update(kw)
    store.upsert_track(t)


async def test_apply_fills_albums_fixes_author_and_invalidates_once(env):
    store, calls = env
    _add(store, "dw", "a" * 32, title="intro", format="David Whittaker", genre=["Amiga", "Module"])
    _add(store, "spc", "b" * 32, title="Title", format="SPC")
    _add(store, "edl", "c" * 32, title="title1", format="EdLib")
    _add(store, "mat", "d" * 32, title="4-mat")
    _add(store, "tagged", "e" * 32, title="Overworld", format="SPC",
         album="Super Mario World (tag)", album_source="tag")
    res = await sm.apply_to_library()
    assert res["error"] is None
    t = store.get_track
    assert (t("dw")["album"], t("dw")["album_source"]) == ("The Gold of the Aztecs", "modland-filename")
    assert (t("spc")["album"], t("spc")["album_source"]) == ("Super Mario World", "modland")
    assert t("edl")["artist"] == "Drax & Metal"          # not "EdLib Packed & Metal"
    assert t("dw")["artist"] == "David Whittaker"        # composer-named replayer = a credit
    assert t("mat")["album"] == ""                       # "4-mat" never split
    assert t("tagged")["album"] == "Super Mario World (tag)"   # tag kept
    assert res["last_apply"]["albums"] == 2
    assert calls == [1], "album/browse caches must be invalidated exactly once"
    # The album index sees the new album (Albums view / album: search).
    assert "dw" in store.filter_track_ids(album="The Gold of the Aztecs")
    # Idempotent: a second apply writes nothing and doesn't invalidate.
    res2 = await sm.apply_to_library()
    assert res2["last_apply"]["updated"] == 0 and calls == [1]


async def test_filename_switch_off_withdraws_on_next_apply(env, monkeypatch):
    store, _calls = env
    _add(store, "dw", "a" * 32, title="intro", format="David Whittaker", genre=["Amiga", "Module"])
    await sm.apply_to_library()
    assert store.get_track("dw")["album"] == "The Gold of the Aztecs"
    store.set_config(sm.MODLAND_FILENAME_CONFIG_KEY, False)
    await sm.apply_to_library()
    assert store.get_track("dw")["album"] == ""
    assert store.get_track("dw")["album_source"] is None


async def test_write_is_compare_and_set(env, monkeypatch):
    """An album the user sets between the (off-loop) collect and the write
    must survive — the write re-checks the snapshot it was computed from."""
    store, _calls = env
    _add(store, "spc", "b" * 32, title="Title", format="SPC")
    real = sm.collect_updates

    def racing_collect(**kw):
        out = real(**kw)
        # e.g. a rescan lands a real album tag in between (no user_edited mark,
        # so only the compare-and-set can protect it)
        store.update_track_fields("spc", {"album": "Tag Album"})
        return out
    monkeypatch.setattr(sm, "collect_updates", racing_collect)
    await sm.apply_to_library()
    assert store.get_track("spc")["album"] == "Tag Album"
    assert store.get_track("spc").get("album_source") is None


# ── review follow-ups ────────────────────────────────────────────────────────

@pytest.mark.parametrize("seg", ["- Misc", "Covers", "DISK", "Unknown Demo",
                                 "Digit Tracker", "Vgm", "SID"])
def test_modland_container_dirs_are_not_games(seg):
    assert sm.parse_modland_path(f"Megadrive GYM/- unknown/{seg}/x.gym").game is None


def test_real_numeric_game_dirs_survive():
    assert sm.parse_modland_path("MDX/- unknown/1943/194313c.mdx").game == "1943"


def test_filename_prefix_that_is_the_composer_is_refused():
    tiger = sm.parse_modland_path("Protracker/Tiger/tiger-tekknostuff.mod")
    t = {"id": "x", "title": "tekknostuff", "format": "ProTracker", "album": ""}
    assert sm.album_update_for(t, tiger) is None
    detio = sm.parse_modland_path("Protracker/Detio/detio - lost in a dream.mod")
    assert sm.album_update_for({**t, "title": "lost in a dream"}, detio) is None


def test_user_retitled_track_keeps_its_file_name_album():
    t = _amiga(title="My Better Title", album="Gold of the Aztecs",
               album_source="modland-filename", user_edited=["title"])
    assert sm.album_update_for(t, _WHITTAKER) is None
    # …but switching the option off still withdraws it
    assert sm.album_update_for(t, _WHITTAKER, use_filename=False) == \
        {"album": "", "album_source": None}


def test_smart_title_platform_tokens():
    assert sm.smart_title("sleepwalker (pc)") == "Sleepwalker (PC)"
    assert sm.smart_title("dennis aga") == "Dennis AGA"


def test_status_never_creates_an_index_file(tmp_path, monkeypatch):
    db = tmp_path / "scene" / "modland.sqlite"
    db.parent.mkdir()
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    sm._status["index_rows"] = 0
    assert sm.has_index() is False
    sm.status()
    assert not db.exists(), "status() must not create an empty index"
    db.write_bytes(b"")
    assert sm.has_index() is False                      # 0-byte file ≠ an index


def test_modland_name_keys_from_index(tmp_path, monkeypatch):
    db = _index(tmp_path, [
        ("a" * 32, "Ad Lib/EdLib Packed/Drax/coop-Metal/x.edl"),
        ("b" * 32, "Protracker/Jester/y.mod"),
        ("c" * 32, "HVSC/DEMOS/UNKNOWN/z.sid"),
    ])
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    sm._name_sets_cache = None
    fmts, authors = sm.modland_name_keys()
    assert {"ad lib", "edlib packed", "protracker"} <= fmts
    assert {"drax", "metal", "jester"} <= authors and "demos" not in authors
    assert sm.modland_name_keys() == (fmts, authors)    # cached


async def test_wrong_legacy_credits_are_corrected_but_edits_and_real_credits_kept(env):
    store, _calls = env
    _add(store, "edl", "c" * 32, title="title1", format="EdLib", artist="EdLib Packed & Metal")
    _add(store, "mine", "c" * 32, title="title1", format="EdLib", artist="EdLib Packed & Metal",
         user_edited=["artist"])
    _add(store, "real", "c" * 32, title="title1", format="EdLib", artist="Somebody Else")
    await sm.apply_to_library()
    assert store.get_track("edl")["artist"] == "Drax & Metal"
    assert store.get_track("mine")["artist"] == "EdLib Packed & Metal"   # hand edit wins
    assert store.get_track("real")["artist"] == "Somebody Else"          # not the old credit


async def test_cleared_artist_the_user_chose_is_not_refilled(env):
    store, _calls = env
    _add(store, "spc", "b" * 32, title="Title", format="SPC", artist="", user_edited=["artist"])
    await sm.apply_to_library()
    assert store.get_track("spc")["artist"] == ""
