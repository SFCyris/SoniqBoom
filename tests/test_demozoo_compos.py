# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The party competition a tune entered, its place and the competition's
number of entries (``scene_compo`` / ``scene_place`` /
``scene_compo_entries``): kept in the Demozoo index, written by the apply
with ``scene_party``, left alone by an index built before them, withdrawn by
Reset."""
from __future__ import annotations

import gzip
import sqlite3

from soniqboom.core import demozoo
from tests.test_retro_genres import _dump, _t, store  # noqa: F401  (fixture)

_PLACINGS = ("COPY public.parties_competitionplacing (id, competition_id, production_id, "
             "ranking, \"position\") FROM stdin;\n1\t70\t1\t1\t1\n\\.\n")


def _compo_dump(tmp_path):
    """``_dump`` with "Zone" (1) 2nd of 3 in Assembly 1991's "Music" compo
    ("=2" ranking, no position), "Crack" (2) in two competitions (no compo)."""
    p = _dump(tmp_path)
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        text = fh.read()
    text = text.replace(
        "COPY public.parties_competition (id, party_id, name) FROM stdin;\n70\t7\tMusic\n\\.\n",
        "COPY public.parties_competition (id, party_id, name) FROM stdin;\n"
        "70\t7\tMusic\n71\t7\tChip Music\n\\.\n")
    text = text.replace(_PLACINGS,
                        "COPY public.parties_competitionplacing (id, competition_id, production_id, "
                        "ranking, \"position\") FROM stdin;\n"
                        "1\t70\t1\t=2\t\\N\n2\t70\t11\t1\t1\n3\t70\t14\t3\t3\n"
                        "4\t70\t2\t4\t4\n5\t71\t2\t1\t1\n\\.\n")
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write(text)
    return p


async def test_compo_and_place_are_kept_and_applied(store, tmp_path, monkeypatch):  # noqa: F811
    db = tmp_path / "demozoo.sqlite"
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    st = demozoo.refresh_index(dump_path=_compo_dump(tmp_path))
    assert st["error"] is None and st["compos"] == 1
    monkeypatch.setattr(demozoo, "_last_apply_sig", None)
    demozoo._status["applying"] = False
    mk = lambda tid, title: _t(tid, f"mods/{tid}.mod", "ProTracker", ["Tracker", "Module"],  # noqa: E731
                               title=title, artist="tel")
    store.upsert_tracks_batch([mk("z", "Zone"), mk("c", "Crack")])
    await demozoo.apply_to_library(force=True)
    g = store.get_track
    z = g("z")
    assert (z["scene_party"], z["scene_compo"], z["scene_place"], z["scene_compo_entries"]) == (
        "Assembly 1991", "Music", 2, 4)
    # in two competitions: no single compo (the party too — as before)
    assert not g("c").get("scene_compo") and g("c").get("scene_place") is None
    # Reset withdraws them
    n, batch = demozoo.reset_enrichment()
    store.update_track_fields_batch(batch)
    assert g("z").get("scene_compo") is None and g("z").get("scene_place") is None
    assert g("z").get("scene_compo_entries") is None


async def test_an_index_without_compos_leaves_them(store, tmp_path, monkeypatch):  # noqa: F811
    db = tmp_path / "demozoo.sqlite"
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    demozoo.refresh_index(dump_path=_compo_dump(tmp_path))
    con = sqlite3.connect(db)                       # (as an index built before them)
    con.execute("CREATE TABLE mu2 AS SELECT releaser_id, ptoks, kinds, party FROM music_use")
    con.execute("DROP TABLE music_use")
    con.execute("ALTER TABLE mu2 RENAME TO music_use")
    con.commit()
    con.close()
    assert demozoo.status()["compos"] is None
    monkeypatch.setattr(demozoo, "_last_apply_sig", None)
    demozoo._status["applying"] = False
    store.upsert_tracks_batch([_t("z", "mods/z.mod", "ProTracker", ["Tracker", "Module"],
                                  title="Zone", artist="tel")])
    store.update_track_fields("z", {"scene_compo": "Kept", "scene_place": 5})
    await demozoo.apply_to_library(force=True)
    assert store.get_track("z")["scene_party"] == "Assembly 1991"
    assert (store.get_track("z")["scene_compo"], store.get_track("z")["scene_place"]) == ("Kept", 5)


def test_the_place_is_the_ranking():
    """``ranking`` is the place shown (a tie "=2", "3-4"); ``position`` only
    orders the results — an unranked or disqualified entry has no place."""
    P = demozoo._place
    assert [P("2", "=2"), P("3", "=2"), P("4", "3-4"), P("7", ""), P("9", "DQ"),
            P("5", "\\N"), P("5", None), P(None, None)] == [2, 2, 3, None, None, None, 5, None]
