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
                     "8000\tOcean Loader 2\tmusic\t1987-03-01\n"
                     "9000\tSecond Reality\tproduction\t1993-08-01")   # a DEMO using 8000
    else:
        prod_cols = "id, title"
        prod_rows = ("5000\tZap Tune Deluxe\n"
                     "6000\tZap Tune Deluxe Party Invitation\n"
                     "7000\tUndated Groove Tune\n"
                     "8000\tOcean Loader 2\n"
                     "9000\tSecond Reality")
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
        # demo 9000 uses music 8000 (Ocean Loader 2) as its soundtrack
        "COPY public.productions_soundtracklink (id, production_id, soundtrack_id) FROM stdin;\n"
        "1\t9000\t8000\n\\.\n"
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
    unique, ambig, _, _ = demozoo._parse_dump(_write_dump(tmp_path, with_supertype=True))
    rids = {row[1] for row in ambig if row[0] == "zap"}
    assert rids == {100}                       # coder (200) contributes no music
    assert not any("party" in row[4] for row in ambig)   # demo tokens absent


def test_parse_dump_no_supertype_column_degrades(tmp_path):
    """A dump WITHOUT a supertype column keeps the previous all-productions
    behaviour for disambiguation, and yields ONLY veto rows (year None) for the
    year gate — no dates ⇒ no year is ever stamped, but nothing crashes."""
    unique, ambig, prod_years, _ = demozoo._parse_dump(_write_dump(tmp_path, with_supertype=False))
    rids = {row[1] for row in ambig if row[0] == "zap"}
    assert rids == {100, 200}                  # no supertype ⇒ no filtering
    assert prod_years and all(y is None for _, _, y in prod_years)


def test_parse_dump_prod_years_shapes(tmp_path):
    """prod_years: MUSIC only, digit tokens kept, undated rows preserved as
    year-None vetoes, the demo excluded."""
    _, _, prod_years, _ = demozoo._parse_dump(_write_dump(tmp_path, with_supertype=True))
    assert sorted(prod_years) == [
        ("100", "deluxe tune zap", 1992),
        ("100", "groove tune undated", None),   # undated ⇒ veto row
        ("300", "2 loader ocean", 1987),        # '2' survives _year_toks
    ]


def test_parse_dump_soundtrack_links(tmp_path):
    """The 4th return maps a MUSIC production → the demo(s) that used it,
    resolved to the demo's title + year (inverting the demo→music link)."""
    _, _, _, prod_soundtrack = demozoo._parse_dump(_write_dump(tmp_path, with_supertype=True))
    assert prod_soundtrack == [(8000, "Second Reality", 1993)]


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


def test_production_soundtracks_offline(tmp_path, monkeypatch):
    """After a build, the 'featured in' lookup resolves a music production id to
    the demo(s) that used it; an unknown id (or a pre-soundtrack index) → []."""
    _built_index(tmp_path, monkeypatch)
    assert demozoo._production_soundtracks(8000) == [{"title": "Second Reality", "year": 1993}]
    assert demozoo._production_soundtracks(99999) == []
    assert demozoo._production_soundtracks("not-an-int") == []


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


# ── composer credit gate (the wrong-write cardinal sin) ──────────────────────
# A no-artist scene module's ``composer`` is persisted PERMANENTLY (fill-only,
# no provenance/revert), so a "by X" credit may name the composer ONLY when it
# is a bare "By X" signature or a MUSIC-qualified credit.  These lock the gate
# against the two holes that shipped twice: a non-music word DETACHED from "by"
# by punctuation ("graphics: by X"), and a non-music word simply OMITTED from a
# blacklist ("gr by X", "sfx by X").  The gate is a whitelist — any unrecognised
# word before "by" is refused — so both classes stay closed.
@pytest.mark.parametrize("sample", [
    # non-music word adjacent to "by"
    "gfx by felisuco", "graphics by felisuco", "logo by felisuco",
    "ripped by felisuco", "cracked by felisuco", "code by felisuco",
    "coded by felisuco", "design by felisuco", "greets by felisuco",
    # non-music word DETACHED by punctuation (the CRITICAL leak)
    "graphics: by felisuco", "graphics - by felisuco", "graphics/ by felisuco",
    "graphics. by felisuco", "graphics, by felisuco", "graphics| by felisuco",
    "logo/by felisuco", "pixels. by felisuco",
    # non-music word OMITTED from any blacklist (the HIGH leak)
    "gr by felisuco", "grfx by felisuco", "grafik by felisuco",
    "sfx by felisuco", "fx by felisuco", "vocals by felisuco",
    "samples by felisuco", "sampled by felisuco", "words by felisuco",
    "lyrics by felisuco", "text by felisuco", "voice by felisuco",
    "presented by felisuco", "made by felisuco", "arranged by felisuco",
    "remix by felisuco", "trained by felisuco",
])
def test_music_credits_rejects_non_music(sample):
    """Not a composer credit → nothing is persistable."""
    assert demozoo._music_credits([sample]) == []


