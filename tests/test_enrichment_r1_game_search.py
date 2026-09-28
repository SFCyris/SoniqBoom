# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub #13 enhancement — the ``game:`` search operator.

``game:uridium`` is a case-insensitive PREFIX match on the ``game`` field (a
retro track's game album, a modern file's GAME tag) or, for a SID / Atari ST
tune with no game, on its title — while ``album:`` stays exact.  One predicate in the store's
``_candidate_ids``, shared by /search, /search/quick, smart playlists and the
shuffled play order."""
from __future__ import annotations

import pytest

from soniqboom.api import search as search_api
from soniqboom.core.store import TrackStore


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: s, raising=False)
    s.upsert_tracks_batch([dict(t, path=f"/m/{t['id']}") for t in [
        {"id": "u2", "title": "Uridium 2", "album": "", "artist": "Steve Turner",
         "format": "SID", "added_at": 5},
        {"id": "u2l", "title": "Uridium 2 Loader", "album": "", "format": "SID", "added_at": 4},
        {"id": "ualb", "title": "Title", "album": "Uridium", "album_source": "folder",
         "format": "YM", "added_at": 3},
        {"id": "za1", "title": "Intro", "album": "Zombie Attack", "album_source": "tag",
         "format": "SPC", "added_at": 2},
        {"id": "za2", "title": "Stage 1", "album": "zombie attack", "album_source": "tag",
         "format": "SPC", "added_at": 1},
        {"id": "other", "title": "Paradroid", "album": "Hewson", "format": "SID", "added_at": 6},
        {"id": "mid", "title": "Remix of Uridium", "album": "", "format": "MP3", "added_at": 7},
    ]])
    return s


def _ids(rows):
    return {r["id"] for r in rows}


def test_store_game_predicate_prefix_on_album_or_title(store):
    assert store._candidate_ids(game="uridium") == {"u2", "u2l", "ualb"}
    assert store._candidate_ids(game="  URIDIUM   2 ") == {"u2", "u2l"}
    assert store._candidate_ids(game="zombie") == {"za1", "za2"}
    assert store._candidate_ids(game="nothing") == set()
    # combined with another predicate (intersection)
    assert store._candidate_ids(game="uridium", format_="SID") == {"u2", "u2l"}


def test_album_predicate_stays_exact(store):
    assert store._candidate_ids(album="zombie") == set()
    assert store._candidate_ids(album="Zombie Attack") == {"za1", "za2"}


def test_parse_emits_a_game_term_not_an_album_alias():
    assert search_api._parse_advanced_query("game:uridium") == "@game_tag:{uridium}"
    assert search_api._parse_advanced_query("album:uridium") == "@album_tag:{uridium}"
    # One parse path: every advanced query goes through data._parse_tag_query.
    from soniqboom.core.data import _parse_tag_query
    kw = _parse_tag_query(search_api._parse_advanced_query('game:"Uridium 2" format:SID'))
    assert kw == {"game": "Uridium 2", "format_": "SID"}


async def test_search_endpoints_resolve_game_prefix(store):
    assert _ids(await search_api.run_search_dicts("game:uridium")) == {"u2", "u2l", "ualb"}
    assert _ids(await search_api.run_search_dicts('game:"Uridium 2"')) == {"u2", "u2l"}
    assert _ids(await search_api.run_search_dicts("game:zombie")) == {"za1", "za2"}
    assert _ids(await search_api.run_search_dicts("album:zombie")) == set()
    models = await search_api.run_search("game:uridium")          # smart playlists
    assert {m.id for m in models} == {"u2", "u2l", "ualb"}
    quick = await search_api.quick_search(q="game:zombie", limit=8)
    assert {m.id for m in quick} == {"za1", "za2"}
    # free text after the operator still narrows
    assert _ids(await search_api.run_search_dicts("game:uridium loader")) == {"u2l"}


def test_game_prefix_follows_field_updates(store):
    store.update_track_fields("other", {"album": "Uridium Collection", "album_source": "folder"})
    assert "other" in store._candidate_ids(game="uridium")
    assert store.get_track("other")["game_source"] == "folder"
    store.update_track_fields("other", {"album": "", "album_source": None})
    assert "other" not in store._candidate_ids(game="uridium")
    assert store.get_track("other")["game"] == ""


def test_shuffled_order_uses_the_same_predicate(store):
    """/api/tracks/shuffled resolves ``q`` via search._parse_advanced_query +
    data._parse_tag_query and hands the kwargs to filter_track_ids."""
    from soniqboom.core.data import _parse_tag_query
    preds = _parse_tag_query(search_api._parse_advanced_query("game:uridium"))
    assert preds.get("game") == "uridium"
    assert set(store.filter_track_ids(**preds)) == {"u2", "u2l", "ualb"}



def test_game_matches_retro_games_only():
    """game: filters on the game metadata of retro tracks (the album a file
    header, the song database, Modland or the folder name gave it) and on the
    title of a SID / Atari ST tune with no game — never an MP3, and never a
    tracker or console track's title (a song name, not a game)."""
    s = TrackStore()
    s.upsert_tracks_batch([dict(t, path=f"/m/{t['id']}") for t in [
        {"id": "bd", "title": "mickey mouse", "album": "Mickey Mouse", "album_source": "songdb",
         "format": "Benn Daglish"},
        {"id": "abc", "title": "vaginal lubrication", "format": "ProTracker",
         "album": "Mickey's ABC - A Day at the Fair [cracktro]", "album_source": "songdb"},
        {"id": "sid", "title": "Mickey Mouse", "album": "", "format": "SID"},
        {"id": "ym", "title": "Mickey Tune", "album": "", "format": "YM"},
        # a plain album is no game: the SID's title still names it
        {"id": "sid_album", "title": "Mickey Mouse", "album": "Disney Classics", "format": "SID"},
        {"id": "mod", "title": "mickey_metal", "album": "", "format": "ProTracker"},
        {"id": "spc", "title": "Mickey's Theme", "album": "Magical Quest", "album_source": "tag",
         "format": "SPC"},
        {"id": "mp3", "title": "Mickey Mouse March", "album": "Disney's Greatest", "format": "MP3"},
        {"id": "mp3b", "title": "Intro", "album": "Mickey Mouse Clubhouse", "format": "MP3"},
        # a modern remix with a GAME tag (the file's own: game_source None)
        {"id": "remix", "title": "Mickey Mouse (Remix)", "album": "Remixes", "format": "FLAC",
         "game": "Mickey Mouse"},
    ]])
    assert s._candidate_ids(game="mickey") == {"bd", "abc", "sid", "ym", "sid_album", "remix"}
    assert s._candidate_ids(game="magical quest") == {"spc"}
    assert s._candidate_ids(game="disney") == set()
    t = s.get_track
    assert (t("bd")["game"], t("bd")["game_source"]) == ("Mickey Mouse", "songdb")
    assert (t("remix")["game"], t("remix").get("game_source")) == ("Mickey Mouse", None)
    assert (t("sid_album").get("game", ""), t("mp3b").get("game", "")) == ("", "")
