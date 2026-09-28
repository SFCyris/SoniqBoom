# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub #13 — derived albums across the rest of the system:

* ``store._carry_enrichment`` keeps a derived album across a rescan (a real
  tag that appears later wins);
* ``repair._changed_fields`` never overwrites a derived or user-edited album;
* ``album_source`` round-trips through the AOF replay;
* ``game:`` is an alias of ``album:`` in the advanced search parser, and the
  shuffle endpoint resolves it through the same parser;
* the admin settings GET/PUT round-trip for the five new keys, incl. the
  immediate apply / revert of the two album options."""
from __future__ import annotations

import json

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import scene_metadata as sm
from soniqboom.core.store import TrackStore, _carry_enrichment


async def _no_refresh(_ids):
    """Stand-in for ``folder_album.refresh_album_caches`` (which would touch
    the real data dir's browse cache file)."""
    return None


def _fresh(**kw):
    """A freshly re-extracted track (what a rescan upserts)."""
    t = {"id": "t1", "path": "/m/Game/dw.intro", "title": "intro", "artist": "",
         "album": "", "album_source": None, "file_md5": "a" * 32, "format": "David Whittaker"}
    t.update(kw)
    return t


# ── rescan carry ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("src", ["modland", "modland-filename", "folder"])
def test_derived_album_survives_a_rescan(src):
    old = _fresh(album="Gold of the Aztecs", album_source=src)
    new = _fresh()
    _carry_enrichment(old, new)
    assert (new["album"], new["album_source"]) == ("Gold of the Aztecs", src)


def test_real_tag_that_appears_later_wins():
    old = _fresh(album="Folder Name", album_source="folder")
    new = _fresh(album="Header Game", album_source="tag")
    _carry_enrichment(old, new)
    assert (new["album"], new["album_source"]) == ("Header Game", "tag")


def test_modland_album_dropped_when_the_file_changed():
    old = _fresh(album="Gold of the Aztecs", album_source="modland-filename")
    new = _fresh(file_md5="b" * 32)
    _carry_enrichment(old, new)
    assert new["album"] == "" and new["album_source"] is None


def test_plain_tag_album_is_not_carried():
    """No provenance ⇒ the file's own album; a rescan may refresh it (to empty)."""
    old = _fresh(album="Old Tag")
    new = _fresh()
    _carry_enrichment(old, new)
    assert new["album"] == ""


def test_user_edited_album_wins_over_carry():
    old = _fresh(album="Mine", album_source="folder", user_edited=["album"])
    new = _fresh()
    _carry_enrichment(old, new)
    assert new["album"] == "Mine"


def test_rescan_through_upsert_batch_keeps_it_and_is_idempotent():
    s = TrackStore()
    s.upsert_tracks_batch([_fresh()])
    s.update_track_fields("t1", {"album": "Gold of the Aztecs", "album_source": "modland-filename"})
    s.upsert_tracks_batch([_fresh()])                      # the rescan
    assert s.get_track("t1")["album"] == "Gold of the Aztecs"
    assert "t1" in s.filter_track_ids(album="Gold of the Aztecs")
    s.upsert_tracks_batch([dict(s.get_track("t1"))])       # AOF replay of the merged record
    assert s.get_track("t1")["album_source"] == "modland-filename"


# ── repair ───────────────────────────────────────────────────────────────────

def test_repair_keeps_a_derived_album_when_the_file_has_none():
    from soniqboom.core.repair import _changed_fields
    old = _fresh(album="Gold of the Aztecs", album_source="folder", title="intr�")
    delta = _changed_fields(old, _fresh(title="intro"))
    assert delta == {"title": "intro"}


def test_repair_lets_a_real_tag_win_and_stamps_its_source():
    from soniqboom.core.repair import _changed_fields
    old = _fresh(album="Folder Name", album_source="folder")
    delta = _changed_fields(old, _fresh(album="Header Game", album_source="tag"))
    assert delta == {"album": "Header Game", "album_source": "tag"}


def test_repair_never_touches_user_edited_fields():
    from soniqboom.core.repair import _changed_fields
    old = _fresh(album="Mine", title="My Title", user_edited=["album", "title"])
    assert _changed_fields(old, _fresh(album="Tag", title="File Title")) == {}


# ── persistence ──────────────────────────────────────────────────────────────

def test_album_source_round_trips_through_aof_replay(tmp_path):
    from soniqboom.core.persistence import replay_aof
    s = TrackStore()
    records = []
    s._aof_append = lambda op, **kw: records.append({"op": op, "ts": 0, **kw})
    s.upsert_tracks_batch([_fresh()])
    s.update_track_fields_batch([("t1", {"album": "Gold of the Aztecs",
                                         "album_source": "modland-filename"})])
    (tmp_path / "library.aof").write_text("".join(json.dumps(r) + "\n" for r in records))
    state = {"tracks": {}}
    assert replay_aof(state, tmp_path) == 2
    t = state["tracks"]["t1"]
    assert (t["album"], t["album_source"]) == ("Gold of the Aztecs", "modland-filename")


# ── search ───────────────────────────────────────────────────────────────────

def test_game_operator_is_its_own_prefix_predicate():
    """``game:`` is a prefix match on album OR title; ``album:`` stays exact."""
    from soniqboom.api.search import _parse_advanced_query
    assert _parse_advanced_query('game:"Gold of the Aztecs"') == r"@game_tag:{Gold\ of\ the\ Aztecs}"
    assert _parse_advanced_query('album:"Gold of the Aztecs"') == r"@album_tag:{Gold\ of\ the\ Aztecs}"
    assert _parse_advanced_query("game:Turrican format:ProTracker") == \
        "@game_tag:{Turrican} @format:{PROTRACKER}"
    assert _parse_advanced_query("game:x") is not None


def test_game_search_resolves_album_and_title_prefixes(monkeypatch):
    """The search endpoints and /api/tracks/shuffled share one parse path
    (``data._parse_tag_query``) that hands the ``game`` predicate to the store."""
    from soniqboom.api.search import _parse_advanced_query
    from soniqboom.core.data import _parse_tag_query as query_kwargs
    s = TrackStore()
    s.upsert_tracks_batch([
        _fresh(id="a", album="Gold of the Aztecs", album_source="modland-filename"),
        _fresh(id="b", album="Gold of the Aztecs", album_source="folder"),
        _fresh(id="c", album="Turrican"),
        _fresh(id="d", album="", title="Gold of the Aztecs (intro)"),
    ])
    preds = query_kwargs(_parse_advanced_query('game:"Gold of the Aztecs"'))
    assert preds == {"game": "Gold of the Aztecs"}
    # "d" has no game; an Amiga module's title is not one (only a SID's /
    # Atari tune's is)
    assert set(s.filter_track_ids(**preds)) == {"a", "b"}
    assert set(s.filter_track_ids(**query_kwargs(_parse_advanced_query("game:gold")))) == \
        {"a", "b"}


# ── settings round-trip ──────────────────────────────────────────────────────

_KEYS = {"retro_album_from_folder": False, "modland_filename_game": True,
         "subsonic_folder_albums": True, "render_prewarm": True, "uade_vu_meters": True}


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    s = TrackStore()
    for target in ("soniqboom.core.store.get_store", "soniqboom.core.data.get_store"):
        monkeypatch.setattr(target, lambda: s)
    monkeypatch.setattr(sm, "modland_name_keys", lambda: (frozenset(), frozenset()))
    monkeypatch.setattr(sm, "_db_path", lambda: tmp_path / "no-index.sqlite")
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    fa._last_seq = None
    s.upsert_scan_dir("/m")
    s.upsert_tracks_batch([
        _fresh(id="g1", path="/m/Gold_of_the_Aztecs/dw.intro", artist="David Whittaker"),
        _fresh(id="g2", path="/m/Gold_of_the_Aztecs/dw.ingame", title="ingame",
               artist="David Whittaker"),
    ])
    return s


async def test_settings_defaults_and_round_trip(settings_env):
    from soniqboom.api import admin
    from soniqboom.api import subsonic
    got = await admin.get_settings()
    for k, default in _KEYS.items():
        assert got[k] is default, k
    # The Settings checkbox shows what Subsonic clients actually get.
    assert got["subsonic_folder_albums"] is subsonic._folder_albums_on(settings_env)
    flipped = {k: not v for k, v in _KEYS.items()}
    await admin.update_settings(flipped)
    got = await admin.get_settings()
    for k, v in flipped.items():
        assert got[k] is v, k
    # A body WITHOUT the keys (an old cached admin page) leaves them alone.
    await admin.update_settings({"scan_zips": True})
    got = await admin.get_settings()
    for k, v in flipped.items():
        assert got[k] is v, k


async def test_folder_option_applies_on_and_reverts_off(settings_env):
    from soniqboom.api import admin
    s = settings_env
    res = await admin.update_settings({"retro_album_from_folder": True})
    assert res["folder_albums_filled"] == 2
    assert s.get_track("g1")["album"] == "Gold of the Aztecs"
    # re-saving the same value is not a change → no second pass reported
    res = await admin.update_settings({"retro_album_from_folder": True})
    assert "folder_albums_filled" not in res
    res = await admin.update_settings({"retro_album_from_folder": False})
    assert res["folder_albums_reverted"] == 2
    assert s.get_track("g1")["album"] == "" and s.get_track("g1")["album_source"] is None


async def test_modland_filename_option_off_reverts_only_its_albums(settings_env):
    from soniqboom.api import admin
    s = settings_env
    s.update_track_fields("g1", {"album": "Gold of the Aztecs", "album_source": "modland-filename"})
    s.update_track_fields("g2", {"album": "Tag Album", "album_source": "tag"})
    res = await admin.update_settings({"modland_filename_game": False})
    assert res["modland_albums_reverted"] == 1
    assert s.get_track("g1")["album"] == ""
    assert s.get_track("g2")["album"] == "Tag Album"


async def test_conf_is_saved_before_the_album_pass_runs(settings_env, monkeypatch):
    """A concurrent Save must not be clobbered: the conf-file part of the
    request is persisted BEFORE the (long) album pass awaits."""
    from soniqboom.api import admin
    from soniqboom.config import load_local_conf
    seen = {}

    async def slow_pass(**kw):
        seen["conf_mb"] = load_local_conf().get("remote_cache_max_mb")
        return {"updated": 0}
    monkeypatch.setattr(fa, "apply_folder_albums", slow_pass)
    monkeypatch.setattr("soniqboom.core.remote_cache.get_cache",
                        lambda: type("C", (), {"set_max_mb": lambda self, mb: None})())
    await admin.update_settings({"remote_cache_max_mb": 4321, "retro_album_from_folder": True})
    assert seen["conf_mb"] == 4321


async def test_failing_pass_is_reported_and_the_choice_kept(settings_env, monkeypatch):
    from soniqboom.api import admin

    async def boom(**kw):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(fa, "apply_folder_albums", boom)
    res = await admin.update_settings({"retro_album_from_folder": True})
    assert res["album_pass_error"] == "disk on fire"
    assert (await admin.get_settings())["retro_album_from_folder"] is True
