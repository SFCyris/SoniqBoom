# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline unit tests for the SCENE-tab enrichment (``demozoo.scene_card`` and
helpers).  All network + sqlite dependencies are monkeypatched, so these run
anywhere and lock the CONFIDENCE-GATE behaviour: a wrong scener or a wrong
year must never reach the panel.

The tests deliberately exercise the *pure* logic — release title-match,
refuse-on-ambiguity, year extraction, and the no-raise contract — not the live
Demozoo API (which is covered by the manual end-to-end verification)."""
from __future__ import annotations

import asyncio

import pytest

from soniqboom.core import demozoo


# ── _year_of ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expect", [
    ("1997-08-05", 1997),
    ("2017", 2017),
    ("2023-09-02", 2023),
    ("1985", 1985),
    (None, None),
    ("", None),
    ("n/a", None),
    ("19", None),          # too short
    ("1899", None),        # below the plausible-year floor
    ("3000", None),        # above the ceiling
    (2020, None),          # non-str input
])
def test_year_of(raw, expect):
    assert demozoo._year_of(raw) == expect


# ── scene_card confidence gate ───────────────────────────────────────────────
_BASE = {"releaser_id": 356, "real_name": "Peter Hajba",
         "groups": ["Future Crew"], "url": "https://demozoo.org/sceners/356/"}
_DETAILS = {"name": "Peter Hajba", "aliases": ["Skaven", "Peter Hajba", "Skaven252"],
            "groups": ["Brainstorm", "Future Crew"],
            "links": [{"class": "BandcampArtist", "url": "https://x.bandcamp.com/"}]}
_PRODS = [
    {"id": 1, "title": "War In The Middle Earth-Remix", "year": 1994,
     "type": "Tracked Music", "platforms": ["MS-Dos"], "url": "https://demozoo.org/music/1/"},
    {"id": 2, "title": "Clockbound Days", "year": 2023, "type": "Music",
     "platforms": [], "url": "https://demozoo.org/music/2/"},
    {"id": 3, "title": "Hoggy 2 Music Suite", "year": 2017, "type": "Tracked Music",
     "platforms": [], "url": "https://demozoo.org/music/3/"},
]


def _patch(monkeypatch, *, base=_BASE, prods=_PRODS, detail=None, details=_DETAILS):
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: base)

    async def _fake_details(rid):
        return details

    async def _fake_prods(rid):
        return list(prods) if prods is not None else None

    async def _fake_detail(pid):
        return detail if detail is not None else {
            "id": pid, "title": "prod", "year": 1994, "type": "Tracked Music",
            "platforms": ["MS-Dos"], "parties": [], "placings": [], "links": [],
            "url": f"https://demozoo.org/music/{pid}/"}

    monkeypatch.setattr(demozoo, "fetch_scener_details", _fake_details)
    monkeypatch.setattr(demozoo, "fetch_scener_productions", _fake_prods)
    monkeypatch.setattr(demozoo, "fetch_production_detail", _fake_detail)


def test_scene_card_unresolved_scener(monkeypatch):
    """No scener match ⇒ found:false (panel shows baseline only)."""
    _patch(monkeypatch, base=None)
    card = asyncio.run(demozoo.scene_card("Nobody", track_title="Some Long Title Here"))
    assert card == {"found": False}


def test_scene_card_release_single_match(monkeypatch):
    """A track whose title matches exactly ONE production populates the release
    block with THAT production's canonical year (the retro year overwrite)."""
    _patch(monkeypatch)
    card = asyncio.run(demozoo.scene_card(
        "Skaven", track_title="War in the Middle Earth -Rm"))
    assert card["found"] is True
    assert card["artist"]["real_name"] == "Peter Hajba"
    assert card["release"] is not None
    assert card["release"]["year"] == 1994
    # the matched production is NOT duplicated into the discography list
    disco_titles = [d["title"] for d in card["discography"]]
    assert "War In The Middle Earth-Remix" not in disco_titles
    assert "Clockbound Days" in disco_titles


def test_scene_card_no_title_match_refuses_release(monkeypatch):
    """A title that matches NO production ⇒ no release block (refuse over guess),
    but identity + discography still resolve."""
    _patch(monkeypatch)
    card = asyncio.run(demozoo.scene_card(
        "Skaven", track_title="Totally Unrelated Nonexistent Tune"))
    assert card["found"] is True
    assert card["release"] is None
    assert len(card["discography"]) == 3          # nothing removed


