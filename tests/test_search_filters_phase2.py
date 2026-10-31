# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search filters, phase 2 (internal/SEARCH-FILTERS-PLAN.md).

* A5 several values (``format:flac,alac``, ``-format:mp3,aac``).
* A6 ``genre:`` a word start (the genre's page stays exact).
* B5 ``folder:`` / ``path:`` (never a share's credentials), B6 ``group:`` /
  ``party:``, B7 ``status:``, B8 ``composer:`` / ``title:``, B9 ``lossless:``
  ``bitdepth:`` ``samplerate:`` ``bitrate:``, B10 ``source:``.

Every form parses through the one path; every list surface agrees with a
brute force written from the documented meaning; the search indexes follow
writes (and the audit sees them); caches follow writes that move no browse
state.
"""
from __future__ import annotations

import copy
import hashlib
import math
import random
import re
import unicodedata

import httpx
import pytest

from soniqboom.api import library
from soniqboom.api.search import _parse_advanced_query, run_search, search_predicates
from soniqboom.core import availability, catalogue, search_sets, search_values, shuffle_order
from soniqboom.core import store as store_mod
from soniqboom.core.data import _parse_tag_query, needs_uid
from soniqboom.core.store import TrackStore

from test_search_filters_phase1 import _all_shuffled, _app
from test_subsonic_parity import _tr, env  # noqa: F401 — fixture


# ── parse ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,want", [
    # A5 several values
    ("format:flac,alac", {"anyof": (((("format_", "FLAC"),), (("format_", "ALAC"),)),)}),
    ('genre:metal,"hard rock"',
     {"anyof": (((("genre_match", "metal"),), (("genre_match", "hard rock"),)),)}),
    ('genre:"Rock, Pop"', {"genre_match": "Rock, Pop"}),
    ("format:flac,", {"format_": "FLAC"}),
    ("format:flac,flac", {"format_": "FLAC"}),
    ("year:1985,1990-1992",
     {"anyof": (((("year_max", 1985), ("year_min", 1985)),
                 (("year_max", 1992), ("year_min", 1990))),)}),
    ("-format:mp3,aac", {"negate": ((("format_", "MP3"),), (("format_", "AAC"),))}),
    ("format:flac,alac genre:metal,rock",
     {"anyof": (((("format_", "FLAC"),), (("format_", "ALAC"),)),
                ((("genre_match", "metal"),), (("genre_match", "rock"),)))}),
    ("status:broken,glitched",
     {"anyof": (((("status", "broken"),), (("status", "glitched"),)),)}),
    ("source:hvsc,modland", {"anyof": (((("source", "hvsc"),), (("source", "modland"),)),)}),
    ("Format:FLAC,Alac", {"anyof": (((("format_", "FLAC"),), (("format_", "ALAC"),)),)}),
    # A6
    ("genre:metal", {"genre_match": "metal"}),
    ("-genre:podcast", {"negate": ((("genre_match", "podcast"),),)}),
    # B5
    ("folder:itunes", {"folder": "itunes"}),
    ('folder:"music/itunes"', {"folder": "music/itunes"}),
    ("path:.flac", {"path_match": ".flac"}),
    ("Path:smb://nas", {"path_match": "smb://nas"}),
    # B6
    ("group:fairlight", {"group_match": "fairlight"}),
    ('party:"revision 2019"', {"party": "revision 2019"}),
    # B7
    ("status:broken", {"status": "broken"}),
    ("status:CantPlay", {"status": "cantplay"}),
    ("-status:cantplay", {"negate": ((("status", "cantplay"),),)}),
    # B8
    ("composer:hubbard", {"composer_match": "hubbard"}),
    ('title:"song of"', {"title_match": "song of"}),
    # B9
    ("lossless:yes", {"lossless": "yes"}),
    ("lossless:False", {"lossless": "no"}),
    ("bitdepth:24", {"bitdepth_min": 24, "bitdepth_max": 24}),
    ("bitdepth:>=24", {"bitdepth_min": 24}),
    ("samplerate:>48000", {"samplerate_min": 48001}),
    ("samplerate:44.1k", {"samplerate_min": 44100, "samplerate_max": 44100}),
    ("samplerate:44.1khz-96k", {"samplerate_min": 44100, "samplerate_max": 96000}),
    ("samplerate: 44.1k - 96k", {"samplerate_min": 44100, "samplerate_max": 96000}),
    ("bitrate:<192", {"bitrate_max": 191}),
    ("bitrate:320k", {"bitrate_min": 320, "bitrate_max": 320}),
    ("bitrate:128-256kbps", {"bitrate_min": 128, "bitrate_max": 256}),
    # (review round: spellings people type)
    ("format:flac, alac", {"anyof": (((("format_", "FLAC"),), (("format_", "ALAC"),)),)}),
    ('genre:metal, "hard rock", jazz',
     {"anyof": (((("genre_match", "metal"),), (("genre_match", "hard rock"),),
                 (("genre_match", "jazz"),)),)}),
    ("format:flac, -live", {"format_": "FLAC", "negate": ((("query", "live"),),)}),
    ("samplerate:44.1", {"samplerate_min": 44100, "samplerate_max": 44100}),
    ("samplerate:>48", {"samplerate_min": 48001}),
    ("samplerate:44.1khz - 96khz", {"samplerate_min": 44100, "samplerate_max": 96000}),
    ("samplerate:44.1 khz - 96 khz", {"samplerate_min": 44100, "samplerate_max": 96000}),
    ("bitrate:128 kbps", {"bitrate_min": 128, "bitrate_max": 128}),
    # the same operator twice: both
    ("folder:music folder:live", {"folder": "music", "anyof": (((("folder", "live"),),),)}),
    ("year:1990 year:2000", {"year_min": 1990, "year_max": 1990,
                             "anyof": (((("year_max", 2000), ("year_min", 2000)),),)}),
    ("year:>1990 year:<2000", {"year_min": 1991, "year_max": 1999}),
    # B10
    ("source:hvsc", {"source": "hvsc"}),
    ("source:Scene.org", {"source": "sceneorg"}),
    ("source:share", {"source": "share"}),
    # combined with phase 1
    ("status:broken -format:mp3", {"status": "broken", "negate": ((("format_", "MP3"),),)}),
    ("format:flac,alac bitdepth:24 samplerate:>48000",
     {"anyof": (((("format_", "FLAC"),), (("format_", "ALAC"),)),),
      "bitdepth_min": 24, "bitdepth_max": 24, "samplerate_min": 48001}),
])
def test_parse_forms(q, want):
    assert search_predicates(q) == want


@pytest.mark.parametrize("q", [
    "status:fine", "lossless:maybe", "source:dropbox", "bitdepth:deep", "samplerate:44.0001",
    "samplerate:fast", "bitrate:high", "bitrate:1.5k", "format:,", "status:broken,fine",
    "year:1990,abc", 'genre:""',
])
def test_unreadable_values_stay_text(q):
    assert search_predicates(q) == {"query": q}


def test_unreadable_exclusion_is_a_word():
    assert search_predicates("-status:fine") == {"negate": ((("query", "status:fine"),),)}


def test_uid_follows_several_values():
    assert search_predicates("plays:0,>10", uid="u1")["uid"] == "u1"
    assert needs_uid({"anyof": (((("played", "<1d"),), (("format_", "X"),)),)})
    assert "uid" not in search_predicates("format:a,b", uid="u1")


def test_tag_group_round_trip():
    q = _parse_advanced_query('genre:"a{b}","c|d"')
    assert q == r"(@genre_match:{a\{b\}} | @genre_match:{c\|d})"
    assert _parse_tag_query(q) == {"anyof": (((("genre_match", "a{b}"),),
                                              (("genre_match", "c|d"),)),)}
    # a group next to free text and other terms
    assert search_predicates("live format:flac,alac year:2020") == {
        "anyof": (((("format_", "FLAC"),), (("format_", "ALAC"),)),),
        "year_min": 2020, "year_max": 2020, "query": "live"}


def test_values_helpers():
    sv = search_values.split_values
    assert sv('a,"b, c",d') == ["a", "b, c", "d"]
    assert sv(",,") == [] and sv('"x"') == ["x"]
    nv = search_values.number_value
    assert nv("44.1k", search_values.RATE_UNITS) == "44100"
    assert nv(">=96kHz", search_values.RATE_UNITS) == ">=96000"
    assert nv("44.1", search_values.RATE_UNITS) is None
    assert nv("22.05k-", search_values.RATE_UNITS) == "22050-"
    assert nv("320kbps", search_values.KBPS_UNITS) == "320"
    assert nv("1.5k", search_values.KBPS_UNITS) is None
    fold = store_mod._game_key
    assert search_values.path_texts("ftp://bob:s3cr@t@nas.lan:2121/pub:/Mods/A b.mod", fold) == \
        ("/pub/mods", "ftp://nas.lan:2121/pub/mods/a b.mod")
    assert search_values.path_texts("https://dav.example.com:/Artist/x.flac", fold) == \
        ("/artist", "https://dav.example.com/artist/x.flac")
    assert search_values.path_texts("/Music/Zip/a.zip::sub/x.mod", fold) == \
        ("/music/zip/a.zip/sub", "/music/zip/a.zip/sub/x.mod")
    assert search_values.path_texts("/Café/Ä.flac", fold) == ("/cafe", "/cafe/a.flac")
    # (review round) IPv6 hosts, "/" and "@" in a password, a web root's query
    assert search_values.path_texts("https://u:p@[::1]:8443/d:/a.flac", fold) == \
        ("/d", "https://[::1]:8443/d/a.flac")
    assert search_values.path_texts("ftp://bob:pa/ss@nas/pub:/x/y.mod", fold) == \
        ("/pub/x", "ftp://nas/pub/x/y.mod")
    assert search_values.path_texts("https://dav.example.com/dav?token=abc:/A/b.flac", fold) == \
        ("/dav/a", "https://dav.example.com/dav/a/b.flac")
    assert search_values.path_texts("ftp://nas:2121/pub/@x:/y/z.mod", fold) == \
        ("/pub/@x/y", "ftp://nas:2121/pub/@x/y/z.mod")         # (a port, then a folder "@x")


# ── store: semantics against a brute force ───────────────────────────────────

HVSC_ROOT = "https://hvsc.example/C64Music"
SHARES = {
    "ftp": "ftp://bob:hunter2@nas.lan/pub",
    "smb": "smb://nas.lan/music",
    "webdav": "https://alice:pw@dav.example.com/dav",
}
LOCAL = "/Users/me/Music"
GENRES = ["Heavy Metal", "Rock", "Hard Rock", "Punkrock", "Synth Pop", "Game", "Café Jazz"]
TITLES = ["Song of Myself", "Ghost Love Score", "don't stop", "Café del Mar", "Intro",
          "Songs of Praise", "Commando"]
COMPOSERS = ["Rob Hubbard", "Tuomas Holopainen", "Jean-Michel Jarre", "", "Martin Galway"]
GROUPS = [None, "Fairlight • Maniacs of Noise", "Triad", "Maniacs of Noise"]
PARTIES = [None, "Revision 2019", "Assembly 1994", "The Party 1992"]
FORMATS = ["MP3", "FLAC", "ALAC", "AAC", "SID", "ProTracker", "WAV"]


def _hash(root: str) -> str:
    return hashlib.sha256(root.encode()).hexdigest()[:16]


def _row(i: int, rnd: random.Random) -> dict:
    kind = rnd.choice(["local", "local", "hvsc", "ftp", "smb", "webdav", "loose"])
    folder = rnd.choice(["iTunes/Media", "Nightwish/Imaginaerum", "C64/Hubbard", "Mods",
                         "Crosby, Stills/Déjà Vu"])
    name = f"{i:05d} {rnd.choice(TITLES)}.{rnd.choice(['flac', 'mp3', 'sid'])}"
    if kind == "local":
        root, path = LOCAL, f"{LOCAL}/{folder}/{name}"
    elif kind == "loose":
        root, path = "", f"/Volumes/Ext/{folder}/{name}"
    elif kind == "hvsc":
        root, path = HVSC_ROOT, f"{HVSC_ROOT}:/MUSICIANS/{folder}/{name}"
    else:
        root = SHARES[kind]
        path = f"{root}:/{folder}/{name}"
    bitrate = rnd.choice([None, 128000, 191400, 192000, 192500, 320000, 1411200])
    fmt = rnd.choice(FORMATS)
    row = {
        "id": f"t{i:05d}", "path": path, "scan_root_hash": _hash(root) if root else "",
        "title": rnd.choice(TITLES), "artist": "x", "album": "y", "format": fmt,
        "genre": rnd.sample(GENRES, rnd.choice([0, 1, 2])),
        "composer": rnd.choice(COMPOSERS), "scene_group": rnd.choice(GROUPS),
        "scene_party": rnd.choice(PARTIES),
        "health": rnd.choice([None, None, "broken", "glitched"]),
        "defect": rnd.choice([None, None, None, "corrupt", "partial", "unplayable", "empty"]),
        "bit_depth": rnd.choice([None, 16, 24]),
        "sample_rate": rnd.choice([None, 22050, 44100, 48000, 96000]),
        "bitrate": bitrate, "added_at": i + 1,
    }
    lossless = rnd.choice([True, False, None])
    if lossless is not None:
        row["is_lossless"] = lossless
    return row


@pytest.fixture(autouse=True)
def _hvsc_registered(_no_modland_catalogue_network):
    """The online collection whose root the rows use (the conftest fixture
    unregisters every root before each test)."""
    catalogue.register_root(HVSC_ROOT, key="hvsc", label="HVSC")
    yield
    catalogue.unregister_root(HVSC_ROOT)


@pytest.fixture(scope="module")
def big():
    rnd = random.Random(20261023)
    s = TrackStore()
    for i in range(2500):
        s.upsert_track(_row(i, rnd))
    return s


def _fold(v: str) -> str:
    v = " ".join((v or "").split()).lower()
    out = "".join(ch for ch in unicodedata.normalize("NFKD", v) if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", out)


def _word_start(text: str, q: str) -> bool:
    return re.search(r"(?<![^\W_])" + re.escape(_fold(q)), _fold(text)) is not None


def _norm_path(p: str) -> tuple[str, str]:
    """(folders, whole path) — written from the documented meaning."""
    remote = "://" in p
    if remote:
        scheme, rest = p.split("://", 1)
        host, _, tail = rest.partition("/")
        host = host.rsplit("@", 1)[-1]
        tail = tail.replace(":/", "/", 1)
        p = f"{scheme}://{host}/{tail}"
    p = p.replace("::", "/")
    full = _fold(p)
    body = re.sub(r"^[a-z]+://[^/]*", "", full) if remote else full
    return body.rsplit("/", 1)[0], full


def _lossless(t) -> bool:
    v = t.get("is_lossless")
    if isinstance(v, bool):
        return v
    return t.get("format") in ("FLAC", "ALAC", "WAV")


def _source(t) -> str:
    p = t["path"]
    if p.startswith(HVSC_ROOT + ":/"):
        return "hvsc"
    for k, root in SHARES.items():
        if p.startswith(root + ":"):
            return k
    return "local"


def _kbps(t):
    b = t.get("bitrate")
    return None if not b else math.floor(b / 1000 + 0.5)


def _match(t: dict, cond: dict) -> bool:
    (k, v), = cond.items()
    if k == "fmt":
        return t.get("format") in v
    if k == "genre":
        return any(_word_start(g, v) for g in t.get("genre") or ())
    if k == "folder":
        return _word_start(_norm_path(t["path"])[0], v)
    if k == "path":
        return _fold(v) in _norm_path(t["path"])[1]
    if k == "group":
        return any(_word_start(g, v) for g in (t.get("scene_group") or "").split(" • ") if g)
    if k == "party":
        return _word_start(t.get("scene_party") or "", v)
    if k == "status":
        if v == "cantplay":
            return t.get("health") == "broken" or t.get("defect") in ("corrupt", "unplayable")
        return v in (t.get("health"), t.get("defect"))
    if k == "composer":
        return _word_start(t.get("composer") or "", v)
    if k == "title":
        return _word_start(t.get("title") or "", v)
    if k == "lossless":
        return _lossless(t) == (v == "yes")
    if k in ("bitdepth", "samplerate"):
        n = t.get("bit_depth" if k == "bitdepth" else "sample_rate")
        return n is not None and v[0] <= n <= v[1]
    if k == "bitrate":
        n = _kbps(t)
        return n is not None and v[0] <= n <= v[1]
    if k == "source":
        src = _source(t)
        if v == "share":
            return src in ("ftp", "smb", "webdav")
        if v == "collection":
            return src == "hvsc"
        return src == v
    if k == "any":
        return any(_match(t, c) for c in v)
    raise AssertionError(k)


def _brute(s: TrackStore, pos: list, neg: list = ()) -> set:
    return {tid for tid, t in s._tracks.items()
            if all(_match(t, c) for c in pos) and not any(_match(t, c) for c in neg)}


INF = math.inf
CASES = [
    ("format:flac,alac", [{"fmt": {"FLAC", "ALAC"}}], []),
    ("-format:mp3,aac", [], [{"fmt": {"MP3"}}, {"fmt": {"AAC"}}]),
    ("genre:metal", [{"genre": "metal"}], []),
    ("genre:rock", [{"genre": "rock"}], []),
    ('genre:metal,"synth pop"', [{"any": [{"genre": "metal"}, {"genre": "synth pop"}]}], []),
    ("genre:cafe", [{"genre": "cafe"}], []),
    ("-genre:rock", [], [{"genre": "rock"}]),
    ("folder:itunes", [{"folder": "itunes"}], []),
    ('folder:"itunes/media"', [{"folder": "itunes/media"}], []),
    ("folder:deja", [{"folder": "deja"}], []),
    ('folder:"crosby, stills"', [{"folder": "crosby, stills"}], []),
    ("folder:musicians", [{"folder": "musicians"}], []),
    ("folder:hunter2", [{"folder": "hunter2"}], []),
    ("folder:nas", [{"folder": "nas"}], []),
    ("path:.flac", [{"path": ".flac"}], []),
    ("path:smb://nas.lan", [{"path": "smb://nas.lan"}], []),
    ("path:hunter2", [{"path": "hunter2"}], []),
    ("path:bob", [{"path": "bob"}], []),
    ('path:"media/00"', [{"path": "media/00"}], []),
    ("group:maniacs", [{"group": "maniacs"}], []),
    ("group:fairlight,triad", [{"any": [{"group": "fairlight"}, {"group": "triad"}]}], []),
    ("party:revision", [{"party": "revision"}], []),
    ('party:"party 1992"', [{"party": "party 1992"}], []),
    ("status:broken", [{"status": "broken"}], []),
    ("status:glitched", [{"status": "glitched"}], []),
    ("status:corrupt", [{"status": "corrupt"}], []),
    ("status:partial", [{"status": "partial"}], []),
    ("status:unplayable", [{"status": "unplayable"}], []),
    ("status:empty", [{"status": "empty"}], []),
    ("status:cantplay", [{"status": "cantplay"}], []),
    ("-status:cantplay", [], [{"status": "cantplay"}]),
    ("status:broken,partial", [{"any": [{"status": "broken"}, {"status": "partial"}]}], []),
    ("composer:hubbard", [{"composer": "hubbard"}], []),
    ("composer:michel", [{"composer": "michel"}], []),
    ('composer:"jean-michel"', [{"composer": "jean-michel"}], []),
    ("title:song", [{"title": "song"}], []),
    ('title:"song of"', [{"title": "song of"}], []),
    ("title:cafe", [{"title": "cafe"}], []),
    ("title:don't", [{"title": "don't"}], []),
    ("title:stop", [{"title": "stop"}], []),
    ("title:ong", [{"title": "ong"}], []),
    ("lossless:yes", [{"lossless": "yes"}], []),
    ("lossless:no", [{"lossless": "no"}], []),
    ("bitdepth:24", [{"bitdepth": (24, 24)}], []),
    ("bitdepth:<24", [{"bitdepth": (-INF, 23)}], []),
    ("samplerate:>48000", [{"samplerate": (48001, INF)}], []),
    ("samplerate:44.1k-48k", [{"samplerate": (44100, 48000)}], []),
    ("bitrate:<192", [{"bitrate": (-INF, 191)}], []),
    ("bitrate:192", [{"bitrate": (192, 192)}], []),
    ("bitrate:193", [{"bitrate": (193, 193)}], []),
    ("bitrate:>=320", [{"bitrate": (320, INF)}], []),
    ("source:hvsc", [{"source": "hvsc"}], []),
    ("source:local", [{"source": "local"}], []),
    ("source:ftp", [{"source": "ftp"}], []),
    ("source:smb", [{"source": "smb"}], []),
    ("source:webdav", [{"source": "webdav"}], []),
    ("source:share", [{"source": "share"}], []),
    ("source:collection", [{"source": "collection"}], []),
    ("source:hvsc,smb", [{"any": [{"source": "hvsc"}, {"source": "smb"}]}], []),
    ("-source:local", [], [{"source": "local"}]),
    ("status:broken -format:mp3", [{"status": "broken"}], [{"fmt": {"MP3"}}]),
    ("source:hvsc composer:hubbard", [{"source": "hvsc"}, {"composer": "hubbard"}], []),
    ("format:flac,alac bitdepth:24 samplerate:>48000",
     [{"fmt": {"FLAC", "ALAC"}}, {"bitdepth": (24, 24)}, {"samplerate": (48001, INF)}], []),
    ("folder:itunes -lossless:yes", [{"folder": "itunes"}], [{"lossless": "yes"}]),
    ("group:maniacs genre:game,rock", [{"group": "maniacs"},
                                       {"any": [{"genre": "game"}, {"genre": "rock"}]}], []),
]


@pytest.mark.parametrize("q,pos,neg", CASES, ids=[c[0] for c in CASES])
def test_store_against_brute_force(big, q, pos, neg):
    want = _brute(big, pos, neg)
    preds = search_predicates(q)
    assert big.result_ids(**preds) == want
    assert {r["id"] for r in big.filter_tracks(**preds, limit=5000)} == want
    assert set(big.filter_track_ids(**preds)) == want
    p1 = [r["id"] for r in big.filter_tracks(**preds, limit=40, offset=0)]
    p2 = [r["id"] for r in big.filter_tracks(**preds, limit=40, offset=40)]
    assert not set(p1) & set(p2) and set(p1 + p2) <= want
    assert len(p1) == min(40, len(want))


def test_cases_are_not_trivial(big):
    """Every brute-force case selects something and not everything (else it
    tests nothing) — but the credential ones, a share's host (no folder) and
    the inside of a word (no word start), which must find nothing."""
    n = len(big._tracks)
    for q, pos, neg in CASES:
        got = len(_brute(big, pos, neg))
        if q in ("folder:hunter2", "path:hunter2", "path:bob", "folder:nas", "title:ong"):
            assert got == 0, q
        else:
            assert 0 < got < n, q


def test_genre_page_stays_exact():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a", "genre": ["Rock"], "added_at": 1})
    s.upsert_track({"id": "b", "path": "/m/b", "genre": ["Hard Rock"], "added_at": 2})
    assert s.result_ids(genre="Rock") == {"a"}                           # the page
    assert s.result_ids(**search_predicates("genre:rock")) == {"a", "b"}
    assert _parse_tag_query(r"@genre:{Rock}") == {"genre": "Rock"}       # the drill-down


def test_same_value_of_two_operators_never_shares_a_walk():
    """The kept walk of ``folder:x`` must not answer ``composer:x`` (the walk
    key holds the predicate names, not only their values)."""
    s = TrackStore()
    for i in range(6000):
        s.upsert_track({"id": f"t{i}", "path": f"/m/{'nightwish' if i % 2 else 'other'}/{i}.mp3",
                        "composer": "Nightwish" if i % 3 == 0 else "X",
                        "scene_party": "Nightwish" if i % 5 == 0 else None, "added_at": i})
    for q in ("folder:nightwish", "composer:nightwish", "party:nightwish", "path:nightwish",
              "title:nightwish"):
        preds = search_predicates(q)
        want = s.result_ids(**preds)
        got = [r["id"] for r in s.filter_tracks(**preds, limit=400)]
        assert set(got) <= want and len(got) == min(400, len(want)), q


def test_title_word_start_after_any_punctuation():
    s = TrackStore()
    for tid, title in (("a", "Rock’n’Roll"), ("b", "Intro–Outro"), ("c", "Live…Again"),
                       ("d", "don't stop"), ("e", "Stroll")):
        s.upsert_track({"id": tid, "path": f"/m/{tid}", "title": title, "added_at": 1})
    q = lambda text: s.result_ids(**search_predicates(text))          # noqa: E731
    assert q("title:roll") == {"a"}
    assert q("title:outro") == {"b"} and q("title:again") == {"c"}
    assert q("title:don't") == {"d"} and q('title:"don\'t st"') == {"d"}
    assert q("title:troll") == set()


def test_folder_with_slashes():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/Users/me/Music/iTunes/x.mp3", "added_at": 1})
    s.upsert_track({"id": "b", "path": "/Users/me/Musical/x.mp3", "added_at": 2})
    q = lambda text: s.result_ids(**search_predicates(text))          # noqa: E731
    assert q("folder:/music") == {"a", "b"}            # (a word start, like folder:music)
    assert q('folder:"/music/"') == {"a"}
    assert q('folder:"itunes/"') == {"a"}
    assert q('folder:"music/itunes"') == {"a"}
    assert q("folder:music") == {"a", "b"}


def test_credentials_are_never_indexed():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "ftp://bob:hunter2@nas/pub:/x/y.mod", "added_at": 1})
    s.upsert_track({"id": "b", "path": "https://u:p%40ss@dav.example.com:/A/b.flac",
                    "added_at": 2})
    blob = repr(s._path_text) + repr(s._tag_folder)
    assert "hunter2" not in blob and "bob" not in blob and "p%40ss" not in blob
    assert s.result_ids(**search_predicates("path:nas/pub/x")) == {"a"}
    assert s.result_ids(**search_predicates("path:dav.example.com/a/b")) == {"b"}


# ── the indexes follow writes; caches follow writes that move no browse state ─

def test_indexes_follow_writes_and_the_audit_sees_them():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/One/a.flac", "title": "Ghost", "composer": "X",
                    "scene_party": "Revision", "bit_depth": 16, "sample_rate": 44100,
                    "bitrate": 900000, "added_at": 1, "format": "FLAC"})
    s.upsert_track({"id": "b", "path": "/m/Two/b.mp3", "title": "Other", "added_at": 2,
                    "format": "MP3"})
    q = lambda text: s.result_ids(**search_predicates(text))           # noqa: E731
    assert q("status:broken") == set()
    seq, browse = s._search_seq, s._browse_seq
    s.update_track_fields("b", {"health": "broken"})
    assert q("status:broken") == {"b"} and q("status:cantplay") == {"b"}
    assert s._search_seq != seq and s._browse_seq == browse         # (no browse write)
    s.update_track_fields("a", {"scene_party": "Assembly", "bit_depth": 24,
                                "sample_rate": 96000, "is_lossless": False})
    assert q("party:revision") == set() and q("party:assembly") == {"a"}
    assert q("bitdepth:24") == {"a"} and q("samplerate:96k") == {"a"}
    assert q("lossless:no") == {"a", "b"}
    s.update_track_fields("a", {"path": "/m/Three/a.flac", "title": "Spirit", "composer": "Yann"})
    assert q("folder:one") == set() and q("folder:three") == {"a"}
    assert q("path:three/a.flac") == {"a"}
    assert q("title:ghost") == set() and q("title:spirit") == {"a"}
    assert q("composer:yann") == {"a"}
    assert s.verify_indexes()["index_ok"]
    s.delete_track("a")
    assert q("folder:three") == set() and q("title:spirit") == set()
    assert s._path_text.keys() == {"b"}
    assert s.verify_indexes()["index_ok"]


def test_drift_in_a_search_index_is_reported():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a", "health": "broken", "added_at": 1})
    s._tag_status["glitched"] = {"a"}
    rep = s.verify_indexes()
    assert not rep["index_ok"] and "_tag_status" in {m["index"] for m in rep["mismatches"]}


def test_walk_and_search_set_follow_a_mark(monkeypatch):
    """A health mark moves no browse state: the kept walk (a large candidate
    set) and a new search must still see it; an open list's handle keeps its
    snapshot."""
    s = TrackStore()
    monkeypatch.setattr(store_mod, "_store", s)
    availability.reset_for_tests()
    for i in range(7000):
        s.upsert_track({"id": f"t{i}", "path": f"/m/{i}", "added_at": i})
    q = search_predicates("-status:broken")
    assert s.filter_tracks(**q, limit=1)[0]["id"] == "t6999"
    r1, _ = search_sets.resolve(s, q, state=search_sets.State(s))
    s.update_track_fields("t6999", {"health": "broken"})
    assert s.filter_tracks(**q, limit=1)[0]["id"] == "t6998"
    kept, renewed = search_sets.resolve(s, q, rs=r1.rs, state=search_sets.State(s))
    assert kept is r1 and not renewed
    fresh, _ = search_sets.resolve(s, q, state=search_sets.State(s))
    assert fresh is not r1 and "t6999" not in fresh.ids
    availability.reset_for_tests()


def test_source_follows_the_catalogue_registry():
    root = "https://aminet.example/aminet"
    s = TrackStore()
    s.upsert_track({"id": "a", "path": f"{root}:/mods/x.mod", "scan_root_hash": _hash(root),
                    "added_at": 1})
    q = search_predicates("source:aminet")
    key0 = s.volatile_key(q)
    assert s.result_ids(**q) == set()                    # (not a registered collection)
    assert s.result_ids(**search_predicates("source:webdav")) == {"a"}
    catalogue.register_root(root, key="aminet", label="Aminet")
    try:
        assert s.volatile_key(q) != key0
        assert s.result_ids(**q) == {"a"}
        assert s.result_ids(**search_predicates("source:webdav")) == set()
    finally:
        catalogue.unregister_root(root)


def test_index_generation_moves_with_a_search_index_write():
    s = TrackStore()
    s.upsert_track({"id": "a", "path": "/m/a", "added_at": 1})
    g = s.index_generation()
    s.update_track_fields("a", {"bit_depth": 24})
    assert s.index_generation() != g


# ── every surface agrees (HTTP) ──────────────────────────────────────────────

@pytest.fixture
def lib(monkeypatch, big):
    s = copy.deepcopy(big)
    monkeypatch.setattr(store_mod, "_store", s)
    monkeypatch.setattr(library, "_AGG_CACHE", {})
    monkeypatch.setattr(library, "_AGG_ETAGS", {})
    availability.reset_for_tests()
    shuffle_order.clear()
    yield s
    availability.reset_for_tests()
    shuffle_order.clear()


SURFACE = ["format:flac,alac", "-format:mp3,aac", "genre:rock", "folder:itunes",
           "path:.flac", "group:maniacs", "party:revision", "status:broken -format:mp3",
           "composer:hubbard", 'title:"song of"', "lossless:yes bitdepth:24",
           "samplerate:>48000", "bitrate:<192", "source:hvsc composer:hubbard",
           "source:share", "folder:itunes -lossless:yes"]


def _want(s, q):
    for cq, pos, neg in CASES:
        if cq == q:
            return _brute(s, pos, neg)
    extra = {"lossless:yes bitdepth:24": ([{"lossless": "yes"}, {"bitdepth": (24, 24)}], [])}
    pos, neg = extra[q]
    return _brute(s, pos, neg)


@pytest.mark.parametrize("q", SURFACE)
async def test_every_surface_agrees(lib, q):
    want = _want(lib, q)
    assert want, q
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(None)),
                                 base_url="http://t") as c:
        r = (await c.get("/api/search/page", params={"q": q, "limit": 5000})).json()
        assert r["total"] == len(want) and {t["id"] for t in r["tracks"]} == want
        # (a shuffle deals only what can play: not the tracks auto-play passes by)
        deal = want - (availability.unplayable(lib) or set())
        total, got = await _all_shuffled(c, q)
        assert total == len(deal) and got == deal
        prev = (await c.get("/api/search", params={"q": q, "limit": 200})).json()
        assert {t["id"] for t in prev} <= want and len(prev) == min(200, len(want))
        quick = (await c.get("/api/search/quick", params={"q": q, "limit": 8})).json()
        assert {t["id"] for t in quick} <= want and len(quick) == min(8, len(want))
        f = (await c.get("/api/search/filter", params={"q": q, "limit": 2000})).json()
        assert {t["id"] for t in f} == want
    assert {t.id for t in await run_search(q, limit=5000)} == want


async def test_structured_filter_combines_q_with_its_fields(lib):
    want = _brute(lib, [{"genre": "game"}, {"status": "cantplay"}])
    exact_game = {tid for tid in want if "Game" in (lib._tracks[tid].get("genre") or [])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(None)),
                                 base_url="http://t") as c:
        r = (await c.get("/api/search/filter",
                         params={"genre": "Game", "q": "status:cantplay", "limit": 2000})).json()
        assert {t["id"] for t in r} == exact_game
        # the same field in both: both must match (here: nothing)
        r = (await c.get("/api/search/filter",
                         params={"format": "MP3", "q": "format:flac", "limit": 2000})).json()
        assert r == []
        r = (await c.get("/api/search/filter",
                         params={"scene_group": "Triad", "format": "MP3", "q": "format:flac",
                                 "limit": 2000})).json()
        assert r == []
        grp = (await c.get("/api/search/filter",
                           params={"scene_group": "Triad", "q": "status:broken,glitched",
                                   "limit": 2000})).json()
        assert {t["id"] for t in grp} == _brute(
            lib, [{"group": "triad"}, {"any": [{"status": "broken"}, {"status": "glitched"}]}])


def test_subsonic_search3_reads_the_new_operators(env):
    for tid, title, fmt in (("p1", "zz one", "FLAC"), ("p2", "zz two", "ALAC"),
                            ("p3", "zz three", "MP3")):
        env.store.upsert_track(_tr(tid, title=title, artist="Zed", store=env.store,
                                   added=9000, fmt=fmt))
    env.store.update_track_fields("p3", {"health": "broken"})
    ids = lambda q: sorted(s["id"] for s in env.ok("search3.view", query=q)  # noqa: E731
                           ["searchResult3"].get("song", []))
    assert ids("zz format:flac,alac") == ["p1", "p2"]
    assert ids("zz status:broken") == ["p3"]
    assert ids("zz -status:broken") == ["p1", "p2"]
    assert ids("zz title:two") == ["p2"]
    assert ids("zz folder:misc") == ["p1", "p2", "p3"]
