# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Online covers for library tracks without one (``core/online_cover.py``):
which tracks are looked up and by what, the exact-match rules (one album, the
year for several of one name, nothing otherwise), the sources and their order,
what is remembered (a cover, "none" for a month, never a failed connection),
the queue, and the stored pictures.  The lookup services are faked at
``_http_get``; ``tests/data/online_cover_musicbrainz.json`` holds answers
MusicBrainz really gave (trimmed to the fields that are read)."""
from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from soniqboom.api import library
from soniqboom.config import settings
from soniqboom.core import artistinfo, catalogue, data, online_cover as OC, retro

REAL = json.loads((Path(__file__).parent / "data" / "online_cover_musicbrainz.json").read_text("utf-8"))

RED, BLUE, GREEN = (200, 30, 30), (30, 30, 200), (30, 160, 60)
CAA = OC._CAA


def jpeg(color=RED, size=(600, 600), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


def colour(data_: bytes) -> tuple:
    """The nearest of RED / BLUE / GREEN to the picture's centre."""
    with Image.open(io.BytesIO(data_)) as im:
        px = im.convert("RGB").getpixel((im.width // 2, im.height // 2))
    return min((RED, BLUE, GREEN), key=lambda c: sum((a - b) ** 2 for a, b in zip(c, px)))


def side(data_: bytes) -> int:
    with Image.open(io.BytesIO(data_)) as im:
        return max(im.size)


def tr(tid="t1", **kw):
    d = dict(id=tid, path=f"/m/{tid}.mp3", format="MP3", artist="Queen", album="Jazz",
             title="Bicycle Race", album_artist="", year=None, cover_art=None,
             sid_md5=None, album_source=None)
    d.update(kw)
    return SimpleNamespace(**d)


def rel(rid, title, artist="Queen", *, rg, date="1978", status="Official", primary="Album",
        secondary=None, artist_id="a-queen"):
    return {"id": rid, "title": title, "status": status, "date": date,
            "artist-credit": [{"name": artist, "artist": {"id": artist_id, "name": artist}}],
            "release-group": {"id": rg, "primary-type": primary,
                              **({"secondary-types": secondary} if secondary else {})}}


def front(kind: str, ident: str) -> str:
    return f"{CAA}/{kind}/{ident}/front-1200"


class World:
    def __init__(self):
        self.calls = []         # (url, mb, headers)
        self.mb = []            # (text in "<kind>:<query>", answer dict | status)
        self.pics = {}          # url → bytes | (status, body, content type)
        self.discogs = None     # answer dict | status
        self.down = False
        self.offline = False
        self.setting = True
        self.sent = []
        self.hook = None

    async def get(self, url, *, headers=None, mb=False):
        self.calls.append((url, mb, dict(headers or {})))
        if self.hook:
            self.hook(url)
        if self.offline or self.down:
            raise OC.Unreachable("no connection")
        if url.startswith(OC._MB + "/"):
            u = urlsplit(url)
            what = f"{u.path.rsplit('/', 1)[-1]}:{parse_qs(u.query)['query'][0]}"
            for text, answer in self.mb:
                if text in what:
                    return self._answer(answer(url) if callable(answer) else answer)
            return self._answer({"releases": [], "recordings": []})
        if url.startswith(OC._DISCOGS):
            return self._answer(self.discogs if self.discogs is not None else {"results": []})
        pic = self.pics.get(url)
        if pic is None:
            return 404, b"", "text/html"
        if isinstance(pic, tuple):
            return pic
        return 200, pic, "image/jpeg"

    @staticmethod
    def _answer(answer):
        if isinstance(answer, int):
            return answer, b"busy", "text/plain"
        return 200, json.dumps(answer).encode(), "application/json"

    # what was asked
    def queries(self):
        return [parse_qs(urlsplit(u).query)["query"][0] for u, _mb, _h in self.calls
                if u.startswith(OC._MB + "/")]

    def asked(self, prefix):
        return [u for u, _mb, _h in self.calls if u.startswith(prefix)]


@pytest.fixture
def w(monkeypatch, tmp_path):
    OC._reset_for_tests()
    world = World()
    monkeypatch.setattr(settings, "art_cache_dir", str(tmp_path / "art"))
    monkeypatch.setattr(settings, "discogs_token", "")
    monkeypatch.setattr(OC, "_http_get", world.get)
    monkeypatch.setattr(OC, "_offline", lambda: world.offline)
    monkeypatch.setattr(OC, "_BUSY_WAITS", ())                    # (the waits have their own tests)

    async def get_config(key, default=None):
        assert key == OC.SETTING
        return world.setting
    monkeypatch.setattr(data, "get_config", get_config)

    async def broadcast(msg):
        world.sent.append(msg)
    monkeypatch.setattr(library, "_broadcast", broadcast)
    world.root = tmp_path / "art" / "online"
    yield world
    OC._reset_for_tests()


async def settle():
    while True:
        worker = OC._worker
        if worker is None or worker.done():
            return
        await worker


async def lookup(track):
    """Ask for ``track``'s cover, let the lookups run, and return its state."""
    OC.request(await OC.state(track), track.id)
    await settle()
    return await OC.state(track)


def files(world) -> list[str]:
    return sorted(p.name.split(".", 1)[1] for p in world.root.glob("*/*")) if world.root.exists() else []


# ── Who is looked up ─────────────────────────────────────────────────────────

def test_a_tagged_audio_file_is_looked_up_by_its_album():
    # (the checks that exclude chip formats and collection tracks must run,
    # not fail quietly: a failure there reads as "never looked up")
    assert retro.is_retro_format("MP3") is False
    assert catalogue.is_catalogue_path("/m/t1.mp3") is False
    sub = OC.subject(tr(year=1978))
    assert sub == OC.Subject("album", "Queen", "Jazz", 1978, False, "a|queen|jazz")
    assert OC.subject({"path": "/m/a.flac", "format": "FLAC", "artist": "Queen", "album": "Jazz"}).key == "a|queen|jazz"


@pytest.mark.parametrize("change", [
    {"path": ""},
    {"sid_md5": "0123"},
    {"path": "http://radio.invalid/stream"},
    {"path": "https://radio.invalid/a.mp3"},
    {"format": "ProTracker"},
    {"format": "SID"},
    {"artist": "Unknown Artist"},
    {"artist": ""},
    {"artist": "Various Artists"},
    {"artist": "Queen", "album": "", "title": ""},
    {"artist": "???", "album": "", "title": "!!!"},
])
def test_tracks_that_are_never_looked_up(change):
    assert retro.is_retro_format("ProTracker") and retro.is_retro_format("SID")
    assert OC.subject(tr(**change)) is None


def test_a_track_of_an_online_collection_is_never_looked_up(monkeypatch):
    monkeypatch.setattr(catalogue, "is_catalogue_path", lambda p: str(p).startswith("modland:"))
    assert OC.subject(tr(path="modland:/pub/a.mp3")) is None
    assert OC.subject(tr(path="/m/a.mp3")) is not None


def test_a_track_on_a_network_share_is_looked_up():
    assert OC.subject(tr(path="smb://nas.invalid/Music:/Queen/Jazz/01.mp3")).key == "a|queen|jazz"


def test_the_album_artist_decides_and_a_placeholder_one_does_not():
    assert OC.subject(tr(artist="Freddie Mercury", album_artist="Queen")).key == "a|queen|jazz"
    assert OC.subject(tr(album_artist="Unknown Artist")).key == "a|queen|jazz"


def test_names_are_compared_without_case_accents_punctuation_and_a_leading_the():
    a = OC.subject(tr(artist="The Beatles", album="Sgt. Pepper’s Lonely Hearts Club Band"))
    b = OC.subject(tr(artist="beatles", album="SGT PEPPERS LONELY HEARTS CLUB BAND"))
    assert a.key == b.key == "a|beatles|sgtpepperslonelyheartsclubband"
    assert OC.subject(tr(artist="Beyoncé", album="Simon & Garfunkel")).key == "a|beyonce|simonandgarfunkel"
    assert OC.subject(tr(artist="Queen feat. David Bowie")).artist == "Queen"
    assert OC.subject(tr(artist="Queen (ft. Bowie)")).key == "a|queen|jazz"


def test_a_compilation_is_keyed_by_album_and_year():
    s = OC.subject(tr(artist="Iggy Pop", album_artist="Various Artists", album="Trainspotting", year=1996))
    assert (s.kind, s.various, s.key) == ("album", True, "a|~va|trainspotting|y1996")
    assert OC.subject(tr(album_artist="VA", album="Trainspotting")).key == "a|~va|trainspotting"


@pytest.mark.parametrize("change", [
    {"album": ""}, {"album": "Unknown Album"}, {"album": "<unknown>"},
    {"album_source": "folder"},       # (an album name that is not from the file's tags)
])
def test_without_a_tagged_album_the_song_is_looked_up(change):
    s = OC.subject(tr(**change))
    assert (s.kind, s.name, s.key) == ("song", "Bicycle Race", "t|queen|bicyclerace")


def test_a_year_out_of_range_does_not_count():
    assert OC.subject(tr(year=0)).year is None
    assert OC.subject(tr(year="1978")).year is None
    assert OC.subject(tr(year=True)).year is None
    assert OC.subject(tr(year=1978)).year == 1978


# ── One album: the cover ─────────────────────────────────────────────────────

async def test_one_album_of_that_name_gives_its_cover_in_three_sizes(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg(RED, (1600, 1600))
    st = await lookup(tr())
    assert st.hit and st.record["source"] == "musicbrainz" and st.record["ref"] == "release-group/g1"
    assert w.queries() == ['release:"Jazz" AND artist:"Queen"']
    assert [mb for _u, mb, _h in w.calls] == [True, False]        # (only MusicBrainz is paced)
    assert files(w) == ["full.jpg", "json", "lg.jpg", "sm.jpg"]
    sizes = {n: await OC.image(st, n) for n in ("full", "lg", "sm")}
    assert {n: side(b) for n, b in sizes.items()} == {"full": 1200, "lg": 550, "sm": 200}
    assert all(b[:2] == b"\xff\xd8" and colour(b) == RED for b in sizes.values())
    assert await OC.image(st, "other") == sizes["full"]
    assert w.sent == [{"event": "art_ready", "track_id": "t1"}]


async def test_a_small_picture_is_not_enlarged_and_a_png_becomes_a_jpeg(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = (200, jpeg(GREEN, (300, 240), "PNG"), "image/png")
    st = await lookup(tr())
    full = await OC.image(st, "full")
    assert full[:2] == b"\xff\xd8" and side(full) == 300 and side(await OC.image(st, "sm")) == 200


async def test_the_second_ask_is_answered_from_memory_and_after_a_restart_from_disk(w):
    w.mb.append(('release:"Jazz"', {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.mb.append(('release:"Innuendo"', {"releases": [rel("r2", "Innuendo", rg="g2", date="1991")]}))
    w.pics[front("release-group", "g1")] = jpeg(RED)
    w.pics[front("release-group", "g2")] = jpeg(BLUE)
    a, b = tr("t1"), tr("t2", album="Innuendo")
    assert (await lookup(a)).hit                              # cold: asked
    n = len(w.calls)
    st = await OC.state(a)                                    # warm: memory
    assert st.hit and OC.request(st, "t1") is False and len(w.calls) == n
    assert (await lookup(b)).hit                              # cold again, another album
    assert len(w.calls) == n + 2
    OC._reset_for_tests()                                     # "restart": memory gone
    st_a, st_b = await OC.state(a), await OC.state(b)
    assert st_a.hit and st_b.hit and len(w.calls) == n + 2    # from disk, nothing asked
    assert colour(await OC.image(st_a, "lg")) == RED and colour(await OC.image(st_b, "lg")) == BLUE
    assert st_a.stamp == st_a.record["at"] > 0


async def test_every_track_of_the_album_shares_one_lookup_and_is_told(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    tracks = [tr(f"t{i}", title=f"Song {i}") for i in range(4)]
    for t in tracks:
        assert OC.request(await OC.state(t), t.id) is True
    assert OC.request(await OC.state(tracks[0]), "t0") is True        # (asked twice: one waiter)
    await settle()
    assert len(w.queries()) == 1
    assert [m["track_id"] for m in w.sent] == ["t0", "t1", "t2", "t3"]
    assert all(m["event"] == "art_ready" for m in w.sent)


async def test_the_group_front_missing_tries_its_earliest_releases(w):
    w.mb.append(("release:", {"releases": [
        rel("r-1990", "Jazz", rg="g1", date="1990"), rel("r-1978", "Jazz", rg="g1", date="1978-11-10"),
        rel("r-none", "Jazz", rg="g1", date=None), rel("r-1985", "Jazz", rg="g1", date="1985"),
        rel("r-2011", "Jazz", rg="g1", date="2011")]}))
    w.pics[front("release", "r-1985")] = jpeg(BLUE)
    st = await lookup(tr())
    assert st.hit and colour(await OC.image(st, "sm")) == BLUE
    assert w.asked(CAA) == [front("release-group", "g1"), front("release", "r-1978"), front("release", "r-1985")]


async def test_only_three_releases_are_tried_for_a_picture(w):
    w.mb.append(("release:", {"releases": [rel(f"r{i}", "Jazz", rg="g1", date=str(1978 + i)) for i in range(6)]}))
    st = await lookup(tr())
    assert st.known and not st.hit
    assert w.asked(CAA) == [front("release-group", "g1")] + [front("release", f"r{i}") for i in range(3)]


# ── The exact-match rules ────────────────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    [rel("r1", "Jazz II", rg="g1")],                                   # another title
    [rel("r1", "Jazz (Live)", rg="g1")],
    [rel("r1", "Jazz", "Queen Latifah", rg="g1")],                     # another artist
    [rel("r1", "Jazz", "The Queen Tribute Band", rg="g1")],
    [{"id": "r1", "title": "Jazz", "release-group": {"id": "g1"}}],    # no credit
    [{"id": "r1", "title": "Jazz", "artist-credit": [{"name": "Queen"}]}],   # no album id
    [],
])
async def test_anything_but_the_same_artist_and_album_shows_nothing(w, answer):
    w.mb.append(("release:", {"releases": answer}))
    w.pics[front("release-group", "g1")] = jpeg()
    st = await lookup(tr())
    assert st.known and not st.hit and w.asked(CAA) == [] and w.sent == []
    assert files(w) == ["json"]


async def test_a_joined_credit_and_an_alias_spelling_match(w):
    credit = [{"name": "Simon", "joinphrase": " & ", "artist": {"id": "a1", "name": "Paul Simon"}},
              {"name": "Garfunkel", "artist": {"id": "a2", "name": "Art Garfunkel"}}]
    w.mb.append(("release:", {"releases": [{"id": "r1", "title": "Bookends", "status": "Official",
                                            "artist-credit": credit, "release-group": {"id": "g1", "primary-type": "Album"}}]}))
    w.pics[front("release-group", "g1")] = jpeg()
    assert (await lookup(tr(artist="Simon and Garfunkel", album="Bookends"))).hit


def test_official_releases_and_the_best_kind_decide_the_albums():
    sub = OC.subject(tr())
    groups = OC._release_groups({"releases": [
        rel("b1", "Jazz", rg="g-boot", status="Bootleg"),
        rel("s1", "Jazz", rg="g-single", primary="Single"),
        rel("c1", "Jazz", rg="g-comp", secondary=["Compilation"]),
        rel("a1", "Jazz", rg="g-album", date="1978"), rel("a2", "JAZZ", rg="g-album", date="1994-05-01"),
    ]}, sub, "Jazz")
    assert [(g.id, sorted(g.years), g.releases) for g in groups] == [
        ("g-album", [1978, 1994], [("1978", "a1"), ("1994-05-01", "a2")])]
    only_boot = OC._release_groups({"releases": [rel("b1", "Jazz", rg="g-boot", status="Bootleg")]}, sub, "Jazz")
    assert [g.id for g in only_boot] == ["g-boot"]                # (nothing official: what there is)
    kinds = OC._release_groups({"releases": [
        rel("s1", "Jazz", rg="g-single", primary="Single"), rel("e1", "Jazz", rg="g-ep", primary="EP"),
        rel("c1", "Jazz", rg="g-comp", secondary=["Compilation"])]}, sub, "Jazz")
    assert [g.id for g in kinds] == ["g-comp"]                    # an album of any kind before an EP


def test_choosing_among_albums():
    g = lambda i, *years: OC._Group(i, years=set(years))          # noqa: E731
    one, two = g("a", 1978), g("b", 1991)
    assert OC._choose([], 1978) == (None, False)
    assert OC._choose([one], None) == (one, False)
    assert OC._choose([one], 2020) == (one, False)                # one album: the year is not needed
    assert OC._choose([one], 2020, need_year=True) == (None, False)
    assert OC._choose([one], None, need_year=True) == (one, False)
    assert OC._choose([one], 1978, need_year=True) == (one, False)
    assert OC._choose([one, two], None) == (None, True)
    assert OC._choose([one, two], 1991) == (two, True)
    assert OC._choose([one, two], 2000) == (None, True)
    assert OC._choose([g("a", 1978, 1991), two], 1991) == (None, True)     # the year fits both


async def test_real_answer_two_albums_of_one_name_are_told_apart_by_the_year(w, monkeypatch):
    """Queen's "Greatest Hits" is two different albums at MusicBrainz (the
    1981 one, and a 2004 one)."""
    monkeypatch.setattr(settings, "discogs_token", "tok")
    w.discogs = {"results": [{"id": 1, "title": "Queen - Greatest Hits", "year": "1981",
                              "cover_image": "https://i.discogs.com/x.jpg"}]}
    w.pics["https://i.discogs.com/x.jpg"] = jpeg(GREEN)
    answer = REAL["queen_greatest_hits"]
    ids = sorted({r["release-group"]["id"] for r in answer["releases"]})
    assert len(ids) == 2
    old = next(i for i in ids if i.startswith("69ce61c8"))
    new = next(i for i in ids if i.startswith("015fa681"))
    w.mb.append(('release:"Greatest Hits" AND artist:"Queen"', answer))
    w.pics[front("release-group", old)] = jpeg(RED)
    w.pics[front("release-group", new)] = jpeg(BLUE)

    def t(tid, year):
        return tr(tid, album="Greatest Hits", year=year)

    st = await lookup(t("none", None))
    assert st.known and not st.hit and st.record.get("amb") is True
    assert w.asked(CAA) == [] and w.asked(OC._DISCOGS) == []      # no other source decides it
    assert w.sent == []
    st81 = await lookup(t("y81", 1981))
    assert st81.hit and st81.key == "a|queen|greatesthits|y1981" and colour(await OC.image(st81, "sm")) == RED
    st04 = await lookup(t("y04", 2004))
    assert st04.hit and colour(await OC.image(st04, "sm")) == BLUE
    st75 = await lookup(t("y75", 1975))
    assert st75.known and not st75.hit
    assert [m["track_id"] for m in w.sent] == ["y81", "y04"]
    assert (await OC.state(t("none", None))).hit is False         # still nothing without a year
    assert (await OC.state(t("again", 1981))).hit                 # another 1981 track: no new lookup
    assert len(w.queries()) == 4


async def test_tracks_of_different_years_waiting_on_one_ambiguous_name(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Hits", rg="g-a", date="1981"),
                                           rel("r2", "Hits", rg="g-b", date="2004")]}))
    w.pics[front("release-group", "g-a")] = jpeg(RED)
    w.pics[front("release-group", "g-b")] = jpeg(BLUE)
    a, a2, b, c = (tr("a", album="Hits", year=1981), tr("a2", album="Hits", year=1981),
                   tr("b", album="Hits", year=2004), tr("c", album="Hits"))
    for t in (a, a2, b, c):
        OC.request(await OC.state(t), t.id)
    await settle()
    assert [m["track_id"] for m in w.sent] == ["a", "a2", "b"]    # c has no year: not told, no cover
    assert len(w.queries()) == 2                                   # 1981 (with the note), then 2004
    assert colour(await OC.image(await OC.state(a2), "sm")) == RED
    assert colour(await OC.image(await OC.state(b), "sm")) == BLUE
    assert not (await OC.state(c)).hit


async def test_real_answer_a_compilation_needs_the_tracks_year(w):
    w.mb.append((f'release:"Trainspotting" AND arid:{OC._VA_ARID}', REAL["va_trainspotting"]))
    gid = next(r["release-group"]["id"] for r in REAL["va_trainspotting"]["releases"]
               if r["title"] == "Trainspotting")
    w.pics[front("release-group", gid)] = jpeg(GREEN)

    def t(tid, year):
        return tr(tid, artist="Iggy Pop", album_artist="Various Artists", album="Trainspotting", year=year)

    st = await lookup(t("y96", 1996))
    assert st.hit and st.key == "a|~va|trainspotting|y1996" and colour(await OC.image(st, "sm")) == GREEN
    assert (await lookup(t("y99", 1999))).hit is False            # no release of it that year
    assert (await lookup(t("none", None))).hit                    # no year tagged: the one album
    assert w.queries()[0] == f'release:"Trainspotting" AND arid:{OC._VA_ARID}'


async def test_a_compilation_must_be_credited_to_various_artists(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Trainspotting", "Various Artists", rg="g1",
                                               artist_id="someone-else")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    st = await lookup(tr(album_artist="Various Artists", album="Trainspotting"))
    assert st.known and not st.hit


# ── Disc / edition qualifiers ────────────────────────────────────────────────

@pytest.mark.parametrize("album, plain", [
    ("The Wall (Disc 2)", "The Wall"), ("The Wall [Deluxe Edition]", "The Wall"),
    ("The Wall CD1", "The Wall"), ("The Wall - Disc 2", "The Wall"),
    ("The Wall (2011 Remastered)", "The Wall"), ("The Wall (Deluxe Edition) (Disc 1)", "The Wall"),
    ("The Wall", ""), ("The Wall (Live Edition)", ""), ("Live (Remix Edition)", ""),
    ("(Disc 2)", ""), ("Disc 2", ""),
])
def test_the_plain_title(album, plain):
    assert OC._plain_title(album) == plain


async def test_a_qualified_title_is_asked_as_tagged_first_then_plain(w):
    w.mb.append(('release:"The Wall"', {"releases": [rel("r1", "The Wall", "Pink Floyd", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    st = await lookup(tr(artist="Pink Floyd", album="The Wall (Disc 2)"))
    assert st.hit and st.key == "a|pinkfloyd|thewalldisc2"
    assert w.queries() == ['release:"The Wall (Disc 2)" AND artist:"Pink Floyd"',
                           'release:"The Wall" AND artist:"Pink Floyd"']


async def test_a_title_naming_another_recording_is_not_shortened(w):
    w.mb.append(('release:"The Wall"', {"releases": [rel("r1", "The Wall", "Pink Floyd", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    st = await lookup(tr(artist="Pink Floyd", album="The Wall (Live Edition)"))
    assert not st.hit and w.queries() == ['release:"The Wall (Live Edition)" AND artist:"Pink Floyd"']


async def test_quotes_in_a_name_cannot_break_the_search_phrase(w):
    await lookup(tr(artist='AC"DC\\', album='Back "in" Black'))
    assert w.queries() == ['release:"Back  in  Black" AND artist:"AC DC"']


# ── Discogs ──────────────────────────────────────────────────────────────────

DISCOGS = {"results": [
    {"id": 5, "master_id": 9, "title": "Queen (2) - Jazz", "year": "1978", "cover_image": "https://i.discogs.com/a.jpg"},
    {"id": 6, "master_id": 9, "title": "Queen* - Jazz", "year": "1994", "cover_image": "https://i.discogs.com/b.jpg"},
    {"id": 7, "title": "Queen - Jazz II", "cover_image": "https://i.discogs.com/other.jpg"},
    {"id": 8, "title": "Queens - Jazz", "cover_image": "https://i.discogs.com/other2.jpg"},
]}


async def test_discogs_is_asked_after_musicbrainz_with_the_token_in_the_header(w, monkeypatch):
    monkeypatch.setattr(settings, "discogs_token", "SeCrEtToKeN")
    w.discogs = DISCOGS
    w.pics["https://i.discogs.com/a.jpg"] = jpeg(BLUE)
    st = await lookup(tr())
    assert st.hit and st.record["source"] == "discogs" and st.record["ref"] == "master/9"
    assert colour(await OC.image(st, "full")) == BLUE
    (url, mb, headers), = [c for c in w.calls if c[0].startswith(OC._DISCOGS)]
    assert "SeCrEtToKeN" not in url and mb is False
    assert headers["Authorization"] == "Discogs token=SeCrEtToKeN"
    assert parse_qs(urlsplit(url).query) == {"type": ["release"], "artist": ["Queen"],
                                             "release_title": ["Jazz"], "per_page": ["25"]}
    assert [c[0] for c in w.calls].index(url) > 0                 # MusicBrainz came first


async def test_musicbrainz_album_without_a_picture_falls_to_discogs(w, monkeypatch):
    monkeypatch.setattr(settings, "discogs_token", "tok")
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.discogs = DISCOGS
    w.pics["https://i.discogs.com/b.jpg"] = jpeg(GREEN)           # (the first picture is missing)
    st = await lookup(tr())
    assert st.hit and st.record["source"] == "discogs" and colour(await OC.image(st, "sm")) == GREEN
    assert w.asked("https://i.discogs.com/") == ["https://i.discogs.com/a.jpg", "https://i.discogs.com/b.jpg"]


async def test_without_a_token_discogs_is_not_asked(w):
    w.discogs = DISCOGS
    w.pics["https://i.discogs.com/a.jpg"] = jpeg()
    st = await lookup(tr())
    assert st.known and not st.hit and w.asked(OC._DISCOGS) == []


async def test_a_compilation_is_never_asked_at_discogs(w, monkeypatch):
    monkeypatch.setattr(settings, "discogs_token", "tok")
    w.discogs = {"results": [{"id": 1, "title": "Various - Trainspotting", "cover_image": "https://i.discogs.com/a.jpg"}]}
    st = await lookup(tr(album_artist="Various", album="Trainspotting"))
    assert st.known and not st.hit and w.asked(OC._DISCOGS) == []


async def test_two_discogs_albums_of_one_name_need_the_year(w, monkeypatch):
    monkeypatch.setattr(settings, "discogs_token", "tok")
    w.discogs = {"results": [
        {"id": 1, "master_id": 1, "title": "Queen - Hits", "year": "1981", "cover_image": "https://i.discogs.com/a.jpg"},
        {"id": 2, "title": "Queen - Hits", "year": "2004", "cover_image": "https://i.discogs.com/b.jpg"}]}
    w.pics["https://i.discogs.com/a.jpg"] = jpeg(RED)
    w.pics["https://i.discogs.com/b.jpg"] = jpeg(BLUE)
    assert not (await lookup(tr("n", album="Hits"))).hit
    st = await lookup(tr("y", album="Hits", year=2004))
    assert st.hit and st.record["ref"] == "release/2" and colour(await OC.image(st, "sm")) == BLUE


@pytest.mark.parametrize("url, ok", [
    ("https://i.discogs.com/a.jpg", True), ("https://discogs.com/a.jpg", True),
    ("http://i.discogs.com/a.jpg", False), ("https://discogs.com.evil.invalid/a.jpg", False),
    ("https://evildiscogs.com/a.jpg", False), ("https://st.discogs.com/images/spacer.gif", False),
    ("https://10.0.0.1/a.jpg", False), ("", False), ("https://[bad", False),
])
def test_a_discogs_picture_comes_from_discogs_only(url, ok):
    assert OC._discogs_image_ok(url) is ok


async def test_a_refused_discogs_token_reads_as_nothing_found(w, monkeypatch):
    monkeypatch.setattr(settings, "discogs_token", "tok")
    w.discogs = 401
    st = await lookup(tr())
    assert st.known and not st.hit and not OC.paused()


# ── A song without an album ──────────────────────────────────────────────────

def song(**kw):
    return tr(album="", **kw)


def rec(title, artist, releases):
    return {"title": title, "artist-credit": [{"name": artist, "artist": {"id": "a", "name": artist}}],
            "releases": releases}


async def test_real_answer_a_song_takes_its_earliest_album(w):
    w.mb.append(('recording:"Money" AND artist:"Pink Floyd" AND status:official AND primarytype:album',
                 REAL["pink_floyd_money"]))
    dsotm = "f5093c06-23e3-404f-aeaa-40f72885ee3a"                # The Dark Side of the Moon
    w.pics[front("release-group", dsotm)] = jpeg(BLUE)
    st = await lookup(song(artist="Pink Floyd", title="Money"))
    assert st.hit and st.key == "t|pinkfloyd|money" and st.record["ref"] == f"release-group/{dsotm}"
    assert w.asked(CAA) == [front("release-group", dsotm)]
    assert len(w.queries()) == 1
    assert [mb for _u, mb, _h in w.calls] == [True, False]        # (only MusicBrainz is paced)


@pytest.mark.parametrize("artist", ["", "Unknown Artist", "Various Artists"])
def test_a_song_without_a_usable_artist_is_not_looked_up(artist):
    assert OC.subject(song(artist=artist, title="Money")) is None
    assert OC.subject(song(artist="Pink Floyd", title="Money")).kind == "song"


async def test_a_song_only_on_a_single_is_found_by_the_wider_search(w):
    w.mb.append(("AND primarytype:album", {"recordings": []}))
    w.mb.append(("recording:", {"recordings": [rec("Bicycle Race", "Queen", [
        rel("s1", "Bicycle Race", rg="g-single", primary="Single")])]}))
    w.pics[front("release-group", "g-single")] = jpeg(GREEN)
    st = await lookup(song())
    assert st.hit and colour(await OC.image(st, "sm")) == GREEN
    assert w.queries() == ['recording:"Bicycle Race" AND artist:"Queen" AND status:official AND primarytype:album',
                           'recording:"Bicycle Race" AND artist:"Queen" AND status:official']


def test_which_albums_a_song_is_taken_from():
    sub = OC.subject(song())
    answer = {"recordings": [
        rec("Bicycle Race (live)", "Queen", [rel("x1", "X", rg="g-other-title")]),
        rec("Bicycle Race", "Queen Tribute", [rel("x2", "X", rg="g-other-artist")]),
        rec("Bicycle Race", "Queen", [
            rel("b1", "Boot", rg="g-boot", status="Bootleg"),
            rel("l1", "Live Killers", rg="g-live", secondary=["Live"]),
            rel("c1", "Greatest Hits", rg="g-comp", secondary=["Compilation"]),
            rel("o1", "Other", rg="g-other", primary="Other"),
            rel("s1", "Bicycle Race", rg="g-single", primary="Single", date="1978-10-13"),
            rel("e1", "EP", rg="g-ep", primary="EP", date="1970"),
            rel("a2", "Jazz", rg="g-jazz", date="1994"),
            {"id": "n1", "title": "No group", "status": "Official"},
        ]),
        rec("bicycle race", "Queen", [rel("a1", "Jazz", rg="g-jazz", date="1978-11-10"),
                                      rel("a3", "Reissue", rg="g-later", date="1991")]),
    ]}
    assert OC._song_groups(answer, sub) == ["g-jazz", "g-later", "g-single", "g-ep"]
    assert OC._song_groups({}, sub) == [] and OC._song_groups(None, sub) == []


async def test_three_albums_are_tried_for_a_songs_picture(w):
    w.mb.append(("recording:", {"recordings": [rec("Bicycle Race", "Queen", [
        rel(f"r{i}", "A", rg=f"g{i}", date=str(1978 + i)) for i in range(5)])]}))
    w.pics[front("release-group", "g3")] = jpeg()
    st = await lookup(song())
    assert st.known and not st.hit
    assert w.asked(CAA) == [front("release-group", f"g{i}") for i in range(3)]
    assert len(w.queries()) == 1                                   # (albums were found: no wider search)


# ── What is remembered ───────────────────────────────────────────────────────

async def test_none_is_remembered_for_a_month(w):
    t = tr()
    st = await lookup(t)
    assert st.known and not st.hit and st.stamp == 0
    assert OC.request(st, "t1") is False and len(w.queries()) == 1
    OC._reset_for_tests()                                         # also after a restart
    assert (await OC.state(t)).known and len(w.queries()) == 1
    # A month on the answer is unknown again, and a cover found then is shown.
    p = next(w.root.glob("*/*.json"))
    old = json.loads(p.read_text())
    old["at"] -= OC._MISS_TTL + 5
    p.write_text(json.dumps(old))
    OC._reset_for_tests()
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    assert (await OC.state(t)).known is False
    assert (await lookup(t)).hit and len(w.queries()) == 2


async def test_a_month_less_a_minute_is_still_remembered(w):
    t = tr()
    await lookup(t)
    p = next(w.root.glob("*/*.json"))
    old = json.loads(p.read_text())
    old["at"] -= OC._MISS_TTL - 60
    p.write_text(json.dumps(old))
    OC._reset_for_tests()
    assert (await OC.state(t)).known


async def test_a_cover_does_not_expire(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    t = tr()
    await lookup(t)
    p = next(w.root.glob("*/*.json"))
    old = json.loads(p.read_text())
    old["at"] -= 10 * OC._MISS_TTL
    p.write_text(json.dumps(old))
    OC._reset_for_tests()
    assert (await OC.state(t)).hit


@pytest.mark.parametrize("status", [503, 429, 500, 200])
async def test_a_service_that_cannot_answer_is_not_remembered_and_pauses_the_lookups(w, status):
    w.mb.append(("release:", status if status != 200 else {"releases": "x"}))
    if status == 200:
        w.mb[0] = ("release:", None)                              # (an unreadable body, below)

        async def garbled(url, *, headers=None, mb=False):
            w.calls.append((url, mb, {}))
            return 200, b"<html>busy</html>", "text/html"
        OC._http_get = garbled
    a, b = tr("a"), tr("b", album="Innuendo")
    OC.request(await OC.state(a), "a")
    OC.request(await OC.state(b), "b")
    await settle()
    assert len(w.calls) == 1                                      # the other queued lookup was dropped
    assert not (await OC.state(a)).known and not (await OC.state(b)).known
    assert files(w) == [] and OC.paused() and not OC._pending
    assert OC.request(await OC.state(a), "a") is False            # paused: not even queued
    OC._paused_until = 0.0
    assert OC.request(await OC.state(a), "a") is True
    await settle()


def _busy_first(w, monkeypatch, times, status=503, waits=(0.25, 0.5)):
    """MusicBrainz answers ``status`` to its first ``times`` requests; the waits are recorded, not slept."""
    left, slept = [times], []
    real = w.get

    async def get(url, *, headers=None, mb=False):
        if url.startswith(OC._MB + "/") and left[0] > 0:
            left[0] -= 1
            w.calls.append((url, mb, dict(headers or {})))
            return status, b'{"error": "busy"}', "application/json"
        return await real(url, headers=headers, mb=mb)

    async def sleep(secs):
        slept.append(secs)
    monkeypatch.setattr(OC, "_http_get", get)
    monkeypatch.setattr(OC, "_BUSY_WAITS", waits)
    monkeypatch.setattr(OC.asyncio, "sleep", sleep)
    return slept


@pytest.mark.parametrize("status, times", [(503, 1), (429, 2)])
async def test_a_busy_musicbrainz_is_asked_again_and_the_cover_still_comes(w, monkeypatch, status, times):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg(BLUE)
    slept = _busy_first(w, monkeypatch, times, status)
    st = await lookup(tr())
    assert st.hit and not OC.paused()
    assert len(w.asked(OC._MB)) == times + 1 and slept == [0.25, 0.5][:times]


async def test_a_musicbrainz_that_stays_busy_pauses_the_lookups_after_the_last_wait(w, monkeypatch):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg(BLUE)
    slept = _busy_first(w, monkeypatch, 3)
    st = await lookup(tr())
    assert not st.known and OC.paused() and files(w) == []
    assert len(w.calls) == 3 and slept == [0.25, 0.5]              # asked three times, then left alone


async def test_another_musicbrainz_failure_is_not_asked_again(w, monkeypatch):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg(BLUE)
    slept = _busy_first(w, monkeypatch, 1, 500)
    st = await lookup(tr())
    assert not st.known and OC.paused() and slept == []
    assert len(w.asked(OC._MB)) == 1


async def test_only_musicbrainz_is_asked_again_when_busy(w, monkeypatch):
    monkeypatch.setattr(settings, "discogs_token", "tok")
    w.discogs = 429
    slept = _busy_first(w, monkeypatch, 0)
    st = await lookup(tr())
    assert not st.known and OC.paused() and slept == []
    assert len(w.asked(OC._DISCOGS)) == 1


async def test_no_connection_is_not_remembered(w):
    w.down = True
    st = await lookup(tr())
    assert not st.known and OC.paused() and files(w) == []


async def test_an_image_host_error_is_a_none_for_an_hour_of_this_run_only(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = (500, b"oops", "text/plain")
    w.pics[front("release", "r1")] = (503, b"oops", "text/plain")
    t = tr()
    st = await lookup(t)
    assert st.known and not st.hit and st.record["ttl"] == OC._SOFT_TTL
    assert files(w) == [] and not OC.paused()
    assert OC.request(st, "t1") is False                          # not asked again right away
    OC._memo[st.key]["at"] -= OC._SOFT_TTL + 1                     # an hour on
    assert (await OC.state(t)).known is False
    OC._reset_for_tests()                                         # or a restart
    assert (await OC.state(t)).known is False


async def test_a_bug_in_a_lookup_costs_that_lookup_only(w, monkeypatch):
    real = OC._lookup_album

    async def boom(sub, run):
        if sub.name == "Jazz":
            raise ValueError("bug")
        return await real(sub, run)
    monkeypatch.setattr(OC, "_lookup_album", boom)
    w.mb.append(('release:"Innuendo"', {"releases": [rel("r2", "Innuendo", rg="g2")]}))
    w.pics[front("release-group", "g2")] = jpeg()
    a, b = tr("a"), tr("b", album="Innuendo")
    OC.request(await OC.state(b), "b")
    OC.request(await OC.state(a), "a")                            # (served first: the newest)
    await settle()
    st = await OC.state(a)
    assert st.known and not st.hit and st.record["ttl"] == OC._SOFT_TTL
    assert (await OC.state(b)).hit and not OC.paused()


async def test_a_cover_that_cannot_be_stored_is_a_soft_none(w, monkeypatch):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()

    def full_disk(key, record, images):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(OC, "_write_record", full_disk)
    st = await lookup(tr())
    assert st.known and not st.hit and st.record["ttl"] == OC._SOFT_TTL and w.sent != []


async def test_a_record_whose_picture_is_gone_starts_over(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    t = tr()
    st = await lookup(t)
    for p in w.root.glob("*/*.sm.jpg"):
        p.unlink()
    assert await OC.image(st, "sm") is None
    assert files(w) == [] and (await OC.state(t)).known is False
    assert (await lookup(t)).hit and len(w.queries()) == 2


async def test_forgetting_the_nones_keeps_the_covers(w):
    w.mb.append(('release:"Jazz"', {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    hit, miss, miss2 = tr("a"), tr("b", album="Innuendo"), tr("c", album="Hot Space")
    for t in (hit, miss, miss2):
        await lookup(t)
    assert OC.forget_misses() == 2
    assert (await OC.state(hit)).hit
    assert not (await OC.state(miss)).known and not (await OC.state(miss2)).known
    assert files(w) == ["full.jpg", "json", "lg.jpg", "sm.jpg"]
    assert OC.forget_misses() == 0


async def test_forgetting_everything_drops_memory_and_the_queue(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    t = tr()
    await lookup(t)
    OC._pending["x"] = OC._Job("x", OC.subject(t))
    OC.forget_all()
    assert not OC._memo and not OC._pending
    assert (await OC.state(t)).hit                                # (the files are the caller's to remove)


async def test_a_lookup_that_ends_after_the_cache_was_cleared_stores_nothing(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    w.hook = lambda url: OC.forget_all() if url.startswith(CAA) else None
    st = await lookup(tr())
    assert not st.known and files(w) == [] and w.sent == []


# ── The setting and offline mode ─────────────────────────────────────────────

async def test_switched_off_nothing_is_looked_up_or_shown(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    t = tr()
    assert (await lookup(t)).hit
    w.setting = False
    st = await OC.state(t)
    assert st is OC.NOTHING and not st.hit and await OC.image(st, "sm") is None
    assert OC.request(st, "t1") is False
    w.setting = True
    assert (await OC.state(t)).hit                                # the stored cover is back


@pytest.mark.parametrize("value, on", [(True, True), (None, True), ("1", True), ("on", True),
                                       (False, False), (0, False), ("false", False), ("off", False), ("0", False)])
async def test_the_setting_values(w, value, on):
    w.setting = value
    assert await OC.enabled() is on


async def test_a_setting_that_cannot_be_read_is_off(monkeypatch, w):
    async def broken(key, default=None):
        raise RuntimeError("no store")
    monkeypatch.setattr(data, "get_config", broken)
    assert await OC.enabled() is False and await OC.state(tr()) is OC.NOTHING
    with pytest.raises(RuntimeError):                             # (the settings page must not show "off")
        await OC.setting_value()


async def test_switched_off_while_lookups_wait_stops_them(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.hook = lambda url: setattr(w, "setting", False)             # off during the first lookup
    for i in range(3):
        t = tr(f"t{i}", album=f"Album {i}")
        OC.request(await OC.state(t), t.id)
    await settle()
    assert len(w.calls) == 1 and not OC._pending and not OC.paused()


async def test_offline_mode_asks_nothing_and_keeps_showing_what_is_stored(w):
    w.mb.append(('release:"Jazz"', {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg(BLUE)
    have, new = tr("a"), tr("b", album="Innuendo")
    await lookup(have)
    n = len(w.calls)
    w.offline = True
    st = await OC.state(have)
    assert st.hit and colour(await OC.image(st, "lg")) == BLUE
    assert OC.request(await OC.state(new), "b") is False
    await settle()
    assert len(w.calls) == n and not OC.paused()


async def test_going_offline_while_lookups_wait_stops_them(w):
    tracks = [tr(f"t{i}", album=f"Album {i}") for i in range(3)]
    states = [await OC.state(t) for t in tracks]
    assert [OC.request(st, t.id) for st, t in zip(states, tracks)] == [True] * 3
    w.offline = True                                              # (before the first one ran)
    await settle()
    assert w.calls == [] and not OC._pending


def test_asking_outside_a_running_loop_does_nothing(w):
    st = OC.State(OC.subject(tr()), "a|queen|jazz", None)
    assert OC.request(st, "t1") is False and not OC._pending


# ── The queue ────────────────────────────────────────────────────────────────

async def test_the_newest_ask_is_served_first(w):
    tracks = [tr(name, album=name) for name in ("One", "Two", "Three")]
    states = [await OC.state(t) for t in tracks]
    for t, st in zip(tracks, states):                             # (no await: nothing runs meanwhile)
        OC.request(st, t.id)
    OC.request(states[1], "Two")                                  # asked again: now the newest
    await settle()
    assert [q.split('"')[1] for q in w.queries()] == ["Two", "Three", "One"]


async def test_no_connection_drops_what_is_queued(w):
    w.down = True
    tracks = [tr(name, album=name) for name in ("One", "Two", "Three")]
    states = [await OC.state(t) for t in tracks]
    for t, st in zip(tracks, states):
        OC.request(st, t.id)
    await settle()
    assert len(w.calls) == 1 and not OC._pending and OC.paused()   # (one attempt, not one per album)


async def test_the_queue_and_the_waiters_are_bounded(w, monkeypatch):
    monkeypatch.setattr(OC, "_MAX_PENDING", 3)
    monkeypatch.setattr(OC, "_MAX_WAITERS", 2)
    sub = OC.subject(tr())
    for i in range(5):
        OC._enqueue(f"k{i}", sub, f"t{i}")
    assert list(OC._pending) == ["k2", "k3", "k4"]                # the oldest asks went
    for i in range(5):
        OC._enqueue("k4", sub, f"w{i}")
    assert list(OC._pending["k4"].waiters) == ["t4", "w0"]


async def test_the_memory_of_answers_is_bounded(w, monkeypatch):
    monkeypatch.setattr(OC, "_MAX_MEMO", 3)
    for i in range(3):
        OC._memoise(f"k{i}", None)
    OC._memoise("k1", {"v": 1})                                   # (a known key: no growth)
    assert len(OC._memo) == 3
    OC._memoise("k9", None)
    assert list(OC._memo) == ["k9"]


async def test_an_ask_during_the_running_lookup_joins_it(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    late = tr("late")

    def hook(url):
        if url.startswith(CAA):
            OC._enqueue("a|queen|jazz", OC.subject(late), "late")
    w.hook = hook
    await lookup(tr("first"))
    assert [m["track_id"] for m in w.sent] == ["first", "late"] and len(w.queries()) == 1


# ── Pictures that are refused ────────────────────────────────────────────────

@pytest.mark.parametrize("pic", [
    (200, jpeg(), "text/html"),                         # not served as a picture
    (200, b"\xff\xd8" + b"x" * 5000, "image/jpeg"),     # not a picture
    (200, jpeg(RED, (99, 600)), "image/jpeg"),          # too small
    (200, jpeg()[:200], "image/jpeg"),                  # a stub
    (200, jpeg()[:1500], "image/jpeg"),                 # cut short
    (404, b"", ""), (403, b"", ""), (410, b"", ""), (413, b"", ""),
])
async def test_a_picture_that_is_not_one_is_refused(w, pic):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = pic
    w.pics[front("release", "r1")] = pic
    st = await lookup(tr())
    assert st.known and not st.hit and "ttl" not in st.record     # a plain "none", remembered
    assert files(w) == ["json"]


async def test_a_picture_over_the_size_limit_is_refused(w, monkeypatch):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    monkeypatch.setattr(OC, "_MAX_BYTES", len(jpeg()) - 1)
    st = await lookup(tr())
    assert st.known and not st.hit and files(w) == ["json"]


def test_a_picture_file_without_its_end_is_refused():
    whole = jpeg(BLUE, (600, 600), "PNG")
    assert OC._normalise(whole) is not None
    assert OC._normalise(whole[:-12]) is None                     # (the closing chunk is missing)


async def test_an_absurdly_large_picture_is_refused(w, monkeypatch):
    monkeypatch.setattr(OC, "_MAX_PIXELS", 500 * 500)
    assert OC._normalise(jpeg(RED, (600, 600))) is None
    assert OC._normalise(jpeg(RED, (500, 500))) is not None


def test_a_sideways_photo_is_turned_upright():
    im = Image.new("RGB", (400, 200), RED)
    exif = Image.Exif()
    exif[0x0112] = 6                                    # "rotate 90° to view"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    with Image.open(io.BytesIO(OC._normalise(buf.getvalue())["full"])) as out:
        assert out.size == (200, 400)


# ── The request itself (``_http_get``) ───────────────────────────────────────

@pytest.fixture
def net(monkeypatch):
    seen, paced = [], []

    async def pace():
        paced.append(1)
    monkeypatch.setattr(artistinfo, "mb_pace", pace)
    monkeypatch.setattr(OC, "_offline", lambda: False)
    n = SimpleNamespace(seen=seen, paced=paced, handler=None)

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return n.handler(req)
    monkeypatch.setattr(OC, "_transport", httpx.MockTransport(handle))
    return n


async def test_a_request_follows_the_redirect_and_names_the_app(net):
    def handler(req):
        if req.url.path == "/release/r1/front-1200":
            return httpx.Response(307, headers={"Location": "https://archive.invalid/pic.jpg"})
        return httpx.Response(200, content=b"JPEG", headers={"content-type": "image/jpeg"})
    net.handler = handler
    assert await OC._http_get(f"{CAA}/release/r1/front-1200") == (200, b"JPEG", "image/jpeg")
    assert [str(r.url) for r in net.seen] == [f"{CAA}/release/r1/front-1200", "https://archive.invalid/pic.jpg"]
    assert net.seen[0].headers["user-agent"] == artistinfo._UA and "SoniqBoom" in artistinfo._UA
    assert net.paced == []


async def test_only_musicbrainz_requests_take_a_pacing_slot(net):
    net.handler = lambda req: httpx.Response(200, json={})
    await OC._http_get(f"{OC._MB}/release?query=x", mb=True, headers={"X-Extra": "1"})
    assert net.paced == [1] and net.seen[0].headers["x-extra"] == "1"


async def test_an_oversized_body_reads_as_too_large(net, monkeypatch):
    monkeypatch.setattr(OC, "_MAX_BYTES", 1000)
    net.handler = lambda req: httpx.Response(200, content=b"x" * 1001, headers={"content-type": "image/jpeg"})
    assert await OC._http_get("https://i.invalid/a.jpg") == (413, b"", "")
    net.handler = lambda req: httpx.Response(200, content=b"x" * 1000, headers={"content-type": "image/jpeg"})
    assert (await OC._http_get("https://i.invalid/a.jpg"))[0] == 200


async def test_a_failed_connection_is_unreachable(net):
    def handler(req):
        raise httpx.ConnectError("no route")
    net.handler = handler
    with pytest.raises(OC.Unreachable):
        await OC._http_get("https://i.invalid/a.jpg")


async def test_offline_mode_sends_nothing(net, monkeypatch):
    monkeypatch.setattr(OC, "_offline", lambda: True)
    net.handler = lambda req: httpx.Response(200)
    with pytest.raises(OC.Unreachable):
        await OC._http_get("https://i.invalid/a.jpg", mb=True)
    assert net.seen == [] and net.paced == []


@pytest.mark.parametrize("status, denied_ok, out", [
    (404, False, None), (400, False, None), (401, True, None), (403, True, None),
    (401, False, "unreachable"), (403, False, "unreachable"), (429, True, "unreachable"),
    (500, False, "unreachable"), (503, False, "unreachable"),
])
async def test_which_answers_mean_nothing_and_which_mean_ask_later(net, status, denied_ok, out):
    net.handler = lambda req: httpx.Response(status, json={"error": "x"})
    if out is None:
        assert await OC._json("https://api.invalid/x", denied_ok=denied_ok) is None
    else:
        with pytest.raises(OC.Unreachable):
            await OC._json("https://api.invalid/x", denied_ok=denied_ok)


async def test_a_json_answer_that_is_not_an_object_is_nothing(net):
    net.handler = lambda req: httpx.Response(200, json=[1, 2])
    assert await OC._json("https://api.invalid/x") is None


# ── The ETag mark ────────────────────────────────────────────────────────────

def test_the_etag_is_marked_only_with_a_cover():
    sub = OC.subject(tr())
    hit = OC.State(sub, sub.key, {"v": 1, "ok": True, "at": 1700000000})
    miss = OC.State(sub, sub.key, {"v": 1, "ok": False, "at": 1700000000})
    assert OC.etag('"abc"', hit) == '"abc:o1700000000"'
    assert OC.etag('"abc"', miss) == '"abc"' and OC.etag('"abc"', OC.NOTHING) == '"abc"'
    assert OC.etag('"abc"', OC.State(sub, sub.key, None)) == '"abc"'
    later = OC.State(sub, sub.key, {"v": 1, "ok": True, "at": 1700000500})
    assert OC.etag('"abc"', later) != OC.etag('"abc"', hit)


# ── Review fixes: one image host down, long release lists, vague titles ──────

async def test_an_image_host_that_is_not_reached_costs_those_covers_only(w):
    w.mb.append(('release:"Jazz"', {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.mb.append(('release:"Innuendo"', {"releases": [rel("r2", "Innuendo", rg="g2")]}))
    w.pics[front("release-group", "g2")] = jpeg(GREEN)

    def timeout(url):
        if url.startswith(CAA):
            raise OC.Unreachable("ReadTimeout: ")
    w.hook = timeout
    st = await lookup(tr("a"))
    assert st.known and not st.hit and st.record["ttl"] == OC._SOFT_TTL
    assert not OC.paused() and files(w) == []
    assert len(w.asked(CAA)) == 1                                  # the host is left alone after one failure
    w.hook = None
    b = tr("b", album="Innuendo")
    st = await lookup(b)                                           # the other lookups go on, without that host
    assert len(w.queries()) == 2 and len(w.asked(CAA)) == 1
    assert st.known and not st.hit and st.record["ttl"] == OC._SOFT_TTL
    for host in OC._host_down:                                     # two minutes on
        OC._host_down[host] -= OC._PAUSE + 1
    OC._memo[st.key]["at"] -= OC._SOFT_TTL + 1
    assert (await lookup(b)).hit and len(w.asked(CAA)) == 2


async def test_going_offline_during_a_picture_is_no_answer_at_all(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.hook = lambda url: setattr(w, "offline", True) if url.startswith(CAA) else None
    st = await lookup(tr())
    assert not st.known and not OC._host_down and files(w) == []


def _paged(pages, count):
    """A MusicBrainz release search of ``count`` matches, served by offset."""
    def answer(url):
        off = int(parse_qs(urlsplit(url).query).get("offset", ["0"])[0])
        return {"count": count, "releases": pages.get(off, [])}
    return answer


def _many(n, start=0, rg="g1", date="1978"):
    return [rel(f"r{start + i}", "Jazz", rg=rg, date=date) for i in range(n)]


async def test_a_release_list_longer_than_one_page_is_read_on(w):
    # The second album of the name sits on the second page: without it the first would be taken.
    w.mb.append(("release:", _paged({0: _many(100), 100: _many(49, 100) + [rel("x", "Jazz", rg="g2", date="1994")]}, 150)))
    w.pics[front("release-group", "g1")] = jpeg(RED)
    w.pics[front("release-group", "g2")] = jpeg(BLUE)
    assert not (await lookup(tr("n"))).hit
    urls = w.asked(OC._MB + "/release?")
    assert len(urls) == 2 and "offset" not in urls[0] and urls[1] == urls[0] + "&offset=100"
    st = await lookup(tr("y", year=1994))
    assert st.hit and colour(await OC.image(st, "sm")) == BLUE


async def test_a_release_list_cut_off_needs_the_year(w, monkeypatch):
    w.mb.append(("release:", _paged({o: _many(100, o) for o in range(0, 2000, 100)}, 2000)))
    w.pics[front("release-group", "g1")] = jpeg(RED)
    assert not (await lookup(tr("n"))).hit                         # one album seen, but not every release read
    urls = w.asked(OC._MB + "/release?")
    assert len(urls) == OC._PAGES == 5 and urls[-1].endswith("&offset=400")
    assert w.asked(CAA) == []
    assert not (await lookup(tr("other", year=1999))).hit          # a year that album does not carry
    assert (await lookup(tr("y", year=1978))).hit                  # the year vouches for it


async def test_a_release_list_that_ends_early_stops_the_paging(w):
    w.mb.append(("release:", _paged({0: _many(100)}, 150)))
    w.pics[front("release-group", "g1")] = jpeg(RED)
    assert not (await lookup(tr("n"))).hit
    assert len(w.asked(OC._MB + "/release?")) == 2


@pytest.mark.parametrize("count", ["x", None, 3, 0])
async def test_a_release_list_of_one_page_is_asked_once(w, count):
    w.mb.append(("release:", {"count": count, "releases": _many(3)}))
    w.pics[front("release-group", "g1")] = jpeg(RED)
    assert (await lookup(tr())).hit and len(w.asked(OC._MB + "/release?")) == 1


@pytest.mark.parametrize("discogs_year, shown", [("1981", False), ("2004", True), ("", False)])
async def test_discogs_follows_the_year_musicbrainz_needed(w, monkeypatch, discogs_year, shown):
    # MusicBrainz knows two albums of the name; the track's year picks one, which has no
    # picture there.  The one Discogs album of the name is taken only when it is of that year.
    monkeypatch.setattr(settings, "discogs_token", "tok")
    w.mb.append(("release:", {"releases": [rel("r1", "Hits", rg="g-a", date="1981"),
                                           rel("r2", "Hits", rg="g-b", date="2004")]}))
    w.discogs = {"results": [{"id": 1, "master_id": 1, "title": "Queen - Hits", "year": discogs_year,
                              "cover_image": "https://i.discogs.com/a.jpg"}]}
    w.pics["https://i.discogs.com/a.jpg"] = jpeg(GREEN)
    st = await lookup(tr(album="Hits", year=2004))
    assert len(w.asked(OC._DISCOGS)) == 1 and st.hit is shown
    assert w.asked("https://i.discogs.com/") == (["https://i.discogs.com/a.jpg"] if shown else [])


async def test_a_single_discogs_album_needs_no_year_when_musicbrainz_knew_one(w, monkeypatch):
    monkeypatch.setattr(settings, "discogs_token", "tok")
    w.mb.append(("release:", {"releases": [rel("r1", "Hits", rg="g-a", date="1981")]}))
    w.discogs = {"results": [{"id": 1, "master_id": 1, "title": "Queen - Hits", "year": "1999",
                              "cover_image": "https://i.discogs.com/a.jpg"}]}
    w.pics["https://i.discogs.com/a.jpg"] = jpeg(GREEN)
    assert (await lookup(tr(album="Hits", year=2004))).hit


@pytest.mark.parametrize("title, looked_up", [
    ("Intro", False), ("Introduction", False), ("Outro", False), ("Interlude", False), ("Skit", False),
    ("Untitled", False), ("Unknown", False), ("Track 01", False), ("Track", False), ("Bonus Track", False),
    ("Hidden Track", False), ("Audio Track 12", False), ("[untitled]", False),
    ("1979", True), ("22", True), ("Intro to Love", True), ("Tracks of My Tears", True), ("Bicycle Race", True),
])
def test_a_song_whose_title_says_nothing_is_not_looked_up(title, looked_up):
    sub = OC.subject(tr(album="", title=title))
    assert (sub is not None) is looked_up
    assert OC.subject(tr(title=title)).kind == "album"            # (with an album tag the title does not matter)


@pytest.mark.parametrize("artist, key", [
    ("Queen feat. David Bowie", "queen"), ("Queen (feat. David Bowie)", "queen"), ("Queen [ft. Bowie]", "queen"),
    ("Queen ft. Bowie", "queen"), ("Queen Featuring David Bowie", "queen"), ("Queen feat David Bowie", "queen"),
    ("Queen FEAT. Bowie", "queen"),
    ("Little Feat & Emmylou Harris", "littlefeatandemmylouharris"), ("Little Feat", "littlefeat"),
])
def test_a_guest_credit_is_dropped_but_a_band_name_is_not(artist, key):
    assert OC.subject(tr(album="", artist=artist)).key == f"t|{key}|bicyclerace"


async def test_a_none_found_while_the_nones_were_forgotten_is_not_kept(w):
    w.hook = lambda url: OC.forget_misses() if url.startswith(OC._MB) else None
    t = tr()
    st = await lookup(t)                                           # (say: a Discogs token saved meanwhile)
    assert not st.known and files(w) == []
    w.hook = None
    assert (await lookup(t)).known and files(w) == ["json"]


async def test_a_cover_found_while_the_nones_were_forgotten_is_kept(w):
    w.mb.append(("release:", {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = jpeg()
    w.hook = lambda url: OC.forget_misses() if url.startswith(CAA) else None
    assert (await lookup(tr())).hit


async def test_what_is_still_open(w):
    assert OC.NOTHING.open is False
    t, none = tr(), tr("n", album="Hot Space")
    assert (await OC.state(t)).open                                # not looked up yet
    w.mb.append(('release:"Jazz"', {"releases": [rel("r1", "Jazz", rg="g1")]}))
    w.pics[front("release-group", "g1")] = (500, b"oops", "text/plain")
    w.pics[front("release", "r1")] = (500, b"oops", "text/plain")
    assert (await lookup(t)).open                                  # a none for the hour: asked again later
    assert (await lookup(none)).open is False                      # none found
    w.pics[front("release-group", "g1")] = jpeg()
    OC._reset_for_tests()
    assert (await lookup(t)).open is False                         # the cover


# ── The picture and the request, hardened ────────────────────────────────────

@pytest.mark.parametrize("fmt, taken", [("JPEG", True), ("PNG", True), ("WEBP", True), ("GIF", True),
                                        ("BMP", False), ("TIFF", False), ("ICO", False)])
def test_only_the_usual_picture_formats_are_decoded(fmt, taken):
    assert (OC._normalise(jpeg(RED, (256, 256), fmt)) is not None) is taken


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_a_see_through_picture_is_put_on_white(mode):
    buf = io.BytesIO()
    if mode == "P":
        im = Image.new("P", (300, 300), 0)
        im.putpalette([200, 0, 0] * 256)
        im.save(buf, "PNG", transparency=0)
    else:
        Image.new(mode, (300, 300), (200, 0, 0, 0) if mode == "RGBA" else (60, 0)).save(buf, "PNG")
    out = OC._normalise(buf.getvalue())
    with Image.open(io.BytesIO(out["sm"])) as im:
        assert all(c >= 250 for c in im.convert("RGB").getpixel((im.width // 2, im.height // 2)))


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/pic.jpg", "http://localhost:8080/pic.jpg", "http://10.0.0.5/pic.jpg",
    "http://192.168.1.1/pic.jpg", "http://[::1]/pic.jpg", "http://169.254.169.254/latest", "http://nas.local/pic.jpg",
    "http://db.internal/pic.jpg", "http://app.localhost/pic.jpg", "ftp://archive.invalid/pic.jpg", "file:///etc/passwd",
])
async def test_a_redirect_into_the_local_network_is_refused(net, location):
    net.handler = lambda req: (httpx.Response(302, headers={"Location": location})
                               if req.url.host == "coverartarchive.org"
                               else httpx.Response(200, content=b"INSIDE", headers={"content-type": "image/jpeg"}))
    with pytest.raises(OC.Unreachable, match="refused"):
        await OC._http_get(f"{CAA}/release/r1/front-1200")
    assert len(net.seen) == 1                                      # the target was never asked


@pytest.mark.parametrize("location, final", [
    ("/other/pic.jpg", "https://coverartarchive.org/other/pic.jpg"),
    ("https://93.184.216.34/pic.jpg", "https://93.184.216.34/pic.jpg"),
])
async def test_a_redirect_within_the_internet_is_followed(net, location, final):
    net.handler = lambda req: (httpx.Response(302, headers={"Location": location})
                               if req.url.path == "/release/r1/front-1200"
                               else httpx.Response(200, content=b"JPEG", headers={"content-type": "image/jpeg"}))
    assert (await OC._http_get(f"{CAA}/release/r1/front-1200"))[1] == b"JPEG"
    assert str(net.seen[-1].url) == final


async def test_a_request_that_trickles_is_given_up(net, monkeypatch):
    monkeypatch.setattr(OC, "_DEADLINE", 0.05)

    async def slow(req):
        await asyncio.sleep(5)
        return httpx.Response(200, content=b"JPEG")
    net.handler = slow
    started = time.monotonic()
    with pytest.raises(OC.Unreachable, match="TimeoutError"):
        await OC._http_get("https://i.invalid/a.jpg")
    assert time.monotonic() - started < 2