def test_scene_card_ambiguous_title_refuses_release(monkeypatch):
    """Two productions matching the same title ⇒ refuse the release block
    (can't pick a year with certainty)."""
    dupes = [
        {"id": 10, "title": "Space Journey Part One", "year": 1993,
         "type": "Tracked Music", "platforms": [], "url": "u10"},
        {"id": 11, "title": "Space Journey Part One", "year": 1995,
         "type": "Tracked Music", "platforms": [], "url": "u11"},
    ]
    _patch(monkeypatch, prods=dupes)
    card = asyncio.run(demozoo.scene_card("Skaven", track_title="Space Journey Part One"))
    assert card["found"] is True
    assert card["release"] is None                # ambiguous ⇒ no year overwrite


def test_scene_card_single_token_title_no_release(monkeypatch):
    """A title with <2 distinctive tokens can't drive a match (guards against
    short/common titles false-matching)."""
    _patch(monkeypatch)
    card = asyncio.run(demozoo.scene_card("Skaven", track_title="The Alchemist"))
    assert card["found"] is True
    assert card["release"] is None


def test_scene_card_never_raises_on_bad_productions(monkeypatch):
    """A degraded productions payload must not raise past scene_card."""
    _patch(monkeypatch, prods=None)               # fetch failed
    card = asyncio.run(demozoo.scene_card("Skaven", track_title="War In The Middle Earth-Remix"))
    assert card["found"] is True
    assert card["discography"] == []
    assert card["release"] is None


def test_scene_card_details_failure_keeps_identity(monkeypatch):
    """If the live scener-details call fails, the offline identity survives."""
    _patch(monkeypatch, details=None)
    card = asyncio.run(demozoo.scene_card("Skaven", track_title="War in the Middle Earth -Rm"))
    assert card["found"] is True
    assert card["artist"]["real_name"] == "Peter Hajba"
    assert card["artist"]["groups"] == ["Future Crew"]   # falls back to offline groups


def test_scene_card_thin_detail_backfills_year(monkeypatch):
    """A thin production detail (null year) must not suppress the year overwrite
    — the year is backfilled from the discography row that DID carry it."""
    thin = {"id": 1, "title": "", "year": None, "type": "", "platforms": [],
            "parties": [], "placings": [], "links": [],
            "url": "https://demozoo.org/music/1/"}
    _patch(monkeypatch, detail=thin)
    card = asyncio.run(demozoo.scene_card(
        "Skaven", track_title="War in the Middle Earth -Rm"))
    assert card["release"] is not None
    assert card["release"]["year"] == 1994          # from the _PRODS[0] list row
    assert card["release"]["type"] == "Tracked Music"
    assert card["release"]["title"] == "War In The Middle Earth-Remix"


# ── _parse_dump: shared-handle disambiguation is MUSIC-only ───────────────────
def _write_dump(tmp_path, *, with_supertype: bool):
    """Synthetic pg_dump.

    * handle 'zap' SHARED by musician (releaser 100, 'Zap Tune Deluxe' 1992 +
      undated 'Undated Groove Tune') and coder (200, demo only);
    * UNIQUE scener 'moby' (300, 'Ocean Loader 2' 1987);
    * UNIQUE sceners 'alpha' (400) and 'beta' (500) — used to prove the
      variant-disagreement refusal for an artist string "Alpha (Beta)".
    """
    if with_supertype:
        prod_cols = "id, title, supertype, release_date_date"
        prod_rows = ("5000\tZap Tune Deluxe\tmusic\t1992-06-01\n"
                     "6000\tZap Tune Deluxe Party Invitation\tproduction\t1993-01-01\n"
                     "7000\tUndated Groove Tune\tmusic\t\\N\n"
                     "8000\tOcean Loader 2\tmusic\t1987-03-01")
    else:
        prod_cols = "id, title"
        prod_rows = ("5000\tZap Tune Deluxe\n"
                     "6000\tZap Tune Deluxe Party Invitation\n"
                     "7000\tUndated Groove Tune\n"
                     "8000\tOcean Loader 2")
    dump = (
        "COPY public.demoscene_releaser (id, name, first_name, surname, is_group) FROM stdin;\n"
        "100\tzap\tAnn\tSmith\tf\n200\tzap\tBob\tJones\tf\n"
        "300\tmoby\tCara\tDoe\tf\n400\talpha\tDee\tEve\tf\n500\tbeta\tFin\tGray\tf\n\\.\n"
        "COPY public.demoscene_nick (id, releaser_id, name, abbreviation) FROM stdin;\n"
        "1000\t100\tzap\t\\N\n2000\t200\tzap\t\\N\n3000\t300\tmoby\t\\N\n"
        "4000\t400\talpha\t\\N\n5000\t500\tbeta\t\\N\n\\.\n"
        "COPY public.demoscene_nickvariant (id, nick_id, name) FROM stdin;\n\\.\n"
        "COPY public.demoscene_membership (id, member_id, group_id) FROM stdin;\n\\.\n"
        f"COPY public.productions_production ({prod_cols}) FROM stdin;\n{prod_rows}\n\\.\n"
        "COPY public.productions_production_author_nicks (id, production_id, nick_id) FROM stdin;\n"
        "1\t5000\t1000\n2\t6000\t2000\n3\t7000\t1000\n4\t8000\t3000\n\\.\n"
    )
    import gzip as _gz
    p = tmp_path / "dump.sql.gz"
    with _gz.open(p, "wt", encoding="utf-8") as f:
        f.write(dump)
    return p


