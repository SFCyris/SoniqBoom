# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-3 enrichment fixes around the store, the extractor and repair:

* search is accent-insensitive ("oorni" finds "Lasse Öörni") without
  confusing distinct letters of other scripts (か / が);
* ``game:`` ignores a leading "The" on either side;
* an SPC game name the Modland apply completed (32-byte ID666 cut) survives
  a re-extract ("Read game names") and a rescan while the md5 is unchanged;
* NSF / GBS ripper-placeholder artists ("<?>") are not stored and give way
  to the Modland credit;
* the header-game backfill finds its candidates through the format index."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import metadata, repair
from soniqboom.core import scene_metadata as sm
from soniqboom.core.store import TrackStore, _carry_enrichment, fold_token


def _mk(tid, **kw):
    t = {"id": tid, "path": f"/m/{tid}.mod", "title": "", "artist": "", "album": "",
         "album_artist": "", "format": "ProTracker", "genre": [], "added_at": 1}
    t.update(kw)
    return t


# ── r3-enr-10: accent-folded search tokens ───────────────────────────────────

@pytest.fixture
def people():
    s = TrackStore()
    s.upsert_tracks_batch([
        _mk("oor", artist="Lasse Öörni", title="Tune"),
        _mk("bey", artist="Beyoncé", title="Halo"),
        _mk("bey2", artist="Beyonce", title="Other"),
        _mk("ode", artist="Martin Ødegaard", title="x"),
        _mk("ka", title="か"),
        _mk("ga", title="が"),
        _mk("nfd", artist="Beyoncé", title="Decomposed"),   # NFD-tagged é
    ])
    return s


@pytest.mark.parametrize("q,expect", [
    ("oorni", {"oor"}),
    ("öörni", {"oor"}),
    ("öö", {"oor"}),                               # prefix of the folded token
    ("beyonce", {"bey", "bey2", "nfd"}),
    ("beyoncé", {"bey", "bey2", "nfd"}),
    ("beyoncé", {"bey", "bey2", "nfd"}),     # NFD-typed query
    ("odegaard", {"ode"}),
    ("ødegaard", {"ode"}),
    ("か", {"ka"}),                                # dakuten is not an accent
    ("が", {"ga"}),
])
def test_search_folds_accents(people, q, expect):
    assert people._resolve_query(q) == expect


def test_fold_token_leaves_non_latin_marks():
    assert fold_token("öörni") == "oorni"
    assert fold_token("straße") == "strasse"
    assert fold_token("й") == "й"                  # Cyrillic short i keeps its breve
    assert fold_token("ascii") == "ascii"


def test_folded_tokens_are_removed_with_the_track(people):
    people.update_track_fields("oor", {"artist": "Someone Else"})
    assert people._resolve_query("oorni") == set()
    assert "oorni" not in people._word_index and "öörni" not in people._word_index
    people.delete_track("bey")
    assert people._resolve_query("beyonce") == {"bey2", "nfd"}
    assert people.verify_indexes()["index_ok"]


# ── r3-enr-14: game: ignores a leading "The" ─────────────────────────────────

def test_game_prefix_ignores_a_leading_article():
    s = TrackStore()
    s.upsert_tracks_batch([
        _mk("ln3", title="The Last Ninja 3", format="SID"),      # SIDs: game in the title
        _mk("ln2", title="Last Ninja 2", format="SID"),
        _mk("gg", album="The Great Giana Sisters", album_source="modland", title="Intro"),
        _mk("gg2", album="Great Giana Sisters", album_source="songdb", title="Ingame"),
        _mk("ur", title="Uridium", format="SID"),
        _mk("other", title="Theme of Uridium", format="SID"),
    ])
    assert s._candidate_ids(game="last ninja") == {"ln2", "ln3"}
    assert s._candidate_ids(game="The Last Ninja") == {"ln2", "ln3"}
    assert s._candidate_ids(game="great giana") == {"gg", "gg2"}
    assert s._candidate_ids(game="the great giana sisters") == {"gg", "gg2"}
    assert s._candidate_ids(game="uridium") == {"ur"}
    assert s._candidate_ids(game="the") == {"ln3", "gg", "other"}   # plain prefix


# ── r3-enr-4: SPC Modland completion survives re-extracts and rescans ───────

CUT = "Street Fighter II - The World Wa"
FULL = "Street Fighter II - The World Warrior"


def _completed(md5="a" * 32):
    return {"id": "s", "path": "/m/sf2.spc", "format": "SPC", "album": FULL,
            "album_source": fa.SOURCE_MODLAND, "file_md5": md5, "artist": "Capcom",
            "scene_path": "Nintendo SPC/Capcom/Street Fighter II - The World Warrior/x.spc"}


def test_modland_completes_the_cut_name():
    mp = sm.parse_modland_path(_completed()["scene_path"])
    t = dict(_completed(), album=CUT, album_source=fa.SOURCE_TAG)
    assert sm.album_update_for(t, mp) == {"album": FULL, "album_source": fa.SOURCE_MODLAND}


def test_reextract_keeps_the_completion():
    from soniqboom.core.repair import _changed_fields
    assert _changed_fields(_completed(), {"album": CUT, "album_source": "tag"}) == {}
    # a different file (md5 known and changed) → the header wins again
    assert _changed_fields(_completed(), {"album": CUT, "album_source": "tag",
                                          "file_md5": "b" * 32})["album"] == CUT
    # an unrelated header game still wins over a derived album
    got = _changed_fields(_completed(), {"album": "Final Fight", "album_source": "tag"})
    assert got == {"album": "Final Fight", "album_source": "tag"}


