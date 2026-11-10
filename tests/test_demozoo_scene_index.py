# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The SCENE tab's details from the Demozoo export (no request to Demozoo's
API, which asks for a key since November 2026): the link URLs of every
Demozoo link type, the index's detail tables, the readers, the card built from
them, and the one-time rebuild of an index of an older layout."""
from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from soniqboom.core import demozoo, demozoo_links

_SAMPLES = Path(__file__).parent / "data" / "demozoo_link_samples.json"


# ── link URLs ───────────────────────────────────────────────────────────────
def test_every_demozoo_link_type_gives_demozoos_own_url():
    # (expected URLs made by Demozoo's own link code, one or more per type;
    # an ftp: or telnet: address is no web link — left out, as the page did)
    rows = json.loads(_SAMPLES.read_text("utf-8"))
    assert len({r[0] for r in rows}) == 149
    want_of = lambda cls, want: want if want.startswith(("http://", "https://")) else None  # noqa: E731
    wrong = [(cls, param, want, (demozoo_links.link(cls, param) or {}).get("url"))
             for cls, param, want in rows
             if (demozoo_links.link(cls, param) or {}).get("url") != want_of(cls, want)]
    assert wrong == []


@pytest.mark.parametrize("cls,param", [
    ("BaseUrl", "javascript:alert(1)"), ("BaseUrl", "data:text/html,x"),
    ("BaseUrl", "//evil.example/"), ("MastodonAccount", "javascript:alert(1)"),
    ("WikipediaPage", "vbscript:x"), ("TelnetLink", "bbs.example.org"),
    ("BandcampArtist", "evil.example/#"), ("BandcampArtist", "evil.example?"),
    ("BandcampArtist", "a.b"), ("DeviantartUser", "x@evil.example"),
    ("FujiologyFile", "@evil.example/x"), ("FujiologyFile", ".evil.example/x"),
    ("ModlandFile", ":80@evil.example/"), ("BandcampTrack", "evil.example?/x")])
def test_a_link_that_would_leave_its_site_or_the_web_is_left_out(cls, param):
    assert demozoo_links.link(cls, param) is None


def test_a_plain_web_link_and_a_subdomain_name_still_pass():
    assert demozoo_links.link("BaseUrl", "https://example.org/a?b=1")["url"] == \
        "https://example.org/a?b=1"
    assert demozoo_links.link("BandcampArtist", "Purple-Motion")["url"] == \
        "https://Purple-Motion.bandcamp.com/"
    assert demozoo_links.link("BandcampTrack", "pm/sundance")["url"] == \
        "https://pm.bandcamp.com/track/sundance"


def test_a_link_is_labelled_by_its_site():
    assert demozoo_links.link("BandcampArtist", "purplemotion") == {
        "url": "https://purplemotion.bandcamp.com/", "label": "Bandcamp",
        "class": "BandcampArtist"}
    assert demozoo_links.link("BaseUrl", "http://www.valtone.com/")["label"] == "valtone.com"
    assert demozoo_links.link("PouetProduction", "112")["label"] == "Pouët"


@pytest.mark.parametrize("cls,param", [
    ("NoSuchLinkType", "x"), ("BandcampArtist", ""), (None, "x"),
    ("BandcampTrack", "no-slash"), ("DiscogsRelease", "a/b/c"), ("GithubDirectory", "u/r")])
def test_an_unknown_type_or_a_malformed_parameter_gives_no_link(cls, param):
    assert demozoo_links.link(cls, param) is None


# ── the index ───────────────────────────────────────────────────────────────
def _dump(tmp_path: Path) -> Path:
    """A small export: Purple Motion (369) — nicks, a nick variant "PM",
    groups now and before, links — two music productions and a demo, their
    platforms, a placing, a release party and links; a namesake "PM" can't
    take the match."""
    rows = (
        "COPY public.demoscene_releaser (id, name, first_name, surname, is_group, notes, location) FROM stdin;\n"
        "369\tPurple Motion\tJonne\tValtonen\tf\t"
        "Worked on [Death Rally](https://demozoo.org/x/) (1996).\\r\\n\\r\\n* 1st best musician in **Imphobia 9**."
        "\tFinland\n"
        "10\tFuture Crew\t\\N\t\\N\tt\t\\N\t\\N\n11\ttAAt\t\\N\t\\N\tt\t\\N\t\\N\n"
        "12\tFive Musicians\t\\N\t\\N\tt\t\\N\t\\N\n"
        "500\tSomeone Else\tAl\tOther\tf\t\\N\t\\N\n\\.\n"
        "COPY public.demoscene_releaserexternallink (id, link_class, parameter, releaser_id, source) FROM stdin;\n"
        "1\tBandcampArtist\tpurplemotion\t369\t\\N\n"
        "2\tDiscogsArtist\tJonne+Valtonen\t369\t\\N\n"
        "3\tBaseUrl\thttp://www.valtone.com/\t369\t\\N\n"
        "4\tSomeFutureLinkType\tx\t369\t\\N\n"
        "5\tZxdemoAuthor\t1428\t369\t\\N\n\\.\n"
        "COPY public.demoscene_membership (id, member_id, group_id, is_current, data_source) FROM stdin;\n"
        "1\t369\t10\tt\t\\N\n2\t369\t11\tt\t\\N\n3\t369\t12\tf\t\\N\n\\.\n"
        "COPY public.demoscene_nick (id, releaser_id, name, abbreviation, differentiator) FROM stdin;\n"
        "387\t369\tPurple Motion\t\t\n74868\t369\tLord Soth\t\t\n900\t500\tSomeone Else\t\t\n\\.\n"
        "COPY public.demoscene_nickvariant (id, nick_id, name, search_title) FROM stdin;\n"
        "143381\t387\tPM\tpm\n\\.\n"
        "COPY public.parties_competition (id, party_id, name, shown_date_date, shown_date_precision, platform_id, production_type_id) FROM stdin;\n"
        "70\t80\tPC Music Multichannel\t\\N\t\\N\t\\N\t\\N\n\\.\n"
        "COPY public.parties_competitionplacing (id, competition_id, production_id, ranking, \"position\", score) FROM stdin;\n"
        "1\t70\t6720\t3\t3\t\\N\n\\.\n"
        "COPY public.parties_party (id, name, party_series_id, start_date_date) FROM stdin;\n"
        "80\tAssembly 1993\t\\N\t1993-07-30\n81\tThe Party 1992\t\\N\t1992-12-27\n\\.\n"
        "COPY public.parties_party_releases (id, party_id, production_id) FROM stdin;\n"
        "1\t81\t6721\n2\t81\t9000\n\\.\n"
        "COPY public.platforms_platform (id, name) FROM stdin;\n4\tMS-Dos\n\\.\n"
        "COPY public.productions_production (id, title, release_date_date, release_date_precision, supertype) FROM stdin;\n"
        "6720\tStarshine\t1993-07-30\td\tmusic\n"
        "6721\tSatellite One\t1992-12-27\td\tmusic\n"
        "6722\tUndated Tune\t\\N\t\\N\tmusic\n"
        "9000\tSecond Reality\t1993-08-01\td\tproduction\n"
        "7000\tStarshine\t2001-01-01\td\tmusic\n\\.\n"
        "COPY public.productions_production_author_nicks (id, production_id, nick_id) FROM stdin;\n"
        "1\t6720\t387\n2\t6721\t387\n3\t6722\t74868\n4\t9000\t387\n5\t7000\t900\n\\.\n"
        "COPY public.productions_production_platforms (id, production_id, platform_id) FROM stdin;\n"
        "1\t6720\t4\n2\t9000\t4\n\\.\n"
        "COPY public.productions_production_types (id, production_id, productiontype_id) FROM stdin;\n"
        "1\t6720\t50\n\\.\n"
        "COPY public.productions_productionlink (id, link_class, parameter, production_id, is_download_link, description) FROM stdin;\n"
        "1\tModlandFile\t/pub/modules/Screamtracker 3/Purple Motion/starshine.s3m\t6720\tt\t\n"
        "2\tModarchiveModule\t39062\t6720\tf\t\n"
        "3\tPouetProduction\t63\t9000\tf\t\n\\.\n"
        "COPY public.productions_productiontype (id, name) FROM stdin;\n50\tTracked Music\n\\.\n"
    )
    p = tmp_path / "dump.sql.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(rows)
    return p


@pytest.fixture()
def index(tmp_path, monkeypatch):
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "scene" / "demozoo.sqlite")
    (tmp_path / "scene").mkdir()
    st = demozoo.refresh_index(_dump(tmp_path))
    assert not st.get("error")

    def _no_network(*a, **k):
        raise AssertionError("the Scene tab asked Demozoo over the network")
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    return tmp_path / "scene" / "demozoo.sqlite"


def test_the_index_holds_the_scene_tabs_details(index):
    con = sqlite3.connect(index)
    assert con.execute("PRAGMA user_version").fetchone()[0] == demozoo.INDEX_SCHEMA
    # (music productions only — the demo 9000 is no discography entry)
    assert sorted(r[0] for r in con.execute("SELECT id FROM music_prod")) == [6720, 6721, 6722, 7000]
    assert demozoo.status()["productions"] == 4 and demozoo.status()["needs_rebuild"] is False


def test_a_sceners_details(index):
    d = demozoo.scener_details(369)
    assert d["groups"] == ["Future Crew", "tAAt", "Five Musicians"]       # current ones first
    assert d["aliases"] == ["Purple Motion", "Lord Soth"] and d["variants"] == ["PM"]
    # (Demozoo's order: the web address first, then by type; an unknown type
    # and a type Demozoo no longer shows — ZxdemoAuthor — left out)
    assert [(lk["label"], lk["url"]) for lk in d["links"]] == [
        ("valtone.com", "http://www.valtone.com/"),
        ("Bandcamp", "https://purplemotion.bandcamp.com/"),
        ("Discogs", "https://www.discogs.com/artist/Jonne+Valtonen")]
    assert d["notes"] == "Worked on Death Rally (1996).\n\n1st best musician in Imphobia 9."
    assert d["location"] == "Finland"
    assert d["demozoo_url"] == "https://demozoo.org/sceners/369/"
    assert demozoo.scener_details(99999) is None


def test_a_sceners_music_newest_first(index):
    prods = demozoo.scener_productions(369)
    assert [p["title"] for p in prods] == ["Starshine", "Satellite One", "Undated Tune"]
    assert prods[0] == {"id": 6720, "title": "Starshine", "year": 1993, "type": "Tracked Music",
                        "platforms": ["MS-Dos"], "url": "https://demozoo.org/productions/6720/"}


def test_a_productions_release_details(index):
    d = demozoo.production_detail(6720)
    assert d["placings"] == [{"rank": "3", "competition": "PC Music Multichannel",
                              "party": "Assembly 1993", "year": 1993,
                              "url": "https://demozoo.org/parties/80/"}]
    assert [lk["label"] for lk in d["links"]] == ["ModArchive", "Modland"]    # downloads last
    assert d["links"][1]["url"] == \
        "https://ftp.modland.com/pub/modules/Screamtracker 3/Purple Motion/starshine.s3m"
    assert demozoo.production_detail(6721)["parties"] == [
        {"name": "The Party 1992", "year": 1992, "url": "https://demozoo.org/parties/81/"}]
    assert demozoo.production_detail(9000) is None                          # not music


def test_the_scene_card_without_any_request(index, monkeypatch):
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: {
        "releaser_id": 369, "real_name": "Jonne Valtonen", "groups": ["Future Crew"],
        "url": "https://demozoo.org/sceners/369/"})
    card = asyncio.run(demozoo.scene_card("Purple Motion", track_title="Starshine - PM"))
    assert card["found"] and card["release"]["id"] == 6720                 # "PM": a nick variant
    assert card["release"]["placings"][0]["party"] == "Assembly 1993"
    assert [lk["label"] for lk in card["artist"]["links"]] == ["valtone.com", "Bandcamp", "Discogs"]
    assert card["artist"]["location"] == "Finland"
    assert [d["title"] for d in card["discography"]] == ["Satellite One", "Undated Tune"]


@pytest.mark.parametrize("title,release", [
    ("Starshine - PM", 6720), ("Starshine by Purple Motion", 6720),
    ("Satellite One", 6721),                                 # two words: the plain match
    ("Starshine", None),                                     # one word, no composer named
    ("Lord Soth", None),                                     # nothing left but a name
    ("Second Reality - PM", None),                           # a demo, no music production
    ("Starshine 2 - PM", None),                              # a sequel number is no match
    ("Lord of Starshine", None),                             # an alias word inside the title
    ("Starshine - Someone Else", None)])                     # names another composer
def test_the_release_a_title_names(index, monkeypatch, title, release):
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: {
        "releaser_id": 369, "real_name": "Jonne Valtonen", "groups": [],
        "url": "https://demozoo.org/sceners/369/"})
    card = asyncio.run(demozoo.scene_card("Purple Motion", track_title=title))
    assert (card["release"] or {}).get("id") == release


def test_an_older_index_gives_an_identity_only_card_and_asks_for_a_rebuild(index, monkeypatch):
    con = sqlite3.connect(index)
    for t in ("music_prod", "scener_detail", "prod_author"):
        con.execute(f"DROP TABLE {t}")
    con.execute("PRAGMA user_version = 0")
    con.commit()
    con.close()
    assert demozoo.needs_rebuild() and demozoo.status()["needs_rebuild"]
    assert demozoo.status()["productions"] is None
    assert demozoo.scener_details(369) is None and demozoo.scener_productions(369) is None
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: {
        "releaser_id": 369, "real_name": "Jonne Valtonen", "groups": ["Future Crew"],
        "url": "https://demozoo.org/sceners/369/"})
    card = asyncio.run(demozoo.scene_card("Purple Motion", track_title="Starshine - PM"))
    assert card["found"] and card["artist"]["groups"] == ["Future Crew"]
    assert card["discography"] == [] and card["release"] is None


def test_an_older_index_is_built_again_once(index, monkeypatch):
    con = sqlite3.connect(index)
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    started = []

    class _Thread:
        def __init__(self, target, name, daemon, kwargs=None):
            assert daemon and target is demozoo.refresh_index
            assert kwargs == {"mark_rebuild": True}
            started.append(name)

        def start(self):
            pass
    monkeypatch.setattr(demozoo.threading, "Thread", _Thread)
    monkeypatch.setattr(demozoo, "_rebuild_started", False)
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "1")
    assert demozoo.rebuild_if_outdated() is False                      # offline: not
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    assert demozoo.rebuild_if_outdated() is True
    assert demozoo.rebuild_if_outdated() is False                      # once per process
    assert started == ["demozoo-rebuild"]


def test_a_current_index_is_not_built_again(index, monkeypatch):
    monkeypatch.setattr(demozoo, "_rebuild_started", False)
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    assert demozoo.rebuild_if_outdated() is False


class _RunNow:
    """``threading.Thread`` running its target at ``start`` (in the test)."""
    runs: list = []

    def __init__(self, target, name, daemon, kwargs=None):
        self.target, self.kwargs = target, kwargs or {}

    def start(self):
        _RunNow.runs.append(self.target(**self.kwargs))


def _older_layout(index: Path) -> None:
    con = sqlite3.connect(index)
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()


class _Download:
    """``urlopen`` answering with the bytes of a file."""
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk


def test_a_build_stopped_after_the_download_is_not_tried_at_every_start(index, tmp_path, monkeypatch):
    # (a build the system stopped — out of memory — isn't tried at every start)
    _older_layout(index)
    data = _dump(tmp_path).read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Download(data))

    def _stopped(*a, **k):
        raise MemoryError()
    monkeypatch.setattr(demozoo, "_parse_dump", _stopped)
    monkeypatch.setattr(demozoo.threading, "Thread", _RunNow)
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    monkeypatch.setattr(demozoo, "_rebuild_started", False)
    assert demozoo.rebuild_if_outdated() is True
    assert (index.parent / demozoo.REBUILD_MARK).read_text() == str(demozoo.INDEX_SCHEMA)
    monkeypatch.setattr(demozoo, "_rebuild_started", False)          # the next start
    assert demozoo.rebuild_if_outdated() is False
    assert demozoo.needs_rebuild()                                    # (Settings still offers it)


def test_a_failed_download_is_tried_again_at_the_next_start(index, tmp_path, monkeypatch):
    # (no network yet at the first try, Demozoo down …)
    _older_layout(index)

    def _unreachable(*a, **k):
        raise urllib.error.URLError("temporary DNS failure")
    monkeypatch.setattr(urllib.request, "urlopen", _unreachable)
    monkeypatch.setattr(demozoo.threading, "Thread", _RunNow)
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    monkeypatch.setattr(demozoo, "_rebuild_started", False)
    assert demozoo.rebuild_if_outdated() is True
    assert "refresh failed" in (demozoo.status()["error"] or "")
    assert not (index.parent / demozoo.REBUILD_MARK).exists()
    data = _dump(tmp_path).read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Download(data))
    monkeypatch.setattr(demozoo, "_rebuild_started", False)          # the next start
    assert demozoo.rebuild_if_outdated() is True
    assert not demozoo.needs_rebuild() and not demozoo.status()["error"]
    assert demozoo.rebuild_if_outdated() is False


def test_a_damaged_index_is_replaced_by_a_refresh(index, tmp_path):
    index.write_bytes(b"not a database at all" * 100)
    assert demozoo.needs_rebuild()
    st = demozoo.refresh_index(_dump(tmp_path))
    assert not st.get("error") and not demozoo.needs_rebuild()
    assert demozoo.scener_details(369)["location"] == "Finland"
    assert sorted(p.name for p in index.parent.iterdir()) == ["demozoo.sqlite"]


def test_a_reader_keeps_the_old_index_while_a_refresh_replaces_it(index, tmp_path):
    # (the new index is a file of its own: an open reader isn't held up by
    # the build and goes on reading the old one; the next reader gets the new)
    con = sqlite3.connect(f"{index.resolve().as_uri()}?mode=ro", uri=True)
    before = con.execute("SELECT COUNT(*) FROM music_prod").fetchone()[0]
    dump = _dump(tmp_path)
    with gzip.open(dump, "rt", encoding="utf-8") as f:
        text = f.read()
    text = text.replace("7000\tStarshine\t2001-01-01\td\tmusic\n",
                        "7000\tStarshine\t2001-01-01\td\tmusic\n6723\tNew Tune\t1994-01-01\td\tmusic\n")
    text = text.replace("5\t7000\t900\n", "5\t7000\t900\n6\t6723\t387\n")
    with gzip.open(dump, "wt", encoding="utf-8") as f:
        f.write(text)
    assert not demozoo.refresh_index(dump).get("error")
    assert con.execute("SELECT COUNT(*) FROM music_prod").fetchone()[0] == before
    con.close()
    assert "New Tune" in {p["title"] for p in demozoo.scener_productions(369)}


def test_the_automatic_rebuild_waits_out_a_scan(index, monkeypatch):
    from soniqboom.core import scanner
    con = sqlite3.connect(index)
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    monkeypatch.setattr(demozoo, "_rebuild_started", False)
    monkeypatch.setattr(scanner, "full_scan_active", lambda: True)
    later = []

    async def _run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "call_later", lambda d, fn: later.append((d, fn)))
        return demozoo.rebuild_if_outdated()
    assert asyncio.run(_run()) is False
    assert later == [(demozoo._REBUILD_SCAN_WAIT_S, demozoo.rebuild_if_outdated)]
    assert not (index.parent / demozoo.REBUILD_MARK).exists()


def test_the_artist_card_bio_is_the_profiles_notes(index, monkeypatch):
    from soniqboom.core import artistinfo
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: {
        "releaser_id": 369, "real_name": "Jonne Valtonen", "groups": [],
        "url": "https://demozoo.org/sceners/369/"})

    async def _no_summary(title):
        raise AssertionError("Wikipedia asked although the profile links no article")
    monkeypatch.setattr(artistinfo, "_wiki_summary", _no_summary)
    card = asyncio.run(artistinfo.get_artist_info("Purple Motion", is_retro=True))
    assert card["local_bio"] and card["bio"].startswith("Worked on Death Rally")
    assert card["location"] == "Finland" and card["source"] == "Demozoo"
    assert card["image"] is None


def _with_article(monkeypatch, summary: dict):
    """The profile links a Wikipedia article, whose summary is ``summary``."""
    from soniqboom.core import artistinfo
    real = demozoo.fetch_scener_details

    async def _details(rid):
        d = await real(rid)
        if d:
            d["links"] = d["links"] + [{"url": "https://en.wikipedia.org/wiki/Jonne_Valtonen",
                                        "label": "Wikipedia", "class": "WikipediaPage"}]
        return d
    monkeypatch.setattr(demozoo, "fetch_scener_details", _details)
    monkeypatch.setattr(artistinfo, "_read_cache", lambda key: None)
    monkeypatch.setattr(artistinfo, "_write_cache", lambda key, rec: None)
    asked = []

    async def _summary(title):
        asked.append(title)
        return summary
    monkeypatch.setattr(artistinfo, "_wiki_summary", _summary)
    return asked


_ARTICLE = {"title": "Jonne Valtonen", "extract": "Jonne Valtonen is a Finnish composer.",
            "thumbnail": {"source": "https://upload.wikimedia.org/jv.jpg"},
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Jonne_Valtonen"}}}


def test_the_notes_stay_the_bio_and_a_linked_article_gives_its_photo(index, monkeypatch):
    from soniqboom.core import artistinfo
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: {
        "releaser_id": 369, "real_name": "Jonne Valtonen", "groups": [],
        "url": "https://demozoo.org/sceners/369/"})
    asked = _with_article(monkeypatch, _ARTICLE)
    card = asyncio.run(artistinfo.get_artist_info("Purple Motion", is_retro=True))
    assert asked == ["Jonne_Valtonen"]
    assert card["bio"].startswith("Worked on Death Rally") and card["local_bio"]
    assert card["image"] == "https://upload.wikimedia.org/jv.jpg"
    assert card["image_page"] == "https://en.wikipedia.org/wiki/Jonne_Valtonen"   # (its credit)
    assert card["source"] == "Demozoo" and card["url"] == "https://demozoo.org/sceners/369/"
    assert card["title"] == "Purple Motion" and "Finnish composer" not in card["bio"]


def test_an_article_without_a_photo_leaves_the_notes_card_as_it_is(index, monkeypatch):
    from soniqboom.core import artistinfo
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: {
        "releaser_id": 369, "real_name": "Jonne Valtonen", "groups": [],
        "url": "https://demozoo.org/sceners/369/"})
    _with_article(monkeypatch, {k: v for k, v in _ARTICLE.items() if k != "thumbnail"})
    card = asyncio.run(artistinfo.get_artist_info("Purple Motion", is_retro=True))
    assert card["image"] is None and card["bio"].startswith("Worked on Death Rally")


def test_a_profile_without_notes_gets_the_linked_article(index, monkeypatch):
    from soniqboom.core import artistinfo
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: {
        "releaser_id": 500, "real_name": "Al Other", "groups": [],
        "url": "https://demozoo.org/sceners/500/"})
    _with_article(monkeypatch, _ARTICLE)
    card = asyncio.run(artistinfo.get_artist_info("Someone Else", is_retro=True))
    assert not card["local_bio"] and card["source"] == "Wikipedia"
    assert card["bio"] == "Jonne Valtonen is a Finnish composer."
    assert card["image"] == "https://upload.wikimedia.org/jv.jpg"


def test_a_profile_without_notes_still_asks_wikipedia(index, monkeypatch):
    from soniqboom.core import artistinfo
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: {
        "releaser_id": 500, "real_name": "Al Other", "groups": [],
        "url": "https://demozoo.org/sceners/500/"})
    asked = []

    async def _wiki(card, *, photo_only=False):
        asked.append((card["name"], photo_only))
        return card
    monkeypatch.setattr(artistinfo, "_with_wikipedia", _wiki)
    card = asyncio.run(artistinfo.get_artist_info("Someone Else", is_retro=True))
    assert asked == [("Someone Else", False)] and not card["local_bio"]


def test_every_production_of_a_prolific_composer_can_match(tmp_path, monkeypatch):
    # (a release beyond the newest 200 productions still matches)
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "demozoo.sqlite")
    con = sqlite3.connect(tmp_path / "demozoo.sqlite")
    demozoo._write_details(con, {
        "sceners": [(7, "Busy", "[]", "[]", "[]", "", "")],
        "prods": [(i, f"Tune Number {i}" if i else "Oldest Distinct Title",
                   f"{2000 + i // 100}-01-01", 2000 + i // 100, "", "[]") for i in range(250)],
        "authors": [(7, i) for i in range(250)]})
    con.commit()
    con.close()
    assert len(demozoo.scener_productions(7)) == 250
    monkeypatch.setattr(demozoo, "lookup_scener", lambda name, tt=None: {
        "releaser_id": 7, "real_name": "", "groups": [], "url": "https://demozoo.org/sceners/7/"})
    card = asyncio.run(demozoo.scene_card("Busy", track_title="Oldest Distinct Title"))
    assert card["release"]["id"] == 0 and len(card["discography"]) == 15


@pytest.mark.parametrize("raw,want", [
    ("P/\\\\te", "P/\\te"), ("a\\tb", "a\tb"), ("x\\\\\\\\y", "x\\\\y"), ("\\N", None),
    ("oct\\101", "octA"), ("plain", "plain")])
def test_copy_escapes_are_read_in_one_pass(raw, want):
    assert demozoo._unesc(raw) == want


def test_a_missing_detail_table_is_reported(tmp_path, caplog):
    dump = tmp_path / "d.sql.gz"
    with gzip.open(dump, "wt", encoding="utf-8") as f:
        f.write("COPY public.demoscene_releaser (id, name, is_group) FROM stdin;\n1\tX\tf\n\\.\n")
    with caplog.at_level("WARNING"):
        demozoo._parse_dump(dump, {})
    assert "productions_productionlink" in caplog.text


def test_a_missing_detail_column_is_reported(tmp_path, caplog):
    # (the profile's notes / location, a link's type, read by name)
    dump = tmp_path / "d.sql.gz"
    with gzip.open(dump, "wt", encoding="utf-8") as f:
        f.write("COPY public.demoscene_releaser (id, name, is_group, profile_notes) FROM stdin;\n"
                "1\tX\tf\t\\N\n\\.\n"
                "COPY public.demoscene_releaserexternallink (id, kind, parameter, releaser_id) "
                "FROM stdin;\n\\.\n")
    with caplog.at_level("WARNING"):
        demozoo._parse_dump(dump, {})
    for col in ("demoscene_releaser.notes", "demoscene_releaser.location",
                "demoscene_releaserexternallink.link_class"):
        assert col in caplog.text
    assert "demoscene_releaserexternallink.parameter" not in caplog.text


def test_every_type_of_a_production_is_kept(tmp_path):
    rows = _dump(tmp_path)
    with gzip.open(rows, "rt", encoding="utf-8") as f:
        text = f.read()
    text = text.replace("1\t6720\t50\n\\.\n", "1\t6720\t50\n2\t6720\t51\n\\.\n").replace(
        "50\tTracked Music\n\\.\n", "50\tTracked Music\n51\tExecutable Music\n\\.\n")
    with gzip.open(rows, "wt", encoding="utf-8") as f:
        f.write(text)
    details: dict = {}
    demozoo._parse_dump(rows, details)
    assert {p[0]: p[4] for p in details["prods"]}[6720] == "Executable Music, Tracked Music"


@pytest.mark.parametrize("md,want", [
    ("See [Wikipedia](https://en.wikipedia.org/wiki/Foo_(musician)) for more",
     "See Wikipedia for more"),
    ("![logo](https://example.org/l.png) Hello", "Hello"),
    ("a <b>bold</b><br>next", "a bold\nnext"),
    ("a \\*star\\* and a \\\\ b", "a *star* and a \\ b"),
    ("rated 5* and 4*", "rated 5* and 4*"),
    ("> quoted\n> more", "quoted\nmore"),
    ("Ref [site][1] here\n\n[1]: https://example.org", "Ref site here"),
    ("**strong**, *em*, _u_ and snake_case_name `code`",
     "strong, em, u and snake_case_name code"),
    ("* one\n* two\n\n# Head\npara", "one\ntwo\n\nHead\npara"),
    ("<https://example.org> x", "https://example.org x"),
    ("a\n\n\n\nb", "a\n\nb"), ("", ""), (None, ""),
    ("left Zombie Boys for Gate.[^1] Then on.\n\n[^1]: [Box II](https://example.org/p/1/) by Gate.",
     "left Zombie Boys for Gate.[1] Then on.\n\n[1] Box II by Gate."),
    ("† 2022 [[pouet.net thread]](https://www.pouet.net/topic.php?which=1)",
     "† 2022 [pouet.net thread]"),
    ("_Please be aware, piracy in\nany form is illegal_", "Please be aware, piracy in\nany form is illegal"),
    ("*one\n\ntwo*", "*one\n\ntwo*")])
def test_the_profile_notes_are_plain_text(md, want):
    assert demozoo._plain_notes(md) == want


def test_long_or_hostile_notes_are_converted_quickly():
    import time
    for md in ("[" * 50_000, "*a " * 20_000, "_a " * 20_000, "**a " * 20_000, "![" * 30_000):
        t0 = time.perf_counter()
        demozoo._plain_notes(md)
        assert time.perf_counter() - t0 < 1.5, md[:6]


def test_the_index_holds_the_notes_as_plain_text(index):
    con = sqlite3.connect(index)
    notes = con.execute("SELECT notes FROM scener_detail WHERE releaser_id = 369").fetchone()[0]
    assert notes == "Worked on Death Rally (1996).\n\n1st best musician in Imphobia 9."


def test_the_old_live_cache_goes_with_a_new_index(tmp_path, monkeypatch):
    monkeypatch.setattr(demozoo, "_db_path", lambda: tmp_path / "scene" / "demozoo.sqlite")
    (tmp_path / "scene").mkdir()
    for n in ("scener_369.json", "scener_369_prods.json", "prod_6720.json"):
        (tmp_path / "scene" / n).write_text("{}")
    assert not demozoo.refresh_index(_dump(tmp_path)).get("error")
    assert sorted(p.name for p in (tmp_path / "scene").iterdir()) == ["demozoo.sqlite"]


def test_one_build_at_a_time(index, monkeypatch):
    def _second(_path):
        raise AssertionError("a second build ran")
    monkeypatch.setattr(demozoo, "_refresh_index", _second)
    assert demozoo._refresh_lock.acquire(blocking=False)
    try:
        assert "names" in demozoo.refresh_index(None)
    finally:
        demozoo._refresh_lock.release()