def test_parse_dump_shared_handle_music_only(tmp_path):
    """With supertypes present, the shared handle 'zap' only carries the
    MUSICIAN's (releaser 100) music production as disambiguation evidence — the
    coder namesake's colliding DEMO title is excluded."""
    unique, ambig, _ = demozoo._parse_dump(_write_dump(tmp_path, with_supertype=True))
    rids = {row[1] for row in ambig if row[0] == "zap"}
    assert rids == {100}                       # coder (200) contributes no music
    assert not any("party" in row[4] for row in ambig)   # demo tokens absent


def test_parse_dump_no_supertype_column_degrades(tmp_path):
    """A dump WITHOUT a supertype column keeps the previous all-productions
    behaviour for disambiguation, and yields ONLY veto rows (year None) for the
    year gate — no dates ⇒ no year is ever stamped, but nothing crashes."""
    unique, ambig, prod_years = demozoo._parse_dump(_write_dump(tmp_path, with_supertype=False))
    rids = {row[1] for row in ambig if row[0] == "zap"}
    assert rids == {100, 200}                  # no supertype ⇒ no filtering
    assert prod_years and all(y is None for _, _, y in prod_years)


def test_parse_dump_prod_years_shapes(tmp_path):
    """prod_years: MUSIC only, digit tokens kept, undated rows preserved as
    year-None vetoes, the demo excluded."""
    _, _, prod_years = demozoo._parse_dump(_write_dump(tmp_path, with_supertype=True))
    assert sorted(prod_years) == [
        ("100", "deluxe tune zap", 1992),
        ("100", "groove tune undated", None),   # undated ⇒ veto row
        ("300", "2 loader ocean", 1987),        # '2' survives _year_toks
    ]


# ── _production_year: exact-identity + veto + consensus gate ─────────────────
def test_production_year_exact_identity_gate():
    rows = [
        (frozenset({"deluxe", "tune", "zap"}), 1992),
        (frozenset({"deluxe", "tune", "zap", "remix"}), 1994),   # different title
    ]
    t = frozenset({"zap", "tune", "deluxe"})
    # Exact identity: the remix's 1994 must NOT bleed onto the original.
    assert demozoo._production_year(rows, t) == 1992
    # The remix title itself no longer matches the original's row either.
    assert demozoo._production_year(rows, frozenset({"zap", "tune", "deluxe", "remix"})) == 1994
    # Digit sequels stay distinct ('2' kept by _year_toks).
    rows2 = [(frozenset({"last", "ninja"}), 1991),
             (frozenset({"last", "ninja", "2"}), 1990)]
    assert demozoo._production_year(rows2, frozenset({"last", "ninja"})) == 1991
    assert demozoo._production_year(rows2, frozenset({"last", "ninja", "2"})) == 1990


def test_production_year_undated_veto_and_consensus():
    t = frozenset({"zap", "tune", "deluxe"})
    # Same-title sibling with NO date ⇒ can't know which printing ⇒ refuse.
    rows = [(t, 1992), (t, None)]
    assert demozoo._production_year(rows, t) is None
    # Two same-title printings with the SAME date ⇒ fine.
    assert demozoo._production_year([(t, 1992), (t, 1992)], t) == 1992
    # Different dates ⇒ refuse.
    assert demozoo._production_year([(t, 1992), (t, 1994)], t) is None
    # Empty track tokens ⇒ refuse; no match ⇒ None.
    assert demozoo._production_year([(t, 1992)], frozenset()) is None
    assert demozoo._production_year([(t, 1992)], frozenset({"other"})) is None


