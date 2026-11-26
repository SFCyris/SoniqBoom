# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One identity per scene musician (core/composers.py) and the views on it.

A synthetic library with the credit styles the live one has — a SID's own
author, HVSC's folder ("Tel_Jeroen" listed "Tel Jeroen" until a play),
Modland's SNDH folder ("Daglish_Ben"), transliterated folders
("Huelsbeck_Chris" / "Chris Hülsbeck"), "Real Name (Handle)", "Handle/Group",
collaborations, mainstream composer tags — and a synthetic Demozoo export
(real names, nick variants, namesakes, groups, parties with dates and series):

* the Scene → Composers list holds the scene musicians, one row each, and
  no mainstream composer tag; each row's count is what its drill-down lists;
* the Artists view folds a musician's spellings into one row;
* ``composer:`` and the composer search find a musician by any name;
* the composer page's payload; the ``place:`` / ``compo:`` operators; the
  chips; the Discover list, "On this day" and the parties coming up;
* the identities are rebuilt after a write (cold / warm, different inputs)
  and agree with HVSC's learn-on-play relabelling.
No network: the Demozoo index is built from a local dump.
"""
from __future__ import annotations

import copy
import datetime
import gzip
import hashlib
import sqlite3
import urllib.request
import uuid

import httpx
import pytest
from fastapi import FastAPI

from soniqboom.api import artist as artist_api
from soniqboom.api import library, search
from soniqboom.api import tracks as tracks_api
from soniqboom.api.search import _parse_advanced_query, search_predicates
from soniqboom.core import availability, catalogue, composers, demozoo, remote_cache
from soniqboom.core import scene_discovery as sd
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _ph(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


_HVSC_ROOT = "https://6581.nl/C64Music"
_ML_ROOT = "ftp://ftp.modland.com/pub/modules"


def _row(path, fmt, artist="", *, composer="", title=None, root="/m", **kw):
    tid = str(uuid.uuid5(uuid.NAMESPACE_URL, path))
    d = {"id": tid, "path": path, "title": title or path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
         "artist": artist, "composer": composer, "format": fmt, "added_at": 1_700_000_000,
         "mtime": 1.7e9, "duration": 120.0, "file_size": 1000,
         "dir_hash": _ph(path.rsplit("/", 1)[0]), "scan_root_hash": _ph(root),
         "is_duplicate_primary": True}
    d.update(kw)
    return d


def _hvsc(rel, artist, **kw):
    # (an HVSC online tune: unplayed — mtime 0 — its credit is its folder's)
    return _row(f"{_HVSC_ROOT}:/{rel}", "SID", artist, root=_HVSC_ROOT, mtime=0, **kw)


def _ml(rel, fmt, artist, **kw):
    return _row(f"{_ML_ROOT}:/{rel}", fmt, artist, root=_ML_ROOT, mtime=0, scene_path=rel, **kw)


ROWS = [
    # Ben Daglish: the SID header, HVSC's folder (unplayed), Modland's SNDH folder
    _row("/m/c64/Daglish/Gauntlet.sid", "SID", "Ben Daglish", year=1986),
    _row("/m/c64/Daglish/Trap.sid", "SID", "Ben Daglish", year=1986),
    _hvsc("MUSICIANS/D/Daglish_Ben/Deflektor.sid", "Daglish Ben", year=1987),
    _ml("SNDH/Daglish_Ben/the_last_ninja.sndh", "SNDH", "Daglish_Ben", year=1988),
    # a collaboration: under each of them
    _row("/m/c64/Hubbard/Commando.sid", "SID", "Rob Hubbard & Ben Daglish", year=1985),
    _row("/m/c64/Hubbard/Monty.sid", "SID", "Rob Hubbard", year=1985),
    _ml("SNDH/Hubbard_Rob/goldrunner.sndh", "SNDH", "Hubbard_Rob", year=1987),
    # Chris Hülsbeck: four spellings, his CD album too
    _row("/m/am/Huelsbeck/turrican.mod", "ProTracker", "Chris Huelsbeck", year=1990,
         scene_party="Revision 2019", scene_compo="Amiga Music", scene_place=1),
    _row("/m/c64/Huelsbeck/Shades.sid", "SID", "Chris Hülsbeck", year=1986),
    _ml("SNDH/Huelsbeck_Chris/r_type.sndh", "SNDH", "Huelsbeck_Chris", year=1989),
    _row("/m/cd/Huelsbeck/01 Turrican.flac", "FLAC", "Chris Huelsbeck", year=2013),
    # Jeroen Tel: HVSC's folder before a play, the header after
    _hvsc("MUSICIANS/T/Tel_Jeroen/Cybernoid.sid", "Tel Jeroen", year=1988),
    _row("/m/c64/Tel/Myth.sid", "SID", "Jeroen Tel", year=1989,
         scene_party="X 2019", scene_compo="C64 Music", scene_place=2),
    # Demozoo: Reyn Ouwehand is Romeo Knight
    _row("/m/c64/Ouwehand/Last Ninja 3.sid", "SID", "Reyn Ouwehand", year=1991),
    _row("/m/am/rk/rk-tune.mod", "ProTracker", "Romeo Knight", year=1992),
    # "Real Name (Handle)" filed by HVSC under the handle, and the handle alone
    _row("/m/hvsc/C64Music/MUSICIANS/D/Drax/Hawkeye.sid", "SID", "Thomas Mogensen (DRAX)",
         year=1988),
    _ml("Protracker/Drax/drax-tune.mod", "ProTracker", "Drax", year=1993),
    # squashed / dashed handles
    _row("/m/am/4mat/madness.mod", "ProTracker", "4-Mat", year=1993,
         scene_party="Assembly 1993", scene_compo="4-Channel Music", scene_place=3),
    _ml("Protracker/4mat/empty.mod", "ProTracker", "4mat", year=1994),
    # a handle and its group after a slash; the group known to the library
    _row("/m/c64/Laxity/tune.sid", "SID", "Laxity/Vibrants", year=1995),
    _row("/m/c64/Laxity/other.sid", "SID", "Laxity", year=1996, scene_group="Vibrants"),
    # namesakes on Demozoo: no Demozoo link, never merged with Jester the other
    _row("/m/am/jester/stardust.mod", "ProTracker", "Jester", year=1992,
         scene_party="Revision 2020", scene_compo="Amiga Music", scene_place=1),
    # a group credited as the artist (Demozoo: a group) — no composer row
    _row("/m/c64/mon/mon.sid", "SID", "Maniacs of Noise", year=1990),
    # placeholders
    _row("/m/c64/x/unknown.sid", "SID", "<?>"),
    _row("/m/am/x/unknown.mod", "ProTracker", "_unknown"),
    # mainstream: composer tags and an artist that never wrote a scene tune
    _row("/m/pop/a.mp3", "MP3", "Someone", composer="(French folk song)"),
    _row("/m/pop/b.mp3", "MP3", "A. Davis & Kanye West", composer="A. Davis & Kanye West"),
    _row("/m/pop/c.flac", "FLAC", "Pop Band", composer="Pop Writer"),
    # chips
    _row("/m/zx/tune.ay", "AY", "Zx Composer", year=1987,
         scene_party="Revision 2019", scene_compo="Oldskool Music", scene_place=2),
    _row("/m/a8/tune.sap", "SAP", "Atari Composer", year=1988),
    _row("/m/snes/tune.spc", "SPC", "Koji Kondo", year=1991),
    _row("/m/pc/tune.hsc", "HSC AdLib", "Adlib Composer", year=1993),
    _row("/m/nes/tune.nsf", "NSF", "Koji Kondo", year=1986),
    # a MIDI cover: no scene tune
    _row("/m/midi/song.mid", "MIDI", "Midi Arranger"),
]


def _dump(tmp_path):
    """A small Demozoo export: Romeo Knight (Reyn Ouwehand), Jeroen Tel (+ a
    two-letter abbreviation), 4-Mat (variant "4mat"), two sceners named
    Jester, groups, music with placings, parties with dates and series."""
    text = (
        "COPY public.demoscene_releaser (id, name, first_name, surname, is_group, notes, location) FROM stdin;\n"
        "100\tRomeo Knight\tReyn\tOuwehand\tf\tC64 and Amiga musician.\tNetherlands\n"
        "101\tJeroen Tel\tJeroen\tTel\tf\t\\N\tNetherlands\n"
        "102\t4-Mat\tMatthew\tSimmonds\tf\t\\N\tUK\n"
        "103\tJester\tVolker\tRust\tf\t\\N\t\\N\n"
        "104\tJester\tSomebody\tElse\tf\t\\N\t\\N\n"
        "105\tDrax\tThomas\tMogensen\tf\t\\N\tDenmark\n"
        "200\tVibrants\t\\N\t\\N\tt\t\\N\t\\N\n"
        "201\tManiacs of Noise\t\\N\t\\N\tt\t\\N\t\\N\n\\.\n"
        "COPY public.demoscene_releaserexternallink (id, link_class, parameter, releaser_id, source) FROM stdin;\n"
        "1\tCsdbScener\t1234\t100\t\\N\n\\.\n"
        "COPY public.demoscene_membership (id, member_id, group_id, is_current, data_source) FROM stdin;\n"
        "1\t100\t200\tt\t\\N\n2\t101\t201\tt\t\\N\n3\t105\t200\tf\t\\N\n\\.\n"
        "COPY public.demoscene_nick (id, releaser_id, name, abbreviation, differentiator) FROM stdin;\n"
        "1000\t100\tRomeo Knight\tRK\t\n1001\t100\tReyn\t\t\n"
        "1010\t101\tJeroen Tel\tJT\t\n1020\t102\t4-Mat\t\t\n"
        "1030\t103\tJester\t\t\n1040\t104\tJester\t\t\n1050\t105\tDrax\t\t\n"
        "2000\t200\tVibrants\t\t\n2010\t201\tManiacs of Noise\tMoN\t\n\\.\n"
        "COPY public.demoscene_nickvariant (id, nick_id, name, search_title) FROM stdin;\n"
        "1\t1020\t4mat\t4mat\n\\.\n"
        "COPY public.parties_competition (id, party_id, name, shown_date_date, shown_date_precision, platform_id, production_type_id) FROM stdin;\n"
        "70\t80\tC64 Music\t\\N\t\\N\t\\N\t\\N\n71\t81\tAmiga Music\t\\N\t\\N\t\\N\t\\N\n\\.\n"
        "COPY public.parties_competitionplacing (id, competition_id, production_id, ranking, \"position\", score) FROM stdin;\n"
        "1\t70\t5000\t1\t1\t\\N\n2\t71\t5001\t=2\t2\t\\N\n\\.\n"
        "COPY public.parties_party (id, name, party_series_id, start_date_date, start_date_precision, end_date_date, end_date_precision) FROM stdin;\n"
        "80\tX 2019\t2\t2019-11-22\td\t2019-11-24\td\n"
        "81\tRevision 2019\t1\t2019-04-19\td\t2019-04-22\td\n"
        "82\tRevision 2020\t1\t2020-04-10\td\t2020-04-13\td\n"
        "83\tRevision 2025\t1\t2025-04-18\td\t2025-04-21\td\n"
        "84\tRevision 2027\t1\t2027-03-26\td\t2027-03-29\td\n"
        "85\tAssembly 1993\t\\N\t1993-07-30\td\t1993-08-01\td\n"
        "86\tDatastorm 2025\t3\t2025-02-14\td\t2025-02-16\td\n"
        "87\tDatastorm 2026\t3\t2026-02-13\td\t2026-02-15\td\n"
        "88\tGubbdata 2024\t4\t2024-03-01\td\t2024-03-03\td\n"
        "89\tGubbdata 2025\t4\t2025-02-28\td\t2025-03-02\td\n"
        "90\tSomeparty 2019\t\\N\t2019-04-01\tm\t\\N\t\\N\n\\.\n"
        "COPY public.parties_partyseries (id, name, notes, website) FROM stdin;\n"
        "1\tRevision\t\t\n2\tX\t\t\n3\tDatastorm\t\t\n4\tGubbdata\t\t\n\\.\n"
        "COPY public.parties_party_releases (id, party_id, production_id) FROM stdin;\n"
        "1\t81\t5001\n\\.\n"
        "COPY public.platforms_platform (id, name) FROM stdin;\n1\tCommodore 64\n\\.\n"
        "COPY public.productions_production (id, title, release_date_date, release_date_precision, supertype) FROM stdin;\n"
        "5000\tCompo Tune\t2019-11-23\td\tmusic\n5001\tAmiga Tune\t2019-04-20\td\tmusic\n\\.\n"
        "COPY public.productions_production_author_nicks (id, production_id, nick_id) FROM stdin;\n"
        "1\t5000\t1000\n2\t5001\t1000\n\\.\n"
        "COPY public.productions_production_platforms (id, production_id, platform_id) FROM stdin;\n"
        "1\t5000\t1\n\\.\n"
        "COPY public.productions_production_types (id, production_id, productiontype_id) FROM stdin;\n\\.\n"
        "COPY public.productions_productionlink (id, link_class, parameter, production_id, is_download_link, description) FROM stdin;\n\\.\n"
        "COPY public.productions_productiontype (id, name) FROM stdin;\n50\tMusic\n\\.\n"
    )
    p = tmp_path / "demozoo.sql.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(text)
    return p


@pytest.fixture
def dz(tmp_path, monkeypatch):
    """The Demozoo index built from the local dump (no network)."""
    (tmp_path / "scene").mkdir()
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "scene" / "demozoo.sqlite")

    def _no_network(*a, **k):
        raise AssertionError("asked the network")
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    st = demozoo.refresh_index(_dump(tmp_path))
    assert not st.get("error"), st
    return tmp_path / "scene" / "demozoo.sqlite"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """THE store with ``ROWS`` (the online collections' roots registered),
    the list caches emptied."""
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    # (no Demozoo index unless a test asks for one — ``dz``, listed after)
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "no-index" / "demozoo.sqlite")
    monkeypatch.setattr(remote_cache, "_cache", remote_cache.RemoteCache(tmp_path / "rc", 64))
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    catalogue.register_root(_HVSC_ROOT, key="hvsc", label="HVSC")
    catalogue.register_root(_ML_ROOT, key="modland", label="Modland")
    for r in ROWS:
        s.upsert_track(copy.deepcopy(r))
    yield s
    catalogue.unregister_root(_HVSC_ROOT)
    catalogue.unregister_root(_ML_ROOT)
    availability.reset_for_tests()


def _client():
    app = FastAPI()
    for mod in (tracks_api, library, search, artist_api):
        app.include_router(mod.router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _ident(s, name):
    return s.composer_index().find(name)


def _titles(s, ids):
    return sorted(s._tracks[i]["title"] for i in ids)


# ── the name rules ──────────────────────────────────────────────────────────
def test_spellings_fold_case_accents_and_hvscs_transliteration():
    assert composers.ordered_keys("Chris Hülsbeck") == ["chris hulsbeck", "chris huelsbeck"]
    assert composers.ordered_keys("Huelsbeck_Chris") == ["huelsbeck chris"]
    assert composers.ordered_keys("Karl Bjørnar Øie") == ["karl bjornar oie", "karl bjoernar oeie"]
    assert composers.squash_keys("4-Mat") == ["4mat"] and composers.squash_keys("JT") == []
    assert composers.order_free_key("Stephen L. Judd") == composers.order_free_key("Judd_Stephen")
    assert composers.order_free_key("Wyndex") is None


@pytest.mark.parametrize("raw,want", [
    ("Rob Hubbard & Ben Daglish", ["Rob Hubbard", "Ben Daglish"]),
    ("A; B", ["A", "B"]), ("A + B", ["A", "B"]), ("A vs. B", ["A", "B"]),
    ("Rob Hubbard • Ben Daglish", ["Rob Hubbard", "Ben Daglish"]),
    ("Laxity/Vibrants", ["Laxity/Vibrants"]),        # (guarded: decided against the library)
    ("<?> & Someone", ["Someone"]),
])
def test_credit_lists_split_at_their_separators(raw, want):
    assert list(composers.credit_parts(raw)) == want


@pytest.mark.parametrize("raw,want", [
    ("Stephen L. Judd (Wyndex)", ("Stephen L. Judd", "Wyndex")),
    ("Rob Hubbard (remix)", ("Rob Hubbard", "remix")),
    ("Rob Hubbard", ("Rob Hubbard", None)),
])
def test_a_bracket_beside_a_name(raw, want):
    assert composers.split_parenthetical(raw) == want
    assert composers.handle_like("Wyndex") and not composers.handle_like("remix")
    assert not composers.handle_like("Maniacs of Noise") and not composers.handle_like("1987")


# ── the identities ──────────────────────────────────────────────────────────
def test_one_identity_per_musician_without_demozoo(lib):
    s = lib
    ben = _ident(s, "Ben Daglish")
    assert ben.name == "Ben Daglish"
    assert _titles(s, ben.ids) == ["Commando", "Deflektor", "Gauntlet", "Trap", "the_last_ninja"]
    assert _ident(s, "Daglish_Ben") is ben and _ident(s, "daglish ben") is ben
    chris = _ident(s, "Huelsbeck_Chris")
    assert chris.name == "Chris Huelsbeck"
    # (his CD album too: its artist tag is one of his names)
    assert _titles(s, chris.ids) == ["01 Turrican", "Shades", "r_type", "turrican"]
    assert chris.n_scene == 3                        # (the FLAC is no scene tune)
    # HVSC's unplayed folder credit ("Last First") joins the header's name
    tel = _ident(s, "Jeroen Tel")
    assert tel is _ident(s, "Tel Jeroen") and _titles(s, tel.ids) == ["Cybernoid", "Myth"]
    rob = _ident(s, "Rob Hubbard")
    assert _titles(s, rob.ids) == ["Commando", "Monty", "goldrunner"]
    # "Real Name (Handle)" is both
    drax = _ident(s, "DRAX")
    assert drax is _ident(s, "Thomas Mogensen") and _titles(s, drax.ids) == ["Hawkeye", "drax-tune"]
    mat = _ident(s, "4mat")
    assert mat is _ident(s, "4-Mat") and len(mat.ids) == 2
    # "Laxity/Vibrants": Vibrants is a group the library knows — Laxity's
    lax = _ident(s, "Laxity")
    assert _titles(s, lax.ids) == ["other", "tune"] and _ident(s, "Vibrants") is None


def test_the_list_holds_scene_musicians_and_no_mainstream_tag(lib):
    names = {r["composer"] for r in lib.aggregate_facet("composer")}
    for gone in ("(French folk song)", "A. Davis & Kanye West", "A. Davis", "Kanye West",
                 "Pop Writer", "Pop Band", "Someone", "Midi Arranger", "<?>", "_unknown",
                 "Daglish_Ben", "Daglish Ben", "Huelsbeck_Chris", "Tel Jeroen", "Hubbard_Rob"):
        assert gone not in names, gone
    assert {"Ben Daglish", "Rob Hubbard", "Chris Huelsbeck", "Jeroen Tel", "Koji Kondo",
            "4-Mat", "Laxity"} <= names
    # (no Demozoo index: a group credited as the artist is still a name)
    assert "Maniacs of Noise" in names


def test_demozoo_joins_real_names_and_handles_and_refuses_namesakes(lib, dz):
    s = lib
    rk = _ident(s, "Reyn Ouwehand")
    assert rk is _ident(s, "Romeo Knight")
    assert rk.name == "Romeo Knight" and rk.real_name == "Reyn Ouwehand" and rk.releaser_id == 100
    assert _titles(s, rk.ids) == ["Last Ninja 3", "rk-tune"]
    # a two-letter abbreviation is no evidence: a tune credited "JT" stays apart
    s.upsert_track(_row("/m/am/jt/jt.mod", "ProTracker", "JT"))
    jt, tel = _ident(s, "JT"), _ident(s, "Jeroen Tel")
    assert jt is not tel and [s._tracks[i]["title"] for i in jt.ids] == ["jt"]
    assert len(tel.ids) == 2
    # namesakes give no Demozoo link
    jester = _ident(s, "Jester")
    assert jester.releaser_id is None and jester.name == "Jester"
    # the variant "4mat" and the nick "4-Mat" are one person: Demozoo's name
    assert _ident(s, "4mat").name == "4-Mat" and _ident(s, "4mat").releaser_id == 102
    # a credit Demozoo knows only as a group: no composer
    names = {r["composer"] for r in s.aggregate_facet("composer")}
    assert "Maniacs of Noise" not in names and "Romeo Knight" in names
    assert "Reyn Ouwehand" not in names


def test_learn_on_play_keeps_the_identity(lib):
    s = lib
    tel = _ident(s, "Jeroen Tel")
    before = (tel.name, set(tel.ids))
    cyber = next(i for i in tel.ids if s._tracks[i]["title"] == "Cybernoid")
    # (online_hvsc.after_merge: the play reads the header and relabels)
    s.update_track_fields(cyber, {"artist": "Jeroen Tel", "mtime": 1.7e9})
    after = _ident(s, "Jeroen Tel")
    assert after is not tel                         # (a write: built again)
    assert (after.name, set(after.ids)) == before


def test_cold_then_warm_then_a_write_with_another_input(lib):
    s = lib
    calls = []
    real = composers.build_from

    def spy(snap, **kw):
        calls.append(1)
        return real(snap, **kw)
    composers.build_from = spy
    try:
        a = s.composer_index()                      # cold
        b = s.composer_index()                      # warm: the same object
        assert a is b and len(calls) == 1
        rows1 = s.aggregate_facet("composer")
        assert s.aggregate_facet("composer") is rows1
        # another input: a new spelling of Rob Hubbard on a new SNDH
        s.upsert_track(_ml("SNDH/Hubbard_Rob/new.sndh", "SNDH", "Hubbard_Rob"))
        c = s.composer_index()
        assert c is not a and len(calls) == 2
        assert {r["composer"]: r["count"] for r in s.aggregate_facet("composer")}["Rob Hubbard"] == 4
        # a write no identity reads (a rating) builds nothing
        s.set_rating(next(iter(c.by_key["rob hubbard"].ids)), 5)
        assert s.composer_index() is c and len(calls) == 2
    finally:
        composers.build_from = real


# ── the endpoints ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("dedup", [False, True])
async def test_rows_counts_and_drilldowns_agree(lib, dedup):
    lib.set_config("filter_duplicates", dedup)
    async with _client() as c:
        rows = (await c.get("/api/library/composers")).json()
        counts = (await c.get("/api/library/counts")).json()
        assert counts["composers"] == len(rows)
        for r in rows:
            got = (await c.get("/api/search/filter",
                               params={"composer": r["composer"], "limit": 2000})).json()
            assert len(got) == r["count"], r
            page = (await c.get("/api/search/filter", params={
                "composer": r["composer"], "order": "platform", "limit": 2000})).json()
            assert sorted(t["id"] for t in page) == sorted(t["id"] for t in got)
        # an alias opens the same musician
        a = (await c.get("/api/search/filter", params={"composer": "Daglish_Ben"})).json()
        b = (await c.get("/api/search/filter", params={"composer": "Ben Daglish"})).json()
        assert sorted(t["id"] for t in a) == sorted(t["id"] for t in b) and len(a) == 5


async def test_the_artists_view_folds_a_musicians_spellings(lib):
    async with _client() as c:
        rows = (await c.get("/api/library/artists")).json()
        counts = (await c.get("/api/library/counts")).json()
        comp = {r["composer"]: r["count"] for r in (await c.get("/api/library/composers")).json()}
    names = [r["artist"] for r in rows]
    for gone in ("Daglish_Ben", "Daglish Ben", "Huelsbeck_Chris", "Chris Hülsbeck",
                 "Tel Jeroen", "Hubbard_Rob", "4mat"):
        assert gone not in names, gone
    by = {r["artist"]: r for r in rows}
    assert by["Ben Daglish"]["composer"] == "Ben Daglish"
    assert by["Ben Daglish"]["count"] == comp["Ben Daglish"] == 5
    assert by["Chris Huelsbeck"]["count"] == 4
    # rows of no scene musician stay as they were (a collaboration is a tag)
    assert "composer" not in by["Pop Band"] and "composer" not in by["Rob Hubbard & Ben Daglish"]
    assert counts["artists"] == sum(1 for r in rows if not r.get("label"))
    assert names == sorted(names, key=str.lower)


async def test_search_finds_a_musician_by_any_name(lib, dz):
    async with _client() as c:
        def ids(q):
            return c.get("/api/search/filter", params={"q": q, "limit": 2000})
        hub = (await ids("composer:hubbard")).json()
        assert sorted(t["title"] for t in hub) == ["Commando", "Monty", "goldrunner"]
        rk = (await ids('composer:"romeo knight"')).json()
        assert sorted(t["title"] for t in rk) == ["Last Ninja 3", "rk-tune"]
        hul = (await ids("composer:hülsbeck")).json()
        assert len(hul) == 4
        # (the composer tag still counts)
        assert [t["title"] for t in (await ids("composer:writer")).json()] == ["c"]
        found = (await c.get("/api/artist/composers", params={"q": "ouwehand"})).json()
        assert found[0]["name"] == "Romeo Knight" and found[0]["also"] == "Reyn Ouwehand"
        found = (await c.get("/api/artist/composers", params={"q": "Hülsbeck"})).json()
        assert [f["name"] for f in found] == ["Chris Huelsbeck"] and found[0]["also"] == ""
        assert (await c.get("/api/artist/composers", params={"q": "kanye"})).json() == []


async def test_the_composer_page(lib, dz):
    async with _client() as c:
        page = (await c.get("/api/artist/composer", params={"name": "Reyn Ouwehand"})).json()
        assert page["found"] and page["name"] == "Romeo Knight"
        assert page["real_name"] == "Reyn Ouwehand" and page["handles"] == ["Romeo Knight", "Reyn"]
        assert page["groups"] == ["Vibrants"] and page["location"] == "Netherlands"
        assert page["results_total"] == 2
        # (newest year first; a year's parties A–Z)
        assert [(r["rank"], r["competition"], r["party"]) for r in page["results"]] == [
            ("=2", "Amiga Music", "Revision 2019"), ("1", "C64 Music", "X 2019")]
        labels = [lk["label"] for lk in page["links"]]
        assert labels[:2] == ["Demozoo", "CSDb"]
        assert page["count"] == 2 and page["scene"] == 2
        assert [(p["platform"], p["count"]) for p in page["platforms"]] in (
            [("Amiga", 1), ("SID", 1)], [("SID", 1), ("Amiga", 1)])
        assert page["format_platform"] == {"SID": "SID", "ProTracker": "Amiga"}
        # HVSC and Modland folders where the tunes are
        ben = (await c.get("/api/artist/composer", params={"name": "Daglish Ben"})).json()
        urls = [lk["url"] for lk in ben["links"]]
        assert "https://6581.nl/C64Music/MUSICIANS/D/Daglish_Ben/" in urls
        assert "https://ftp.modland.com/pub/modules/SNDH/Daglish_Ben/" in urls
        assert ben["aliases"] == ["Daglish Ben"] or "Daglish Ben" in ben["aliases"]
        # his page lists by platform: the biggest group first, years within
        rows = (await c.get("/api/search/filter", params={
            "composer": "Ben Daglish", "order": "platform", "limit": 2000})).json()
        assert [t["title"] for t in rows] == ["Commando", "Gauntlet", "Trap", "Deflektor",
                                              "the_last_ninja"]
        assert (await c.get("/api/artist/composer", params={"name": "Kanye West"})).json() == {
            "found": False, "name": "Kanye West"}
        # Track Info's Scene tab names the page
        card = (await c.get("/api/artist/scene", params={"name": "Tel Jeroen", "retro": 1})).json()
        assert card["composer"] == {"name": "Jeroen Tel", "count": 2}


# ── place: / compo: ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("q,want", [
    ("place:1", {"place_min": 1, "place_max": 1}),
    ("place:<=3", {"place_max": 3}),
    ("place:2-3", {"place_min": 2, "place_max": 3}),
    # spaced forms, like ``year: <=1990`` (the range fields join them up)
    ("place: <=3", {"place_max": 3}),
    ("place:<= 3", {"place_max": 3}),
    ("place: 1 - 3", {"place_min": 1, "place_max": 3}),
    ('compo:"amiga music"', {"compo_match": "amiga music"}),
    ("compo:oldskool", {"compo_match": "oldskool"}),
    ("-place:1", {"negate": ((("place_max", 1), ("place_min", 1)),)}),
])
def test_place_and_compo_parse(q, want):
    assert search_predicates(q) == want


@pytest.mark.parametrize("q", ["place:first", "place:abc"])
def test_an_unreadable_place_stays_text(q):
    assert search_predicates(q) == {"query": q}


async def test_place_and_compo_find_the_placed_tunes(lib):
    async with _client() as c:
        async def titles(q, **kw):
            r = await c.get("/api/search/filter", params={"q": q, "limit": 2000, **kw})
            return [t["title"] for t in r.json()]
        assert sorted(await titles("place:1")) == ["stardust", "turrican"]
        assert sorted(await titles("place:<=3")) == ["Myth", "madness", "stardust", "tune",
                                                     "turrican"]
        assert sorted(await titles('compo:"amiga music"')) == ["stardust", "turrican"]
        assert await titles("compo:oldskool") == ["tune"]
        # the winners' order: newest party first, by competition and place
        # (the party's year, not the tune's: Revision 2020 before 2019's)
        assert await titles("place:<=3", order="compo") == [
            "stardust", "turrican", "tune", "Myth", "madness"]
        sh = (await c.get("/api/tracks/shuffled", params={"q": "place:1", "seed": 3})).json()
        assert sh["total"] == 2
        summary = (await c.get("/api/library/compo-summary", params={"place_max": 3})).json()
        assert summary["total"] == 5
        assert {p["key"]: p["count"] for p in summary["chips"]}["paula"] == 3
        got = await titles("place:<=3 chip:paula", order="compo")
        assert got == ["stardust", "turrican", "madness"]
    # a placing found later moves the results (``_search_seq``)
    myth = next(t for t in lib._tracks.values() if t["title"] == "Myth")
    lib.update_track_fields(myth["id"], {"scene_place": 1})
    assert len(lib.filter_track_ids(place_min=1, place_max=1)) == 3


# ── chips ───────────────────────────────────────────────────────────────────
async def test_the_chips_and_their_tunes(lib):
    async with _client() as c:
        rows = (await c.get("/api/library/chips")).json()
        counts = (await c.get("/api/library/counts")).json()
        by = {r["key"]: r for r in rows}
        assert counts["chips"] == len(rows)
        assert {"sid", "paula", "ym2149", "ay", "pokey", "spc700", "opl", "2a03"} <= set(by)
        for r in rows:
            got = (await c.get("/api/search/filter", params={"q": f"chip:{r['key']}",
                                                             "limit": 2000})).json()
            assert len(got) == r["count"], r
        assert by["ay"]["count"] == 1 and by["opl"]["count"] == 1
        assert by["ym2149"]["count"] == 3             # (the SNDH tunes)
        for alias, key in (("adlib", "opl"), ("snes", "spc700"), ("amiga", "paula"),
                           ("nes", "2a03"), ("ay-3-8910", "ay")):
            assert search_predicates(f"chip:{alias}") == {"chip": key}


# ── Discover: on this day, parties coming up, presets ────────────────────────
def test_the_party_calendar_and_on_this_day(lib, dz):
    cal = demozoo.party_calendar()
    rev = next(p for p in cal if p["name"] == "Revision 2019")
    assert rev["series"] == "Revision" and rev["start"] == datetime.date(2019, 4, 19)
    some = next(p for p in cal if p["name"] == "Someparty 2019")
    assert some["start"] is None                    # (known to the month only: no day)
    today = datetime.date(2026, 11, 25)
    # (19–22 April 2019; 2025's ran 18–21 April)
    assert [p["name"] for p in sd.parties_on(cal, 4, 22, today)] == ["Revision 2019"]
    assert [p["name"] for p in sd.parties_on(cal, 4, 21, today)] == ["Revision 2025",
                                                                       "Revision 2019"]
    assert [p["name"] for p in sd.parties_on(cal, 11, 23, today)] == ["X 2019"]
    assert sd.parties_on(cal, 4, 1, today) == []    # (Someparty's day isn't known)
    assert sd.parse_day("02-29", today) == (2, 29) and sd.parse_day("13-01", today) == (11, 25)


def test_the_parties_coming_up(dz):
    cal = demozoo.party_calendar()
    up = sd.upcoming(cal, datetime.date(2026, 1, 20), 6)
    by = {u["series"]: u for u in up}
    # a dated edition the index lists; an anniversary of a yearly series
    assert by["Datastorm"]["scheduled"] and by["Datastorm"]["date"] == "2026-02-13"
    assert by["Datastorm"]["past"] == ["Datastorm 2025"]
    assert not by["Gubbdata"]["scheduled"] and by["Gubbdata"]["date"] == "2026-02-28"
    # Revision's next is dated beyond the window; X was held once, long ago
    assert "Revision" not in by and "X" not in by
    up = sd.upcoming(cal, datetime.date(2027, 3, 1), 6)
    assert {u["series"] for u in up} >= {"Revision"}


async def test_the_discover_list_and_its_views(lib, dz, monkeypatch):
    # (Revision 2027 starts on 26 March: within six weeks of 1 March)
    monkeypatch.setattr(library, "_today", lambda: datetime.date(2027, 3, 1))
    async with _client() as c:
        rows = (await c.get("/api/library/discover", params={"date": "04-20"})).json()
        kinds = {r["kind"]: r for r in rows}
        by = {r["discover"]: r for r in rows}
        assert by["Compo winners — every party"]["count"] == 2
        assert by["Top three at every party"]["count"] == 5
        assert kinds["on-this-day"]["count"] == 2 and kinds["on-this-day"]["date"] == "04-20"
        # (Revision — the others have no tunes here — and its past tunes)
        assert kinds["upcoming"]["parties"] == 1 and kinds["upcoming"]["count"] == 3
        assert all(r["count"] > 0 for r in rows)
        otd = (await c.get("/api/library/on-this-day", params={"date": "04-20"})).json()
        assert [p["name"] for p in otd["parties"]] == ["Revision 2019"]
        assert [t["title"] for t in otd["tracks"]] == ["turrican", "tune"]
        up = (await c.get("/api/library/upcoming-parties", params={"weeks": 26})).json()
        rev = next(s for s in up["series"] if s["series"] == "Revision")
        assert rev["scheduled"] and rev["date"] == "2027-03-26" and rev["count"] == 3
        one = (await c.get("/api/library/upcoming-parties", params={
            "weeks": 26, "series": rev["key"]})).json()
        assert [t["title"] for t in one["tracks"]] == ["stardust", "turrican", "tune"]


def test_every_preset_is_a_search_the_box_reads():
    for p in sd.presets(datetime.date(2026, 11, 25)):
        preds = search_predicates(p["q"])
        assert preds and "query" not in preds, p
        assert _parse_advanced_query(p["q"]) is not None


async def test_an_index_without_party_dates_asks_for_a_refresh(lib, dz):
    con = sqlite3.connect(dz)
    con.execute("DROP TABLE party_date")
    con.commit()
    con.close()
    demozoo._cal_memo = None
    async with _client() as c:
        otd = (await c.get("/api/library/on-this-day", params={"date": "04-20"})).json()
        assert otd["needs_index"] and otd["tracks"] == []
        rows = (await c.get("/api/library/discover")).json()
        assert any(r["kind"] == "needs-index" for r in rows)


# ── the Demozoo index layout ────────────────────────────────────────────────
def test_the_index_keeps_names_real_names_and_party_dates(dz):
    con = sqlite3.connect(dz)
    assert con.execute("PRAGMA user_version").fetchone()[0] == demozoo.INDEX_SCHEMA == 3
    kinds = {(k, rid, kind) for k, rid, kind in con.execute("SELECT key, releaser_id, kind FROM scener_name")}
    assert ("reyn ouwehand", 100, "real") in kinds and ("romeo knight", 100, "nick") in kinds
    assert ("4mat", 102, "variant") in kinds and ("maniacs of noise", 201, "group") in kinds
    rows = demozoo.identity_rows(["jester", "reyn ouwehand", "nobody"])
    assert sorted(rows["jester"]) == [(103, "nick"), (104, "nick")]
    assert rows["reyn ouwehand"] == [(100, "real")] and "nobody" not in rows
    det = demozoo.identity_details([100])
    assert det[100]["real_name"] == "Reyn Ouwehand" and det[100]["aliases"] == ["Romeo Knight", "Reyn"]
    assert demozoo.scener_details(100)["real_name"] == "Reyn Ouwehand"
    assert [p["place"] for p in demozoo.scener_placings(100)] == [2, 1]


# ── the review's cases: no merge on weak evidence ───────────────────────────
def _store_of(rows, monkeypatch, tmp_path, *, groups=()):
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    for k, (path, fmt, artist) in enumerate(rows):
        d = _row(path, fmt, artist)
        if groups and k == 0:
            d["scene_group"] = groups[0]
        s.upsert_track(d)
    return s


def test_a_bracket_joins_a_handle_only_on_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "none" / "demozoo.sqlite")
    s = _store_of([
        ("/m/a/1.sid", "SID", "Laxity (Vibrants)"), ("/m/a/2.sid", "SID", "JCH (Vibrants)"),
        ("/m/a/3.mod", "ProTracker", "Purple Motion (FC)"), ("/m/a/4.mod", "ProTracker", "Skaven (FC)"),
        ("/m/a/5.sid", "SID", "Martin Galway (Ocean)"), ("/m/a/6.sid", "SID", "Jonathan Dunn (Ocean)"),
        ("/m/a/7.sid", "SID", "Rob Hubbard (Ben Daglish)"), ("/m/a/8.sid", "SID", "Ben Daglish"),
        ("/m/a/9.sid", "SID", "Ocean"),
        # HVSC files it under the handle: the real name and the handle are one
        ("/m/C64Music/MUSICIANS/W/Wyndex/x.sid", "SID", "Stephen L. Judd (Wyndex)"),
        ("/m/C64Music/MUSICIANS/W/Wyndex/y.sid", "SID", "Wyndex"),
        # …under a group's folder, a member's own: the group is no handle
        ("/m/C64Music/MUSICIANS/V/Vibrants/Laxity/z.sid", "SID", "Laxity (Vibrants)"),
    ], monkeypatch, tmp_path)
    idx = s.composer_index()
    names = {i.name: sorted(s._tracks[t]["artist"] for t in i.ids) for i in idx.by_key.values()}
    assert idx.find("Laxity") is not idx.find("JCH")
    assert idx.find("Purple Motion") is not idx.find("Skaven")
    assert idx.find("Martin Galway") is not idx.find("Jonathan Dunn")
    assert idx.find("Martin Galway") is not idx.find("Ocean")
    assert idx.find("Rob Hubbard") is not idx.find("Ben Daglish")
    assert idx.find("Wyndex") is idx.find("Stephen L. Judd")
    assert "Vibrants" not in names and "FC" not in names, names


def test_demozoo_refuses_a_group_in_brackets(lib, dz):
    s = lib
    for path, artist in (("/m/c64/rk/v.sid", "Romeo Knight (Vibrants)"),
                         ("/m/c64/dx/v.sid", "Drax (Vibrants)")):
        s.upsert_track(_row(path, "SID", artist))
    idx = s.composer_index()
    assert idx.find("Romeo Knight") is not idx.find("Drax")
    assert idx.find("Romeo Knight").releaser_id == 100 and idx.find("Drax").releaser_id == 105


def test_a_mainstream_namesake_keeps_its_albums(monkeypatch, tmp_path):
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "none" / "demozoo.sqlite")
    s = _store_of([("/m/ml/moby.mod", "ProTracker", "Moby"), ("/m/pop/1.mp3", "MP3", "Moby"),
                   ("/m/pop/2.mp3", "MP3", "Moby"),
                   ("/m/ml/ch.mod", "ProTracker", "Chris Huelsbeck"),
                   ("/m/cd/ch.flac", "FLAC", "Chris Huelsbeck")], monkeypatch, tmp_path)
    idx = s.composer_index()
    moby = idx.find("Moby")
    assert [s._tracks[t]["format"] for t in moby.ids] == ["ProTracker"]
    assert "moby" not in idx.by_artist                 # (its Artists row stays as it was)
    assert len(idx.find("Chris Huelsbeck").ids) == 2   # (a two-word name: his CD too)
    assert set(s.filter_track_ids(composer_match="moby")) == moby.ids


def test_squashed_names_join_only_whole_words(monkeypatch, tmp_path):
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "none" / "demozoo.sqlite")
    s = _store_of([("/m/a/1.mod", "ProTracker", "Jo Hansen"), ("/m/a/2.mod", "ProTracker", "Johansen"),
                   ("/m/a/3.mod", "ProTracker", "Lizard King"),
                   ("/m/a/4.mod", "ProTracker", "Lizardking")], monkeypatch, tmp_path)
    idx = s.composer_index()
    assert idx.find("Jo Hansen") is not idx.find("Johansen")
    assert idx.find("Lizard King") is idx.find("Lizardking")


def test_a_folder_name_is_found_in_header_order(monkeypatch, tmp_path):
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "none" / "demozoo.sqlite")
    s = _store_of([("/m/ml/SNDH/Daglish_Ben/a.sndh", "SNDH", "Daglish_Ben")], monkeypatch, tmp_path)
    idx = s.composer_index()
    ident = idx.find("Ben Daglish")
    assert ident is not None and ident.name == "Daglish Ben"   # (no other spelling: as filed)
    assert len(s.filter_track_ids(composer_match="ben daglish")) == 1
    assert [i.name for i in idx.search("ben dag")] == ["Daglish Ben"]


async def test_a_stale_index_counts_what_it_lists(lib, monkeypatch):
    s = lib
    await s.composer_index_async()
    composers._last_build[s] = (__import__("time").monotonic(), 5.0)   # (a slow library)
    gone = next(t for t in s._tracks.values() if t["title"] == "Trap")
    s.delete_track(gone["id"])
    async with _client() as c:
        rows = {r["composer"]: r["count"] for r in (await c.get("/api/library/composers")).json()}
        drill = (await c.get("/api/search/filter", params={"composer": "Ben Daglish"})).json()
        arts = {r["artist"]: r["count"] for r in (await c.get("/api/library/artists")).json()}
    assert not s.composer_index_fresh()                # (served the index built before)
    assert rows["Ben Daglish"] == len(drill) == arts["Ben Daglish"] == 4
    timer = composers._scheduled.pop(s, None)
    if timer is not None:
        timer.cancel()


async def test_a_cached_composer_search_follows_a_rebuild(lib):
    s = lib
    await s.composer_index_async()
    async with _client() as c:
        def total(q):
            return c.get("/api/search/page", params={"q": q, "limit": 50})
        assert (await total("composer:newname")).json()["total"] == 0
        s.upsert_track(_row("/m/c64/new.sid", "SID", "Newname Composer"))
        s.composer_index()                              # (no loop wait: the next await builds)
        await s.composer_index_async()
        assert (await total("composer:newname")).json()["total"] == 1
