# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Modland changelogs (core/modland_changelog.py): the parser (every verb,
paths with spaces / unicode / ": ", the multi-line moved / renamed records,
what it refuses) and the net-change plan (idempotent, moves composed)."""
from __future__ import annotations

import pytest

from soniqboom.core import modland_changelog as cl

# Real records (changelog_2026-09-04.txt, verbs right-aligned to 7 columns).
REAL = """\
  added: Digibooster/Drawer/shocker (short version).digi
updated: Fasttracker 2/Kenet/chip or not.xm
  moved: FredMon/- unknown/game shorty.fred
     to: FredMon/Shorty
     as: ghh-first-demo.fred
deleted: JamCracker/Lydian/bronx megademo ii.jam
renamed: MGSDRV/Kyoda/Sailormoon - Moon prisum.MGS
     to: sailormoon - moon prisum power make up!.MGS
  moved: Protracker/Skytech/peace and chaos (c).mod
     to: Protracker/Mr. Smooth
"""


def test_every_verb_with_spaces_and_the_multi_line_records():
    assert cl.parse(REAL.encode()) == [
        ("added", "Digibooster/Drawer/shocker (short version).digi"),
        ("updated", "Fasttracker 2/Kenet/chip or not.xm"),
        ("moved", "FredMon/- unknown/game shorty.fred", "FredMon/Shorty/ghh-first-demo.fred"),
        ("deleted", "JamCracker/Lydian/bronx megademo ii.jam"),
        ("moved", "MGSDRV/Kyoda/Sailormoon - Moon prisum.MGS",
         "MGSDRV/Kyoda/sailormoon - moon prisum power make up!.MGS"),
        ("moved", "Protracker/Skytech/peace and chaos (c).mod",
         "Protracker/Mr. Smooth/peace and chaos (c).mod"),
    ]


def test_unicode_colons_blank_lines_and_crlf():
    text = ("  added: Protracker/Jörg Ünal/tune: part 1 (★).mod\r\n\r\n"
            "renamed: Protracker/Jörg Ünal/a: b.mod\r\n     to: c: d.mod\r\n")
    assert cl.parse(text.encode("utf-8")) == [
        ("added", "Protracker/Jörg Ünal/tune: part 1 (★).mod"),
        ("moved", "Protracker/Jörg Ünal/a: b.mod", "Protracker/Jörg Ünal/c: d.mod"),
    ]


@pytest.mark.parametrize("text, why", [
    (b"  added: x/a.mod\nsomething else\n", "isn't a changelog record"),
    (b"     to: x/b\n", "without a moved"),
    (b"  moved: x/a.mod\n  added: x/b.mod\n", "has no 'to:'"),
    (b"renamed: x/a.mod\n     to: b.mod\n     as: c.mod\n", "has an 'as:'"),
    (b"renamed: x/a.mod\n     to: y/b.mod\n", "to a path"),
    (b"  moved: x/a.mod\n     as: c.mod\n", "'as:' before 'to:'"),
    (b"  moved: x/a.mod\n     to: y\n     to: z\n", "a second 'to:'"),
    (b"  added: x/../a.mod\n", "unusable path"),
    (b"  added: x/\xff.mod\n", "not UTF-8"),
])
def test_what_the_parser_refuses(text, why):
    with pytest.raises(cl.ChangelogError, match=why.replace("(", r"\(").replace(")", r"\)")):
        cl.parse(text)


def test_dates_of_changelog_names():
    assert cl.date_of("changelog_2026-10-02.txt") == "2026-10-02"
    assert cl.date_of("changelog_2026-10-02.txt.bak") is None
    assert cl.date_of("readme_updates.txt") is None


INDEX = {"A/a.mod", "A/b.mod", "B/c.mod", "B/d.mod"}


def _plan(ops, index=INDEX):
    return cl.plan(ops, index.__contains__)


def test_plan_basic_net_change():
    p = _plan([("added", "A/new.mod"), ("deleted", "A/b.mod"), ("updated", "B/c.mod"),
               ("moved", "B/d.mod", "C/d2.mod")])
    assert p.added == ["A/new.mod"] and p.deleted == ["A/b.mod"]
    assert p.updated == ["B/c.mod"] and p.moved == {"B/d.mod": "C/d2.mod"}
    assert p.need_size() == ["A/new.mod", "B/c.mod"]


def test_plan_is_idempotent_over_an_index_that_has_it_already():
    ops = [("added", "A/new.mod"), ("deleted", "A/b.mod"), ("moved", "B/d.mod", "C/d2.mod")]
    after = (INDEX | {"A/new.mod", "C/d2.mod"}) - {"A/b.mod", "B/d.mod"}
    p = _plan(ops, after)
    assert not p, p                                            # nothing left to do


def test_plan_composes_runs():
    # added then moved = added at its final place; a move of a move is one move
    p = _plan([("added", "A/n.mod"), ("moved", "A/n.mod", "C/n.mod"),
               ("moved", "A/a.mod", "B/a.mod"), ("moved", "B/a.mod", "D/a2.mod")])
    assert p.added == ["C/n.mod"] and p.moved == {"A/a.mod": "D/a2.mod"} and not p.deleted
    # added then deleted = nothing; deleted then added back = a re-sized file
    p = _plan([("added", "A/n.mod"), ("deleted", "A/n.mod"),
               ("deleted", "A/a.mod"), ("added", "A/a.mod")])
    assert not p.added and not p.deleted and p.updated == ["A/a.mod"]
    # an updated file that is then moved: a move that needs the new size
    p = _plan([("updated", "A/a.mod"), ("moved", "A/a.mod", "C/a.mod")])
    assert p.moved == {"A/a.mod": "C/a.mod"} and p.moved_changed == {"C/a.mod"}
    assert p.need_size() == ["C/a.mod"]
    # a moved file that is then deleted is deleted at its old place
    p = _plan([("moved", "A/a.mod", "C/a.mod"), ("deleted", "C/a.mod")])
    assert p.deleted == ["A/a.mod"] and not p.moved


def test_plan_refuses_a_move_it_cannot_place():
    with pytest.raises(cl.ChangelogError, match="neither"):
        _plan([("moved", "X/gone.mod", "Y/also gone.mod")])
    with pytest.raises(cl.ChangelogError, match="both"):
        _plan([("moved", "A/a.mod", "A/b.mod")])


def test_updated_and_added_on_odd_states():
    p = _plan([("updated", "Z/never seen.mod")])               # listed nowhere: a new file
    assert p.added == ["Z/never seen.mod"]
    p = _plan([("added", "A/a.mod")])                          # already there: no-op
    assert not p


# ── a moved tune's user data (core/id_remap.py, op remap_track_ids) ──────────

def test_remap_moves_every_kind_of_user_data_and_is_safe_to_redo():
    from soniqboom.core.id_remap import remap_user_data
    # "b" already has data: another file once at that path (since removed)
    ratings = {"a": 4, "b": 2, "c": 5}
    plays = {"u1": {"a": {"count": 2, "last_played": 100}, "b": {"count": 1, "last_played": 300}},
             "u2": {"a": {"count": 5, "last_played": 50}}}
    history = {"u1": [{"track_id": "a", "ts": 1}, {"track_id": "x", "ts": 2}],
               "u3": [{"track_id": "a", "ts": 9}]}
    pl_in = {"id": "p", "track_ids": ["a", {"id": "c", "subsong": 3}, "x"]}
    playlists = {"p": pl_in}
    waves = {"a": [0.1], "b": [0.9], "d": [0.7]}
    counts = remap_user_data({"a": "b", "c": "d", "same": "same", "": "z"},
                             ratings=ratings, play_stats=plays, history=history,
                             playlists=playlists, waveforms=waves)
    assert ratings == {"b": 4, "d": 5}                 # the moved file's data wins
    assert plays == {"u1": {"b": {"count": 2, "last_played": 100}},      # each user's own
                     "u2": {"b": {"count": 5, "last_played": 50}}}
    assert history["u1"][0] == {"track_id": "b", "ts": 1} and history["u1"][1]["track_id"] == "x"
    assert history["u3"] == [{"track_id": "b", "ts": 9}]
    assert playlists["p"]["track_ids"] == ["b", {"id": "d", "subsong": 3}, "x"]
    assert pl_in["track_ids"] == ["a", {"id": "c", "subsong": 3}, "x"]   # replaced, not edited
    assert waves == {"b": [0.1], "d": [0.7]}           # (c had none: d's stays)
    assert counts == {"ratings": 2, "play_stats": 2, "history": 2, "playlists": 2, "waveforms": 1}
    # Redone (a move followed again after a crash): nothing changes.
    snap = (dict(ratings), {u: dict(m) for u, m in plays.items()},
            {u: list(h) for u, h in history.items()}, dict(playlists), dict(waves))
    again = remap_user_data({"a": "b", "c": "d"}, ratings=ratings, play_stats=plays,
                            history=history, playlists=playlists, waveforms=waves)
    assert (ratings, plays, history, playlists, waves) == snap
    assert again == {"ratings": 0, "play_stats": 0, "history": 0, "playlists": 0, "waveforms": 0}
    assert remap_user_data({}, ratings=ratings, play_stats=None, history=None,
                           playlists=None, waveforms=None)["ratings"] == 0


@pytest.mark.parametrize("mapping", [{"b": "b2", "a": "b"}, {"a": "b", "b": "b2"},
                                     {"a": "b", "b": "a"}])
def test_remap_chains_and_swaps_land_on_the_right_ids(mapping):
    """Every old id moves at once: the order of a rename chain doesn't matter,
    and a swap swaps."""
    from soniqboom.core.id_remap import remap_user_data
    ratings = {"a": 5, "b": 2}
    plays = {"u1": {"a": {"count": 7, "last_played": 1}}}
    remap_user_data(mapping, ratings=ratings, play_stats=plays, history=None,
                    playlists=None, waveforms=None)
    want = {mapping[k]: v for k, v in {"a": 5, "b": 2}.items()}
    assert ratings == want
    assert plays == {"u1": {mapping["a"]: {"count": 7, "last_played": 1}}}


def test_a_remap_survives_a_restart_and_a_merge(tmp_path, monkeypatch):
    """The op is one AOF record: replayed at start and folded into the
    snapshot by the merger, with the same result as live."""
    from soniqboom.core import merger, persistence
    from soniqboom.core import store as store_mod
    from soniqboom.core.aof import AOFWriter
    monkeypatch.setattr(store_mod, "_store", None)
    persistence.init_persistence(tmp_path)
    st = store_mod.get_store()
    w = AOFWriter(tmp_path / "library.aof")
    st._aof_append = w.append
    st.set_rating("old", 4)
    st.record_play("old", uid="")
    st.record_play("old", uid="")
    st.push_history({"track_id": "old", "title": "t", "artist": "", "ts": 7}, uid="")
    st.create_playlist("pl", "Mine", track_ids=["old", {"id": "old2", "subsong": 2}])
    st.store_waveform("old", [0.5, 0.25])
    st.remap_track_ids({"old": "new", "old2": "new2"})
    live = (st.get_rating("new"), st.get_play_stats("new", uid="")["count"],
            st.get_playlist("pl")["track_ids"], st.get_waveform("new"))
    assert live == (4, 2, ["new", {"id": "new2", "subsong": 2}], [0.5, 0.25])
    w.flush_sync()
    st._aof_append = None

    def _loaded():
        store_mod._store = None
        state = persistence.load_snapshot(tmp_path)
        persistence.replay_aof(state, tmp_path)
        persistence.populate_store(state)
        s = store_mod.get_store()
        assert s is not st
        return (s.get_rating("new"), (s.get_play_stats("new", uid="") or {}).get("count"),
                s.get_playlist("pl")["track_ids"],
                bool(s.get_rating("old")), [h["track_id"] for h in s.get_history(5, uid="")])
    # (Waveforms reach disk with the snapshot, not the AOF: compared live only.)
    assert _loaded() == (*live[:3], False, ["new"])               # AOF replay
    assert merger._do_merge(tmp_path) > 0
    assert (tmp_path / "library.aof").stat().st_size == 0         # (all of it in the snapshot)
    assert _loaded() == (*live[:3], False, ["new"])               # merged into the snapshot
