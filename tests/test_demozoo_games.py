"""Demozoo as a game-name source: the index keeps the games a composer's music
production is the soundtrack of, and the apply names a tune's game only when
the tune is named after it (a scene tune a later game reused names none)."""
import gzip

import pytest

from soniqboom.core import demozoo
from soniqboom.core.store import TrackStore


def _write_dump(tmp_path):
    """Composer 'moby' (releaser 300): "Rick Dangerous" (music 10) is the
    soundtrack of two Game productions (a demo version and the game), "Jack
    the Nipper" (11) of the homebrew game "Snake On A Plane", "Tune X" (12)
    of a Demo; "Mole Mayhem Finish" (13) of the game "Mole Mayhem V1.1";
    "Twin Tune" (14) of two different games.  Group 'crew' (900) authored
    music 15 of a game — a group is no composer."""
    dump = (
        "COPY public.demoscene_releaser (id, name, first_name, surname, is_group) FROM stdin;\n"
        "300\tmoby\tCara\tDoe\tf\n900\tcrew\t\\N\t\\N\tt\n\\.\n"
        "COPY public.demoscene_nick (id, releaser_id, name, abbreviation) FROM stdin;\n"
        "3000\t300\tmoby\t\\N\n9000\t900\tcrew\t\\N\n\\.\n"
        "COPY public.demoscene_nickvariant (id, nick_id, name) FROM stdin;\n\\.\n"
        "COPY public.demoscene_membership (id, member_id, group_id) FROM stdin;\n\\.\n"
        "COPY public.productions_production (id, title, supertype, release_date_date) FROM stdin;\n"
        "10\tRick Dangerous\tmusic\t1989-01-01\n"
        "11\tJack the Nipper\tmusic\t1986-01-01\n"
        "12\tTune X\tmusic\t1990-01-01\n"
        "13\tMole Mayhem Finish\tmusic\t1995-01-01\n"
        "14\tTwin Tune\tmusic\t1995-01-01\n"
        "15\tCrew Game Tune\tmusic\t1995-01-01\n"
        "20\tRick Dangerous (Playable Demo)\tproduction\t1989-01-01\n"
        "21\tRick Dangerous\tproduction\t1989-06-01\n"
        "22\tSnake On A Plane\tproduction\t2010-01-01\n"
        "23\tCool Demo\tproduction\t1990-01-01\n"
        "24\tMole Mayhem V1.1\tproduction\t1995-01-01\n"
        "25\tGame One\tproduction\t1995-01-01\n"
        "26\tGame Two\tproduction\t1995-01-01\n"
        "27\tCrew Game\tproduction\t1995-01-01\n\\.\n"
        "COPY public.productions_production_author_nicks (id, production_id, nick_id) FROM stdin;\n"
        "1\t10\t3000\n2\t11\t3000\n3\t12\t3000\n4\t13\t3000\n5\t14\t3000\n6\t15\t9000\n\\.\n"
        "COPY public.productions_productiontype (id, name, path, depth, numchild, \"position\", internal_name) FROM stdin;\n"
        "1\tGame\t0001\t1\t0\t1\tgame\n2\tDemo\t0002\t1\t0\t2\tdemo\n\\.\n"
        "COPY public.productions_production_types (id, production_id, productiontype_id) FROM stdin;\n"
        "1\t20\t1\n2\t21\t1\n3\t22\t1\n4\t23\t2\n5\t24\t1\n6\t25\t1\n7\t26\t1\n8\t27\t1\n\\.\n"
        "COPY public.productions_soundtracklink (id, production_id, soundtrack_id) FROM stdin;\n"
        "1\t20\t10\n2\t21\t10\n3\t22\t11\n4\t23\t12\n5\t24\t13\n6\t25\t14\n7\t26\t14\n8\t27\t15\n\\.\n"
    )
    p = tmp_path / "dump.sql.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(dump)
    return p


def test_the_index_keeps_game_soundtracks_of_composers(tmp_path):
    *_rest, music_games = demozoo._parse_dump(_write_dump(tmp_path))
    assert sorted(music_games) == [
        ("300", "dangerous rick", "Rick Dangerous"),
        ("300", "dangerous rick", "Rick Dangerous (Playable Demo)"),
        ("300", "finish mayhem mole", "Mole Mayhem V1.1"),
        ("300", "jack nipper", "Snake On A Plane"),
        ("300", "tune twin", "Game One"),
        ("300", "tune twin", "Game Two"),
    ]                                   # not the demo's soundtrack, not the group's tune


@pytest.mark.parametrize("title, game", [
    ("Rick Dangerous", "Rick Dangerous"),              # one game once "(Playable Demo)" is cut
    ("Mole Mayhem Finish", "Mole Mayhem"),             # named after it; version cut
    ("Jack the Nipper", None),                         # a later game reused the tune
    ("Twin Tune", None),                               # two different games
    ("Tune X", None), ("Rick Dangerous 2", None), ("", None),
])
def test_a_tune_names_the_game_it_was_made_for(title, game):
    rows = [(frozenset(demozoo._year_toks(m)), g) for m, g in (
        ("Rick Dangerous", "Rick Dangerous"), ("Rick Dangerous", "Rick Dangerous (Playable Demo)"),
        ("Mole Mayhem Finish", "Mole Mayhem V1.1"), ("Jack the Nipper", "Snake On A Plane"),
        ("Twin Tune", "Game One"), ("Twin Tune", "Game Two"))]
    assert demozoo._demozoo_game(rows, frozenset(demozoo._year_toks(title)), title) == game


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "demozoo.sqlite"
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    st = demozoo.refresh_index(dump_path=_write_dump(tmp_path))
    assert st["error"] is None and st["games"] == 6
    store = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: store)

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr("soniqboom.core.folder_album.refresh_album_caches", _no_refresh)
    monkeypatch.setattr(demozoo, "_last_apply_sig", None)
    demozoo._status["applying"] = False
    return store


