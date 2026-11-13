# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Modland's ``_unknown`` author folder (every tune without a known composer
in the 2026-11 index — ``- unknown`` no longer occurs) is no composer credit:
the parser, the catalogue's credit, the md5-join apply, which clears the
"_unknown" artist an older apply stamped (also under a two-level family),
and the rescan carry, which no longer keeps it."""
from __future__ import annotations

import sqlite3

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import modland_catalog as mc
from soniqboom.core import scene_metadata as sm
from soniqboom.core.store import TrackStore


@pytest.mark.parametrize("rel, credit, game", [
    ("MDX/_unknown/1943/194301c.mdx", None, "1943"),
    ("Nintendo SPC/_unknown/Some Game/01 - title.spc", None, "Some Game"),
    ("Ad Lib/EdLib D00/_unknown/tune.d00", None, None),      # two-level family
    ("Protracker/_Unknown/x.mod", None, None),               # (any letter case)
    ("SNDH/Unknown_Composer/x.sndh", None, None),
    ("MDX/Hyperunknown/x.mdx", "Hyperunknown", None),        # a real handle
    ("Protracker/Lizardking/x.mod", "Lizardking", None),
])
def test_unknown_folder_is_no_credit(rel, credit, game):
    mp = sm.parse_modland_path(rel)
    assert sm.modland_credit(mp, None) == credit
    assert mp.game == game
    assert mc.credit_for(rel, None) == (credit or "")


def test_placeholder_credit_names_only_placeholder_folders():
    assert sm._placeholder_credit("MDX/_unknown/1943/a.mdx") == "_unknown"
    assert sm._placeholder_credit("Ad Lib/EdLib D00/_unknown/a.d00") == "_unknown"
    assert sm._placeholder_credit("Fmt/_unknown/coop-Someone/a.mod") == "_unknown & Someone"
    assert sm._placeholder_credit("Protracker/Lizardking/x.mod") is None
    assert sm._placeholder_credit("HVSC/MUSICIANS/x.sid") is None
    assert sm._placeholder_credit("x.mod") is None
    assert fa._generic("_unknown")                         # never a folder album


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
        ("a" * 32, "MDX/_unknown/1943/194301c.mdx"),
        ("b" * 32, "Ad Lib/EdLib D00/_unknown/tune.d00"),
        ("c" * 32, "MDX/_unknown/1943/194302.mdx"),
        ("d" * 32, "MDX/Glo/GLOTECNO.MDX"),
        ("e" * 32, "MDX/_unknown/1943/194303.mdx"),
    ])
    monkeypatch.setattr(sm, "_db_path", lambda: db)

    async def _refresh(ids):
        pass
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    sm._status["applying"] = False
    sm._last_auto_sig = None
    return store


def _add(store, tid, md5, **kw):
    t = {"id": tid, "path": f"/m/{tid}", "title": "t", "artist": "", "album": "",
         "format": "MDX", "genre": ["Chiptune"], "file_md5": md5}
    t.update(kw)
    store.upsert_track(t)


async def test_apply_clears_a_stamped_unknown_and_credits_nobody(env):
    store = env
    _add(store, "old", "a" * 32, artist="_unknown")                    # stamped by an older apply
    _add(store, "edl", "b" * 32, artist="_unknown", format="EdLib")   # …under a two-level family
    _add(store, "new", "c" * 32)                                        # never credited
    _add(store, "glo", "d" * 32)                                        # a real author
    _add(store, "mine", "e" * 32, artist="_unknown", user_edited=["artist"])
    await sm.apply_to_library()
    assert store.get_track("old")["artist"] == ""
    assert store.get_track("edl")["artist"] == ""
    assert store.get_track("new")["artist"] == ""
    assert store.get_track("glo")["artist"] == "Glo"
    assert store.get_track("mine")["artist"] == "_unknown"             # the user's edit stays
    assert store.get_track("old")["scene_path"] == "MDX/_unknown/1943/194301c.mdx"
    assert store.get_track("old")["album"] == "1943"                   # the game dir still counts


def test_a_rescan_no_longer_carries_the_unknown_artist():
    from soniqboom.core.store import _carry_enrichment
    old = {"id": "x", "artist": "_unknown", "file_md5": "a" * 32,
           "scene_path": "MDX/_unknown/1943/194301c.mdx", "format": "MDX"}
    new = {"id": "x", "artist": "", "file_md5": "a" * 32, "format": "MDX"}
    _carry_enrichment(old, new)
    assert not new.get("artist")
    old2 = dict(old, artist="Glo", scene_path="MDX/Glo/GLOTECNO.MDX")
    new2 = {"id": "x", "artist": "", "file_md5": "a" * 32, "format": "MDX"}
    _carry_enrichment(old2, new2)
    assert new2["artist"] == "Glo"                                     # a real credit is carried


def test_the_versions_were_bumped():
    assert sm.MODLAND_APPLY_VERSION == 8          # the apply re-runs once on old libraries
    assert mc.RULES_VERSION >= 7                  # the catalogue re-derives its tunes