@pytest.mark.parametrize("sample,handle", [
    ("by felisuco", "felisuco"),
    ("By felisuco", "felisuco"),
    ("music by felisuco", "felisuco"),
    ("composed by felisuco", "felisuco"),
    ("tune by felisuco", "felisuco"),
    ("song by felisuco", "felisuco"),
    ("melody by felisuco", "felisuco"),
    ("great music by felisuco", "felisuco"),
    ("by 4-mat", "4-mat"),                       # hyphenated handle kept intact
    ("by U4ia", "U4ia"),
])
def test_music_credits_accepts_genuine(sample, handle):
    """Bare or music-qualified "by X" → the composer handle, original case."""
    assert demozoo._music_credits([sample]) == [handle]


@pytest.mark.parametrize("sample,handle", [
    ("By Purple Motion -93", "Purple Motion"),   # space-dash year tag trimmed
    ("By Purple Motion 1993", "Purple Motion"),  # bare year tag trimmed
    ("By Purple Motion of the Future Crew", "Purple Motion"),  # group tail
    ("music by Jugi - Complex", "Jugi"),         # " - group" tail
    ("by Jester & Yolk", "Jester"),              # collaborator tail
])
def test_music_credits_trims_tail(sample, handle):
    assert demozoo._music_credits([sample]) == [handle]


@pytest.mark.parametrize("sample", [
    "ripped by pirate / music by felisuco",      # first credit is a rip, not music
    "gfx by artist, music by felisuco",
    "cracked by group | tune by felisuco",
])
def test_music_credits_multi_credit_finds_the_music(sample):
    """A non-music credit FOLLOWED by a real music credit must not swallow it."""
    assert demozoo._music_credits([sample]) == ["felisuco"]


def test_author_hints_persist_gate():
    """``author_hints_from_track`` exposes a music credit for PERSISTENCE only
    when it is genuine; a graphics/rip credit yields no persistable credit."""
    _, credits = demozoo.author_hints_from_track(
        title="Fast Music", instruments=["gfx by felisuco"])
    assert credits == {}                         # nothing to write
    _, credits = demozoo.author_hints_from_track(
        title="Fast Music", instruments=["music by Purple Motion"])
    assert credits == {"purple motion": "Purple Motion"}  # normalised→original


# ── cross-slot credit split (a role word one sample slot up from "by X") ──────
# Sample lists spell one credit fragment per slot, so a "gfx"/"ripped"/… credit
# can land in the slot BEFORE the "by X" — which, scanned per-slot, would read
# as a bare composer signature.  A KNOWN role word in the previous slot vetoes
# it; an unrelated previous word (a date, a bpm, a greeting — the overwhelming
# real-library case) must NOT, or 800+ genuine signatures would be lost.
@pytest.mark.parametrize("instruments", [
    ["gfx", "by kapteinar"],
    ["graphics", "by kapteinar"],
    ["ripped and converted", "by thexder"],
    ["#### ripped ####", "by mr.young"],
    ["Bad Samples", "by Luv Kohli"],
    ["in full stereo sound", "by www.elysis.de"],
    ["graphics", "by kapteinar", "music", "by someone"],  # role split, real music elsewhere
])
def test_music_credits_cross_slot_role_split_rejected(instruments):
    """A non-music role in the previous slot must not leak a bare "by X"."""
    assert "kapteinar" not in [c.lower() for c in demozoo._music_credits(instruments)]
    if instruments == ["gfx", "by kapteinar"]:
        assert demozoo._music_credits(instruments) == []