def test_rescan_keeps_the_completion_while_the_md5_is_unchanged():
    new = {"id": "s", "path": "/m/sf2.spc", "format": "SPC", "album": CUT,
           "album_source": "tag", "file_md5": "a" * 32}
    _carry_enrichment(_completed(), new)
    assert (new["album"], new["album_source"]) == (FULL, fa.SOURCE_MODLAND)
    changed = dict(new, album=CUT, album_source="tag", file_md5="c" * 32)
    _carry_enrichment(_completed(), changed)
    assert (changed["album"], changed["album_source"]) == (CUT, "tag")


def test_short_spc_names_and_other_formats_are_not_completions():
    assert not fa.spc_name_completes("Super Metroid (J)", "Super Metroid", "SPC")
    assert not fa.spc_name_completes(FULL, CUT, "NSF")
    assert fa.spc_name_completes(FULL, CUT, "spc")
    new = {"id": "s", "format": "SPC", "album": "Super Metroid", "album_source": "tag",
           "file_md5": "a" * 32}
    _carry_enrichment(dict(_completed(), album="Super Metroid (Japan)"), new)
    assert new["album"] == "Super Metroid"


# ── r3-enr-5: placeholder artists ────────────────────────────────────────────

def _pad(s: bytes, n: int) -> bytes:
    return s[:n] + b"\x00" * (n - len(s[:n]))


def _nsf(tmp_path: Path, artist: bytes) -> Path:
    b = bytearray(0x80)
    b[0:5] = b"NESM\x1a"
    b[5], b[6] = 1, 3
    b[0x0E:0x2E] = _pad(b"Mega Man 2", 32)
    b[0x2E:0x4E] = _pad(artist, 32)
    p = tmp_path / "mm2.nsf"
    p.write_bytes(bytes(b) + b"\x00" * 64)
    return p


def _gbs(tmp_path: Path, artist: bytes) -> Path:
    b = bytearray(0x70)
    b[0:3] = b"GBS"
    b[3] = 1
    b[0x10:0x30] = _pad(b"Tetris", 32)
    b[0x30:0x50] = _pad(artist, 32)
    p = tmp_path / "t.gbs"
    p.write_bytes(bytes(b) + b"\x00" * 64)
    return p


@pytest.mark.parametrize("make", [_nsf, _gbs])
@pytest.mark.parametrize("artist", [b"<?>", b"???", b"Unknown", b"\x01\x02garbage"])
def test_placeholder_header_artist_is_not_stored(tmp_path, make, artist):
    d = metadata._extract_gme(make(tmp_path, artist), "x")
    assert "artist" not in d and d["album"] in ("Mega Man 2", "Tetris")


def test_real_header_artist_is_kept(tmp_path):
    assert metadata._extract_gme(_nsf(tmp_path, b"Takashi Tateishi"), "x")["artist"] == \
        "Takashi Tateishi"


@pytest.fixture
def ml_env(tmp_path, monkeypatch):
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)
    db = tmp_path / "modland.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.execute("INSERT INTO mods VALUES (?,?)",
                ("a" * 32, "Nintendo Sound Format/Rezon/over horizon.nsf"))
    con.commit()
    con.close()
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    return store


@pytest.mark.parametrize("artist,locked,expect", [
    ("<?>", False, "Rezon"),
    ("", False, "Rezon"),
    ("<?>", True, "<?>"),                          # a hand-set value is never touched
    ("Someone", False, "Someone"),
])
def test_placeholder_artist_takes_the_modland_credit(ml_env, artist, locked, expect):
    t = _mk("n", path="/m/over.nsf", format="NSF", artist=artist, file_md5="a" * 32)
    if locked:
        t["user_edited"] = ["artist"]
    ml_env.upsert_track(t)
    _matched, batch, expect_map = sm.collect_updates(tracks=[ml_env.get_track("n")])
    upd = dict(batch).get("n", {})
    assert upd.get("artist", artist) == expect
    if "artist" in upd:
        assert expect_map["n"]["artist"] == artist          # CAS precondition = stored value


def test_apply_version_was_bumped_for_the_placeholder_heal():
    assert sm.MODLAND_APPLY_VERSION >= 4


# ── r3-enr-6: backfill candidates from the format index ─────────────────────

def test_backfill_candidates_come_from_the_format_index(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    s.upsert_tracks_batch([
        _mk("nsf", path="/m/a.nsf", format="NSF"),
        _mk("nsfe", path="/m/b.nsfe", format="NSFe", album="X", album_source="folder"),
        _mk("spc", path="/m/c.spc", format="SPC", album="Y", album_source="modland"),
        _mk("vgz", path="/m/pack.zip::d.vgz", format="VGZ"),
        _mk("gbs", path="/m/e.gbs", format="GBS", album="Tetris", album_source="tag"),
        _mk("edit", path="/m/f.vgm", format="VGM", user_edited=["album"],
            game_by_tag=None),                  # its header name read already
        _mk("mp3", path="/m/g.mp3", format="MP3"),
        _mk("rem", path="ftp://h/share:/h.nsf", format="NSF"),
    ])
    walked = []
    monkeypatch.setattr(s, "all_tracks", lambda: walked.append(1) or list(s._tracks.values()))
    ids = [t["id"] for t in repair.find_album_backfill_candidates()]
    assert set(ids) == {"nsf", "nsfe", "spc", "vgz", "rem"}
    assert ids == sorted(ids, key=lambda i: s.get_track(i)["path"])
    assert {t["id"] for t in repair.find_album_backfill_candidates(include_remote=False)} == \
        {"nsf", "nsfe", "spc", "vgz"}
    assert walked == []                                   # no full-library pass