def _t(tid, title, artist="moby", fmt="SID", **kw):
    d = {"id": tid, "path": f"/m/{tid}.sid", "title": title, "artist": artist, "album": "",
         "format": fmt, "genre": [], "file_md5": tid.ljust(32, "0")}
    d.update(kw)
    return d


async def test_the_apply_names_the_game_and_ranks_it_before_the_song_database(env):
    store = env
    store.upsert_tracks_batch([
        _t("a", "Rick Dangerous"),
        _t("b", "Jack the Nipper"),
        _t("c", "Mole Mayhem Finish", fmt="ProTracker", album="Mole Mayhem V1.1",
           album_source="songdb", game_by_songdb="Mole Mayhem V1.1"),
        _t("d", "Rick Dangerous", artist="somebody else"),
        _t("e", "Rick Dangerous", fmt="MP3"),
    ])
    res = await demozoo.apply_to_library(force=True)
    assert res.get("error") is None
    a, b, c, d, e = (store.get_track(x) for x in "abcde")
    assert (a["game_by_demozoo"], a["game"], a["game_source"]) == (
        "Rick Dangerous", "Rick Dangerous", "demozoo")
    assert not b.get("game_by_demozoo") and not b.get("game")
    assert (c["game"], c["game_source"], c["game_aliases"]) == (
        "Mole Mayhem", "demozoo", ["Mole Mayhem V1.1"])
    assert not d.get("game_by_demozoo") and not e.get("game_by_demozoo")
    # a rescan keeps it while artist and title are the same
    store.upsert_tracks_batch([_t("a", "Rick Dangerous")])
    assert store.get_track("a")["game"] == "Rick Dangerous"
    store.upsert_tracks_batch([_t("a", "Rick Dangerous", artist="someone")])
    assert not store.get_track("a").get("game_by_demozoo")


async def test_a_stale_demozoo_game_is_withdrawn_and_reset_removes_it(env):
    store = env
    store.upsert_tracks_batch([_t("a", "Rick Dangerous"), _t("x", "Other", game_by_demozoo="Stale")])
    await demozoo.apply_to_library(force=True)
    assert store.get_track("a")["game"] == "Rick Dangerous"
    assert not store.get_track("x").get("game_by_demozoo")        # no longer derives
    store.update_track_fields("a", {"artist": "unknown person"})    # no longer resolves
    await demozoo.apply_to_library(force=True)
    assert not store.get_track("a").get("game_by_demozoo") and not store.get_track("a").get("game")
    store.update_track_fields("a", {"artist": "moby"})
    await demozoo.apply_to_library(force=True)
    assert store.get_track("a")["game"] == "Rick Dangerous"
    n, batch = demozoo.reset_enrichment()
    store.update_track_fields_batch(batch)
    assert not store.get_track("a").get("game_by_demozoo") and not store.get_track("a").get("game")


def test_an_index_built_before_game_soundtracks_reports_it(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "demozoo.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE scener (name TEXT PRIMARY KEY, releaser_id INTEGER, real_name TEXT, groups TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    assert demozoo.status()["games"] is None


def test_the_tune_must_be_named_after_the_whole_game_title_and_one_game():
    toks = lambda s: frozenset(demozoo._year_toks(s))       # noqa: E731
    rows = [(toks("Rick Dangerously"), "Rick Dangerous")]
    assert demozoo._demozoo_game(rows, toks("Rick Dangerously"), "Rick Dangerously") is None
    rows = [(toks("Rick Dangerous 2 Title"), "Rick Dangerous"),
            (toks("Rick Dangerous 2 Title"), "Rick Dangerous 2")]
    assert demozoo._demozoo_game(rows, toks("Rick Dangerous 2 Title"), "Rick Dangerous 2 Title") is None


async def test_a_track_without_an_artist_loses_its_demozoo_game(env):
    store = env
    store.upsert_tracks_batch([_t("a", "Rick Dangerous")])
    await demozoo.apply_to_library(force=True)
    assert store.get_track("a")["game"] == "Rick Dangerous"
    store.update_track_fields("a", {"artist": ""})
    await demozoo.apply_to_library(force=True)
    assert not store.get_track("a").get("game_by_demozoo") and not store.get_track("a").get("game")


async def test_a_rescan_with_another_title_drops_the_demozoo_game(env):
    store = env
    store.upsert_tracks_batch([_t("a", "Rick Dangerous")])
    await demozoo.apply_to_library(force=True)
    store.upsert_tracks_batch([_t("a", "Something Else")])            # same artist, new title
    assert not store.get_track("a").get("game_by_demozoo")