@pytest.mark.parametrize("instruments,handle", [
    (["Chip Land", "By Zeus"], "Zeus"),
    (["twenty seconds", "by pkk"], "pkk"),
    (["200bpm", "By Midnight Flip '97"], "Midnight Flip"),  # + apostrophe-year trim
    (["Composed Nov. 12 1992", "by SWAMPFOX"], "SWAMPFOX"),
    (["greetings to everyone", "music", "by kapteinar"], "kapteinar"),  # prev slot = music word
])
def test_music_credits_cross_slot_signature_accepted(instruments, handle):
    """A bare "By <scener>" trailing UNRELATED text is a real signature."""
    assert demozoo._music_credits(instruments) == [handle]


@pytest.mark.parametrize("sample,handle", [
    ("music by Catch 22", "Catch 22"),           # 22 is not a plausible year
    ("music by Area 51", "Area 51"),
    ("by 2 Unlimited", "2 Unlimited"),
])
def test_music_credits_preserves_numeric_handle(sample, handle):
    """The year-tail trim must not eat a digit-bearing handle (would collide
    with a different, real scener)."""
    assert demozoo._music_credits([sample]) == [handle]


# ── cross-slot role word that is NOT the previous slot's LAST word ────────────
# The veto scans the WHOLE previous slot, not just its trailing word — a role
# verb is routinely non-terminal ("cracked together", "converted in 0.30 min").
# These are confirmed real-library wrong-writes (a cracker/converter written as
# the composer) that a last-word-only check let through.
@pytest.mark.parametrize("instruments", [
    ["cracked together", "by sinatra"],
    ["samples & jingles", "by loxley"],
    ["ORIGINAL BY JENS", "CONVERTED IN 0.30 MIN", "BY TYAN"],
    ["Converted with Pro-Wizard", "by Gryzor"],
])
def test_music_credits_cross_slot_nonterminal_role_rejected(instruments):
    assert demozoo._music_credits(instruments) == []


# ── "tracked" is a MUSIC verb (sequenced in a tracker), not a rip role ────────
@pytest.mark.parametrize("sample,handle", [
    ("tracked by necros", "necros"),
    ("written and tracked by necros", "necros"),
    ("Composed & Tracked by SMASH", "SMASH"),
    ("tracked 2001-01-08 by eSeMGy", "eSeMGy"),
])
def test_music_credits_tracked_is_music(sample, handle):
    assert demozoo._music_credits([sample]) == [handle]


def test_music_credits_cross_slot_music_word_suppresses_veto():
    """A previous slot that pairs a role with a MUSIC word is not a veto —
    "music & gfx / by X" means X did the music (too)."""
    assert demozoo._music_credits(["music & gfx", "by artist"]) == ["artist"]


def test_music_credits_non_str_slot_is_safe():
    """A non-str slot (int/None/dict) must not raise — it would abort the whole
    batch apply.  Guarded to empty; a valid credit alongside still resolves."""
    assert demozoo._music_credits([123, "by artist", None, {"x": 1}]) == ["artist"]


# ── lookup_scener: sole-producer shared handle resolves without a title ───────
# A handle lands in ambig_prod when >1 releaser carries it, but only the ones
# who RELEASED something appear — so a "shared" handle often has a single real
# candidate.  It must resolve outright (the productionless namesakes can't be a
# music track's author); requiring a title match stranded famous handles like
# "Purple Motion" (→ Jonne Valtonen) whose title was single-token.  In the test
# index, "zap" is shared by 100 (musician) + 200 (coder), but only 100's MUSIC
# production reaches ambig_prod — so "zap" resolves to 100 with no title.
def test_lookup_scener_sole_producer_resolves_without_title(tmp_path, monkeypatch):
    _built_index(tmp_path, monkeypatch)
    card = demozoo.lookup_scener("zap")            # NO title
    assert card and card["releaser_id"] == 100 and card["real_name"] == "Ann Smith"
    # and still resolves the same with a title present
    card2 = demozoo.lookup_scener("zap", "Zap Tune Deluxe")
    assert card2 and card2["releaser_id"] == 100
    # a handle no one released is still unknown
    assert demozoo.lookup_scener("nobody-here") is None