# ── collect_updates: the code that actually writes the library ───────────────
class _FakeStore:
    def __init__(self, tracks):
        self._tracks = tracks

    def all_tracks(self):
        return self._tracks


def _built_index(tmp_path, monkeypatch):
    dbp = tmp_path / "demozoo.sqlite"
    monkeypatch.setattr(demozoo, "_db_path", lambda: dbp)
    res = demozoo.refresh_index(dump_path=_write_dump(tmp_path, with_supertype=True))
    assert not res.get("error")
    return dbp


def _run_collect(monkeypatch, tracks):
    import soniqboom.core.store as store_mod
    monkeypatch.setattr(store_mod, "get_store", lambda: _FakeStore(tracks))
    return demozoo.collect_updates()


def test_collect_updates_stamps_and_preserves_original(tmp_path, monkeypatch):
    _built_index(tmp_path, monkeypatch)
    matched, batch = _run_collect(monkeypatch, [
        {"id": "t1", "format": "SID", "artist": "Moby",
         "title": "Ocean Loader 2", "year": 1999},
    ])
    assert matched == 1
    assert batch == [("t1", {"year": 1987, "year_source": "demozoo",
                             "year_file": 1999})]


def test_collect_updates_idempotent_reapply(tmp_path, monkeypatch):
    _built_index(tmp_path, monkeypatch)
    matched, batch = _run_collect(monkeypatch, [
        {"id": "t1", "format": "SID", "artist": "Moby", "title": "Ocean Loader 2",
         "year": 1987, "year_source": "demozoo", "year_file": 1999},
    ])
    assert matched == 1
    assert batch == []                          # nothing to change


def test_collect_updates_reverts_stale_stamp(tmp_path, monkeypatch):
    """A stamped track the tightened gate no longer endorses gets its original
    year back (the wrong-year EXIT, QA C1 stickiness)."""
    _built_index(tmp_path, monkeypatch)
    matched, batch = _run_collect(monkeypatch, [
        {"id": "t1", "format": "SID", "artist": "Moby", "title": "Ocean Loader 3",
         "year": 1987, "year_source": "demozoo", "year_file": 1999},
    ])
    assert batch == [("t1", {"year": 1999, "year_source": None, "year_file": None})]


def test_collect_updates_respects_user_year(tmp_path, monkeypatch):
    _built_index(tmp_path, monkeypatch)
    matched, batch = _run_collect(monkeypatch, [
        {"id": "t1", "format": "SID", "artist": "Moby", "title": "Ocean Loader 2",
         "year": 2001, "year_source": "user"},
    ])
    assert batch == []                          # hand edit outranks Demozoo


def test_collect_updates_refuses_variant_disagreement(tmp_path, monkeypatch):
    """'Alpha (Beta)' resolves 'alpha'→400 and 'beta'→500 — different sceners.
    lookup_scener refuses this; collect_updates must too (QA M1), including
    withdrawing a previous stamp."""
    _built_index(tmp_path, monkeypatch)
    matched, batch = _run_collect(monkeypatch, [
        {"id": "t1", "format": "SID", "artist": "Alpha (Beta)",
         "title": "Ocean Loader 2", "year": 1999},
        {"id": "t2", "format": "SID", "artist": "Alpha (Beta)",
         "title": "Ocean Loader 2", "year": 1987,
         "year_source": "demozoo", "year_file": 1999},
    ])
    assert matched == 0
    assert batch == [("t2", {"year": 1999, "year_source": None, "year_file": None})]


def test_collect_updates_undated_veto_blocks_stamp(tmp_path, monkeypatch):
    """Releaser 100's 'Undated Groove Tune' exists but carries no date — the
    veto keeps the year unstamped even though the title matches exactly."""
    _built_index(tmp_path, monkeypatch)
    matched, batch = _run_collect(monkeypatch, [
        {"id": "t1", "format": "SID", "artist": "Zap",
         "title": "Undated Groove Tune", "year": 2003},
    ])
    assert matched == 1                         # resolves via ambig title match
    assert batch == []                          # but no year is stamped