def test_lookup_scener_real_name_paren_handle_prefers_primary(tmp_path, monkeypatch):
    """"Real Name (Handle)" whose parenthetical collides with a DIFFERENT scener
    must resolve to the authoritative pre-paren name, not refuse.  In the test
    index "alpha" (400), "beta" (500), "moby" (300) are distinct unique sceners;
    "alpha (moby)" pools both → disagree → old code refused.  Now the
    paren-stripped primary wins (regardless of which side the collision is on)."""
    _built_index(tmp_path, monkeypatch)
    assert demozoo.lookup_scener("alpha (moby)", "x")["releaser_id"] == 400
    assert demozoo.lookup_scener("beta (moby)", "x")["releaser_id"] == 500


# ── reset_enrichment: withdraw enrichment, preserve file tags + user edits ────
def test_reset_enrichment_withdraws_only_enrichment(monkeypatch):
    """Clears the Demozoo year backfill (→ year_file), scene_group, and the
    no-artist-module composer; leaves user hand-edits, user years, real
    file-tag composers (non-retro), and already-clean tracks untouched."""
    tracks = [
        {"id": "1", "format": "ProTracker", "artist": "", "composer": "4-mat",
         "scene_group": "Anarchy • Cosine", "year": 1991,
         "year_source": "demozoo", "year_file": 2007},
        {"id": "2", "format": "ScreamTracker 3", "artist": "Purple Motion",
         "composer": "", "scene_group": "Future Crew", "year": 1993,
         "year_source": "demozoo", "year_file": None},
        {"id": "3", "format": "ProTracker", "artist": "", "composer": "My Fix",
         "scene_group": "My Crew", "user_edited": ["composer", "scene_group"]},
        {"id": "4", "format": "ProTracker", "artist": "", "year": 1988,
         "year_source": "user", "year_file": 1990},
        {"id": "5", "format": "MP3", "artist": "", "composer": "J.S. Bach"},
        {"id": "6", "format": "ProTracker", "artist": "", "composer": "",
         "scene_group": ""},
    ]
    import soniqboom.core.store as store_mod
    monkeypatch.setattr(store_mod, "get_store", lambda: _FakeStore(tracks))
    count, batch = demozoo.reset_enrichment()
    bd = dict(batch)
    assert count == 2
    assert bd["1"] == {"year": 2007, "year_source": None, "year_file": None,
                       "scene_group": None, "composer": None}
    assert bd["2"] == {"year": None, "year_source": None, "year_file": None,
                       "scene_group": None}          # empty composer not touched
    for untouched in ("3", "4", "5", "6"):           # user edits / user year /
        assert untouched not in bd                   # file tag / already clean
    # idempotent: applying the batch (no scene_group/composer/demozoo-year left)
    # yields nothing on a second pass
    cleaned = []
    for t in tracks:
        t = dict(t)
        t.update(bd.get(t["id"], {}))
        cleaned.append(t)
    monkeypatch.setattr(store_mod, "get_store", lambda: _FakeStore(cleaned))
    assert demozoo.reset_enrichment()[0] == 0


def test_auto_apply_toggle_roundtrip(monkeypatch):
    """The post-scan auto-apply preference defaults ON, persists both ways, and
    surfaces in status() — it drives the admin toggle and the scanner guard that
    keeps a Reset from being undone by the next scan."""
    import soniqboom.config as cfg
    mem: dict = {}
    monkeypatch.setattr(cfg, "load_prefs", lambda: dict(mem))
    monkeypatch.setattr(cfg, "save_prefs", lambda d: (mem.clear(), mem.update(d)))
    assert demozoo.auto_apply_enabled() is True          # default ON
    demozoo.set_auto_apply(False)                        # Reset turns it off
    assert demozoo.auto_apply_enabled() is False
    assert demozoo.status()["auto_apply"] is False
    demozoo.set_auto_apply(True)                         # Apply turns it back on
    assert demozoo.auto_apply_enabled() is True
