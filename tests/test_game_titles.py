"""Game names from archive names: the title lists, the matching rules, the
pass and its setting, the TOSEC / Redump downloads."""
import asyncio
import collections
import gzip
import io
import json
import sys
import time
import shutil
import zipfile

import pytest

from soniqboom.core import game_titles as gt
from soniqboom.core import game_titles_dat as dat
from soniqboom.core.store import TrackStore


def _t(tid, path, fmt="Jochen Hippel", **kw):
    d = {"id": tid, "path": path, "title": tid, "artist": "", "album": "", "format": fmt,
         "genre": [], "file_md5": tid.ljust(32, "0")}
    d.update(kw)
    return d


def _index(entries):
    """A TitleIndex from ``[(source, platform, title), …]``."""
    idx = gt.TitleIndex()
    for src, plat, title in entries:
        idx.add(src, plat, title)
    return idx


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    """No pause between requests to one site (``_PAUSE_S``); no download job
    left over from another test."""
    monkeypatch.setattr(gt, "_PAUSE_S", 0)
    gt._jobs.clear()
    gt._ctls.clear()
    yield
    gt._jobs.clear()
    gt._ctls.clear()


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr("soniqboom.core.data.get_store", lambda: s)

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr("soniqboom.core.folder_album.refresh_album_caches", _no_refresh)
    return s


@pytest.fixture
def small_index(monkeypatch):
    idx = _index([
        ("wikidata", "amiga", "Turrican II: The Final Fight"),
        ("wikidata", "amiga", "Turrican"),
        ("mame", "amiga", "Turrican"),
        ("wikidata", "amiga", "Lotus"),
        ("wikidata", "amiga", "Gold of the Aztecs"),
        ("nointro", "amiga", "Gold of the Aztecs"),
        ("wikidata", "amiga", "Street Racer"),
        ("wikidata", "c64", "The Last Ninja 2"),
        ("wikidata", "snes", "Super Mario World (USA)"),
        ("wikidata", "amiga", "Game Over"),
        ("mame", "amiga", "Game Over"),
        ("wikidata", "atarist", "Turrican"),
    ])
    monkeypatch.setattr(gt, "get_index", lambda: idx)
    return idx


async def _drain():
    """Let scheduled background passes finish."""
    for _ in range(200):
        if not gt._bg_tasks and not gt._running:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background pass did not finish")


# ── names ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a, b", [
    ("Turrican II", "turrican_2"), ("Last Ninja, The", "The Last Ninja"),
    ("Rock 'n' Roll", "Rock n Roll"), ("Rock 'n' Roll", "rock and roll"), ("Don't Go", "dont go"),
    ("Ghouls 'n Ghosts", "Ghouls and Ghosts"), ("Guns n' Roses", "guns and roses"),
    ("Fish'n'Chips", "fish and chips"), ("Wasn't Me", "wasnt me"), ("Lotus3", "Lotus 3"),
    ("Turrican +3", "Turrican"), ("R-Type", "r type"), ("Ghouls'n Ghosts", "ghouls n ghosts"),
    ("Pokémon", "pokemon"), ("Alpha Waves (Europe)", "alpha waves"), ("Fire & Ice", "fire and ice"),
    ("Ｔｕｒｒｉｃａｎ", "turrican"), ("Final Fantasy VII", "final fantasy 7"),
    ("Last Ninja 2, The", "last ninja 2"),
])
def test_two_spellings_of_a_title_share_a_key(a, b):
    assert gt.title_key(a) == gt.title_key(b) != ""


@pytest.mark.parametrize("a, b", [
    ("ガ", "カ"),                          # voiced and unvoiced kana are different words
    ("Turrican", "Turrican 2"), ("The", "Theme"), ("Nemesis", "Nemesis 2"),
    ("Alien", "Aliens"), ("Lotus 3", "Lotus 33"),
])
def test_different_titles_keep_different_keys(a, b):
    assert gt.title_key(a) != gt.title_key(b)


def test_the_and_trailer_rules_leave_the_word_itself():
    assert gt.title_key("The") == "the"              # a title that is only "The"
    assert gt.title_key("n") == "n"
    assert gt.title_key("Knight n Day") == "knight and day"
    assert gt.title_key("N Game") == "n game"        # only an "n" between words is "and"
    assert gt.title_key("Game n") == "game n"
    assert gt.title_key("Turrican +") == "turrican"
    assert gt.title_key("The Hobbit") == gt.title_key("Hobbit, The") == "hobbit"


def test_a_subtitle_gives_a_base_key():
    assert gt.base_key("Street Fighter II: The World Warrior") == "street fighter 2"
    assert gt.base_key("Turrican - The Final Fight") == "turrican"
    assert gt.base_key("R-Type") == ""               # a hyphen inside a word is no subtitle
    assert gt.base_key("Aladdin") == ""


@pytest.mark.parametrize("title, shown", [
    ("Alpha Waves (Europe)", "Alpha Waves"), ("Red Baron (Europe, v1.0) [a]", "Red Baron"),
    ("688(I) Hunter-Killer", "688(I) Hunter-Killer"), ("(Europe)", "(Europe)"),
])
def test_region_tags_are_cut_for_display(title, shown):
    assert gt.display_title(title) == shown


def test_the_first_list_spells_a_title_and_a_better_spelling_wins_within_one():
    idx = _index([("mame", "c64", "LAST NINJA, THE"), ("wikidata", "c64", "The Last Ninja")])
    assert idx.lookup("last ninja", ["c64"])[0] == "The Last Ninja"
    idx = _index([("tosec", "c64", "Last Ninja, The"), ("tosec", "c64", "The Last Ninja")])
    assert idx.lookup("last ninja", ["c64"])[0] == "The Last Ninja"
    # a later list never re-spells what an earlier one has, however short
    idx = _index([("wikidata", "c64", "The Last Ninja"), ("tosec", "c64", "Last Ninja")])
    assert idx.lookup("last ninja", ["c64"])[0] == "The Last Ninja"
    idx = _index([("tosec", "c64", "Last Ninja"), ("wikidata", "c64", "The Last Ninja")])
    assert idx.lookup("last ninja", ["c64"])[0] == "The Last Ninja"


def test_roman_numerals_are_preferred_for_display():
    idx = _index([("tosec", "amiga", "Cybernoid 2"), ("tosec", "amiga", "Cybernoid II")])
    assert idx.lookup("cybernoid 2", ["amiga"])[0] == "Cybernoid II"
    idx = _index([("tosec", "amiga", "Cybernoid II"), ("tosec", "amiga", "Cybernoid 2")])
    assert idx.lookup("cybernoid 2", ["amiga"])[0] == "Cybernoid II"


def test_all_caps_and_article_tails_rank_below_plain_spellings():
    assert gt._display_rank("Gods") < gt._display_rank("GODS")
    assert gt._display_rank("The Gods") < gt._display_rank("Gods, The")
    assert gt._display_rank("Pokemon") < gt._display_rank("Pokémon")


def test_the_index_counts_distinct_titles_and_both_sources():
    idx = _index([("wikidata", "amiga", "Turrican"), ("mame", "amiga", "TURRICAN"),
                  ("wikidata", "c64", "Turrican"), ("tosec", "amiga", "Turrican II: The Final Fight")])
    assert idx.total() == 3                          # amiga: turrican, turrican 2 · c64: turrican
    title, exact, btitle, base = idx.lookup("turrican", ["amiga"])
    assert (title, gt._bits(exact), btitle, base) == ("Turrican", 2, None, 0)
    title, exact, btitle, base = idx.lookup("turrican 2", ["amiga"])
    assert (title, exact, btitle, base) == (None, 0, "Turrican II", gt._SOURCE_BIT["tosec"])
    assert idx.lookup("turrican 2 the final fight", ["amiga"])[0] == "Turrican II: The Final Fight"
    assert idx.lookup("turrican", ["c64", "amiga"])[1] == gt._SOURCE_BIT["wikidata"] | gt._SOURCE_BIT["mame"]
    assert idx.lookup("nothing", ["amiga"]) == (None, 0, None, 0)


def test_a_tsv_list_loads_and_counts(tmp_path):
    p = tmp_path / "x.tsv.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("amiga\tTurrican\nc64\tCobra\nbad line\n\t\namiga\tTurrican: Remix\n")
    idx = gt.TitleIndex()
    assert idx.load_tsv("tosec", p) == 3
    assert idx.counts["tosec"] == {"amiga": 2, "c64": 1}
    assert idx.lookup("turrican", ["amiga"])[3] == gt._SOURCE_BIT["tosec"]


# ── platforms ─────────────────────────────────────────────────────────────────

_VGM = ("megadrive", "sms", "gamegear", "arcade", "pce", "msx", "x68000", "neogeo", "gb", "nes")


@pytest.mark.parametrize("fmt, plats, strict", [
    ("SID", ("c64",), False), ("ProTracker", ("amiga", "dos"), True),
    ("FastTracker 2", ("amiga", "dos"), True), ("AHX", ("amiga",), True),
    ("MED", ("amiga",), True), ("Sound FX", ("amiga",), True),
    ("ProTracker (packed)", ("amiga",), True),
    ("Jochen Hippel", ("amiga",), False), ("TFMX", ("amiga",), False),
    ("Jochen Hippel ST", ("atarist",), False), ("YM 2149", ("atarist",), False),
    ("Quartet PSG", ("atarist",), False), ("Sierra AGI", ("dos",), False),
    ("YM", ("atarist",), False), ("Sierra AdLib", ("dos",), False), ("SPC", ("snes",), False),
    ("GSF", ("gba",), False), ("DSF (Dreamcast)", ("dreamcast",), False),
    ("KSS", ("msx", "sms", "gamegear"), False), ("VGM", _VGM, False),
    ("MIDI", (), False), ("MP3", (), False), ("", (), False), (None, (), False),
])
def test_a_format_is_matched_on_its_platforms(fmt, plats, strict):
    assert gt.platforms_for(fmt) == (plats, strict)


# ── matching ──────────────────────────────────────────────────────────────────

def test_an_archive_named_like_a_game_of_the_platform_names_it(small_index):
    idx = small_index
    assert gt.archive_game("/m/Turrican.lha::mdat.song", "TFMX Pro", idx) == "Turrican"
    assert gt.archive_game("/m/turrican_ii.lha::x.hip", "Jochen Hippel", idx) == "Turrican II"
    # another platform's game is none of this track's
    assert gt.archive_game("/m/Turrican.lha::x.sid", "SID", idx) is None
    assert gt.archive_game("/m/Last Ninja 2.zip::x.hip", "Jochen Hippel", idx) is None
    # an Atari ST player's tune is matched on the Atari ST
    assert gt.archive_game("/m/Turrican.zip::x.hip", "Jochen Hippel ST", idx) == "Turrican"
    # not in an archive / not a retro format
    assert gt.archive_game("/m/Turrican/x.hip", "Jochen Hippel", idx) is None
    assert gt.archive_game("/m/Turrican.zip::x.mp3", "MP3", idx) is None


def test_a_title_before_its_subtitle_is_shown_as_the_list_spells_it(small_index):
    got = gt.archive_game("/m/street.zip::turrican_ii.lha::x.hip", "Jochen Hippel", small_index)
    assert got == "Turrican II"
    idx = _index([("wikidata", "snes", "Street Fighter II: The World Warrior")])
    assert gt.archive_game("/m/street_fighter_2.zip::01.spc", "SPC", idx) == "Street Fighter II"
    # a one-word name never matches a title's part before its subtitle
    idx = _index([("wikidata", "amiga", "Stormlord: Deliverance"), ("mame", "amiga", "Stormlord: Deliverance")])
    assert gt.archive_game("/m/Stormlord.zip::a.hip", "Jochen Hippel", idx) is None


@pytest.mark.parametrize("name", [
    "Revision", "Party", "Level 1", "Highscore", "Disk 1", "Music", "Io", "1942",
])
def test_each_refusal_holds_on_its_own(name):
    """Every name here is a title in two lists — it is refused anyway (a party,
    a part of a game's music, a generic word, under three letters, a number)."""
    idx = _index([("wikidata", "amiga", name), ("mame", "amiga", name)])
    assert gt.archive_game(f"/m/{name}.zip::a.hip", "Jochen Hippel", idx) is None


def test_a_three_letter_title_in_two_lists_is_a_game():
    idx = _index([("wikidata", "amiga", "Qix"), ("mame", "amiga", "Qix")])
    assert gt.archive_game("/m/Qix.zip::a.hip", "Jochen Hippel", idx) == "Qix"


def test_generic_part_and_credited_names_are_no_game(small_index):
    idx = small_index
    assert gt.archive_game("/m/Game Over.zip::a.hip", "Jochen Hippel", idx) is None
    assert gt.archive_game("/m/Music.zip::a.hip", "Jochen Hippel", idx) is None
    assert gt.archive_game("/m/1942.zip::a.hip", "Jochen Hippel", _index([
        ("wikidata", "amiga", "1942"), ("mame", "amiga", "1942")])) is None
    credits = {"/m/Turrican.lha": {"turrican"}}
    assert gt.archive_game("/m/Turrican.lha::a.hip", "Jochen Hippel", idx, credits=credits) is None
    # a credit of another archive doesn't count
    credits = {"/m/Other.lha": {"turrican"}}
    assert gt.archive_game("/m/Turrican.lha::a.hip", "Jochen Hippel", idx, credits=credits) == "Turrican"


def test_a_short_single_word_needs_two_lists(small_index):
    assert gt.archive_game("/m/Lotus.lha::a.hip", "Jochen Hippel", small_index) is None
    assert gt.archive_game("/m/Turrican.lha::a.hip", "Jochen Hippel", small_index) == "Turrican"
    # seven letters, one list: no; eight letters, one list: yes; two words, one list: yes
    idx = _index([("wikidata", "amiga", "Stunter"), ("wikidata", "amiga", "Stuntcar"),
                  ("wikidata", "amiga", "Hot Rod")])
    assert gt.archive_game("/m/Stunter.lha::a.hip", "Jochen Hippel", idx) is None
    assert gt.archive_game("/m/Stuntcar.lha::a.hip", "Jochen Hippel", idx) == "Stuntcar"
    assert gt.archive_game("/m/Hot Rod.lha::a.hip", "Jochen Hippel", idx) == "Hot Rod"
    idx.add("mame", "amiga", "Stunter")
    assert gt.archive_game("/m/Stunter.lha::a.hip", "Jochen Hippel", idx) == "Stunter"


def test_a_tracker_module_needs_two_words_and_two_lists(small_index):
    idx = small_index
    assert gt.archive_game("/m/Turrican.zip::title.mod", "ProTracker", idx) is None       # one word
    assert gt.archive_game("/m/Gold_of_the_Aztecs.zip::intro.mod", "ProTracker", idx) == \
        "Gold of the Aztecs"                                                                  # 2 lists
    assert gt.archive_game("/m/Street Racer.zip::a.mod", "ProTracker", idx) is None       # 1 list
    assert gt.archive_game("/m/Last Ninja 2.zip::a.mod", "ProTracker", idx) is None       # c64 only
    assert gt.archive_game("/m/Gold_of_the_Aztecs.zip::intro.ahx", "AHX", idx) == "Gold of the Aztecs"
    idx2 = _index([("wikidata", "amiga", "Street Fighter 2: X"), ("mame", "amiga", "Street Fighter 2: X")])
    assert gt.archive_game("/m/Street Fighter 2.zip::a.mod", "ProTracker", idx2) == "Street Fighter 2"
    idx2 = _index([("wikidata", "amiga", "Street Fighter 2: X")])
    assert gt.archive_game("/m/Street Fighter 2.zip::a.mod", "ProTracker", idx2) is None
    # two words are enough; a one-word inner archive passes the question outwards
    idx3 = _index([("wikidata", "amiga", "Alien Breed"), ("mame", "amiga", "Alien Breed")])
    assert gt.archive_game("/m/Alien Breed.zip::a.mod", "ProTracker", idx3) == "Alien Breed"
    assert gt.archive_game("/m/Alien Breed.zip::Turrican.zip::a.mod", "ProTracker",
                           idx3) == "Alien Breed"


def test_a_tracker_archive_named_like_one_of_its_songs_is_no_game(small_index):
    titles = {"/m/Gold of the Aztecs.zip": ["intro", "GOLD OF THE AZTECS!"]}
    assert gt.archive_game("/m/Gold of the Aztecs.zip::a.xm", "FastTracker 2", small_index,
                           tune_titles=titles) is None
    assert titles["/m/Gold of the Aztecs.zip"] == frozenset({"intro", "gold of the aztecs"})
    # another archive's song doesn't count; a SID's title does not refuse either
    assert gt.archive_game("/m/Gold of the Aztecs.zip::a.xm", "FastTracker 2", small_index,
                           tune_titles={"/m/Other.zip": ["x"]}) == "Gold of the Aztecs"
    # a song-named inner archive passes the question to the game archive around it
    titles = {"/m/Gold of the Aztecs.zip::Street Racer.zip": ["Street Racer"]}
    small_index.add("mame", "amiga", "Street Racer")
    assert gt.archive_game("/m/Gold of the Aztecs.zip::Street Racer.zip::a.xm", "FastTracker 2",
                           small_index, tune_titles=titles) == "Gold of the Aztecs"
    titles = {"/m/Other.zip": ["Gold of the Aztecs"], "/m/Gold of the Aztecs.zip": ["intro"]}
    assert gt.archive_game("/m/Gold of the Aztecs.zip::a.xm", "FastTracker 2", small_index,
                           tune_titles=titles) == "Gold of the Aztecs"
    tracks = [_t("b", "/m/Pack.zip::x.sid", "SID", title="Last Ninja 2"),
              _t("c", "/m/y.xm", "FastTracker 2", title="Loose"),
              _t("d", "/m/Pack.zip::z.mod", "ProTracker", title="  "),
              _t("a", "/m/Pack.zip::9lives.zip::9_LIVES2.XM", "FastTracker 2", title="9 Lives")]
    assert gt._tracker_titles(tracks) == {"/m/Pack.zip::9lives.zip": ["9 Lives"],
                                          "/m/Pack.zip": ["9 Lives"]}


async def test_the_pass_leaves_a_compo_entry_named_after_its_song(store, small_index):
    small_index.add("mame", "amiga", "Street Racer")
    store.upsert_tracks_batch([
        _t("a", "/m/compo/Street Racer.zip::STREET2.XM", "FastTracker 2", title="Street Racer"),
        _t("b", "/m/games/Street Racer.zip::title.xm", "FastTracker 2", title="sr-title"),
    ])
    await gt.apply_archive_games(force=True)
    assert store.get_track("a").get("game_by_archive") is None
    assert store.get_track("b")["game_by_archive"] == "Street Racer"


def test_an_archive_that_only_wraps_a_tracker_module_names_nothing(small_index):
    idx = small_index
    assert gt.archive_game("/m/A/GO.zip::gold_of_the_aztecs.mod.zip::gold_of_the_aztecs.mod",
                           "ProTracker", idx) is None
    assert gt.archive_game("/m/gold_of_the_aztecs.zip::gold_of_the_aztecs.mod", "ProTracker",
                           idx) is None
    # the enclosing archive is looked at instead
    assert gt.archive_game("/m/Gold of the Aztecs.lha::x.mod.zip::x.mod", "ProTracker",
                           idx) == "Gold of the Aztecs"
    # several tunes in it: a module named after the game's archive stays in it
    assert gt.archive_game("/m/Gold of the Aztecs.lha::gold of the aztecs.mod", "ProTracker", idx,
                           multi=frozenset({"/m/Gold of the Aztecs.lha"})) == "Gold of the Aztecs"


def test_a_custom_player_tune_s_own_archive_names_its_game_a_sid_s_does_not(small_index):
    idx = small_index
    assert gt.archive_game("/m/turrican.hip.zip::turrican.hip", "Jochen Hippel", idx) == "Turrican"
    assert gt.archive_game("/m/Turrican.zip::Turrican.hip", "Jochen Hippel", idx) == "Turrican"
    assert gt.archive_game("/m/Turrican.lha::music/turrican.hip", "Jochen Hippel", idx) == "Turrican"
    # a SID is named like a song ("halloween.zip::halloween.sid", a 2003 scene tune)
    assert gt.archive_game("/m/Last_Ninja_2.zip::Last_Ninja_2.sid", "SID", idx) is None
    assert gt.archive_game("/m/Last_Ninja_2.zip::tune.sid", "SID", idx) == "The Last Ninja 2"


def test_archive_names_are_listed_innermost_first():
    assert gt.archive_names("/m/Outer Pack.zip::Turrican.lha::x.hip") == [
        ("Turrican", "/m/Outer Pack.zip::Turrican.lha"), ("Outer Pack", "/m/Outer Pack.zip")]
    assert gt.archive_names("/m/axelf.mod.zip::axelf.mod") == []
    assert gt.archive_names("/m/axelf.mod.zip::axelf.mod", skip_wrapper=False) == [
        ("axelf", "/m/axelf.mod.zip")]
    assert gt.archive_names("/m/Turrican.hip.zip::t.hip", skip_wrapper=False) == [
        ("Turrican", "/m/Turrican.hip.zip")]
    # "<name>.mod.zip" wraps one module, whatever the module is called
    assert gt.archive_names("/m/Game.mod.zip::other.mod") == []
    assert gt.archive_names("/m/Pack.zip::Game.mod.zip::other.mod") == [("Pack", "/m/Pack.zip")]
    assert gt.archive_names("/m/x.hip") == []


def test_the_innermost_archive_that_names_a_game_wins(small_index):
    assert gt.archive_game("/m/Turrican.zip::Gold of the Aztecs.lha::x.hip", "Jochen Hippel",
                           small_index) == "Gold of the Aztecs"
    assert gt.archive_game("/m/Turrican.zip::disk1.adf::x.hip", "Jochen Hippel",
                           small_index) == "Turrican"
    # an inner archive named like no game passes the question outwards
    assert gt.archive_game("/m/Turrican.lha::Random Stuff.zip::x.hip", "Jochen Hippel",
                           small_index) == "Turrican"


def test_the_library_s_hvsc_games_count_as_a_list():
    idx = gt.TitleIndex()
    extra = {"c64": {gt.title_key("Cobra"): "Cobra"}}
    assert gt.archive_game("/m/Cobra.zip::x.sid", "SID", idx, extra=extra) is None      # one list
    idx.add("wikidata", "c64", "Cobra")
    assert gt.archive_game("/m/Cobra.zip::x.sid", "SID", idx, extra=extra) == "Cobra"
    extra = {"c64": {gt.title_key("Commando"): "Commando"}}                            # 8 letters
    assert gt.archive_game("/m/Commando.zip::x.sid", "SID", gt.TitleIndex(), extra=extra) == "Commando"
    tracks = [_t("a", "/C64Music/GAMES/A-F/Cobra.sid", "SID"),
              _t("b", "/C64Music/MUSICIANS/H/Hubbard_Rob/Commando.sid", "SID"),
              _t("c", "/C64Music/GAMES/G-L/Last_Ninja_2.sid", "SID")]
    assert gt._hvsc_games(tracks) == {"c64": {"cobra": "Cobra", "last ninja 2": "Last Ninja 2"}}
    assert gt._hvsc_games(tracks[1:2]) == {}


def test_the_bundled_lists_load_with_their_licences():
    idx = gt.get_index()
    assert idx.total() > 50_000
    for plat in ("c64", "amiga", "snes", "megadrive", "zx", "psx"):
        assert len(idx.keys.get(plat, {})) > 500, plat
    meta = json.loads((gt.BUNDLED_DIR / "sources.json").read_text())
    assert {s: meta[s]["licence"] for s in ("wikidata", "nointro", "mame", "zxdb")} == {
        "wikidata": "CC0 1.0", "nointro": "DAT-o-MATIC Data Usage License",
        "mame": "CC0 1.0", "zxdb": "ODbL 1.0"}
    assert gt.archive_game("/m/Turrican.lha::mdat.song", "TFMX Pro", idx) == "Turrican"
    assert gt.get_index() is idx                     # cached while no list changes


# ── credits ───────────────────────────────────────────────────────────────────

def test_an_archive_s_credits_come_from_its_own_tunes():
    tracks = [
        _t("a", "/m/Pack.zip::Fairlight.zip::a.sid", "SID", comment="1988 Fairlight"),
        _t("b", "/m/Pack.zip::b.sid", "SID", comment="(C) 1990 Thalamus / Hewson"),
        _t("c", "/m/Other.lha::c.hip", artist="Chris Huelsbeck", scene_group="TSK",
           label="Rainbow Arts", composer="C. Huelsbeck", album_artist="Various"),
        _t("d", "/m/loose.hip", artist="Nobody"),
        _t("e", "/m/Other.lha::e.hip", artist="  ", comment="1988 Ignored"),
        _t("f", "/m/Other.lha::f.hip", artist="Late Artist"),
    ]
    got = gt._archive_credits(tracks)
    assert got["/m/Pack.zip::Fairlight.zip"] == {"fairlight"}
    assert got["/m/Pack.zip"] == {"fairlight", "thalamus", "hewson"}
    from soniqboom.core.folder_album import name_key
    assert got["/m/Other.lha"] == {name_key(n) for n in (
        "Chris Huelsbeck", "TSK", "Rainbow Arts", "C. Huelsbeck", "Various", "Late Artist")}
    assert "/m/loose.hip" not in got


async def test_a_game_named_archive_is_kept_when_only_other_archives_credit_that_name(store, small_index):
    store.upsert_tracks_batch([
        _t("a", "/m/Turrican.lha::a.hip", artist="Chris Huelsbeck"),
        _t("b", "/m/Other.lha::b.hip", artist="Turrican"),          # an artist named like it
        _t("c", "/m/Game Over.lha::c.hip"),
    ])
    await gt.apply_archive_games(force=True)
    assert store.get_track("a")["game_by_archive"] == "Turrican"


async def test_an_archive_its_own_tunes_credit_as_their_group_is_no_game(store, small_index):
    store.upsert_tracks_batch([
        _t("a", "/m/Turrican.lha::a.hip", scene_group="Turrican"),
        _t("b", "/m/Turrican.lha::b.hip"),
    ])
    await gt.apply_archive_games(force=True)
    assert store.get_track("b").get("game_by_archive") is None


# ── the pass ──────────────────────────────────────────────────────────────────

async def test_the_pass_names_the_game_below_the_song_database_and_above_the_folder(store, small_index):
    store.upsert_tracks_batch([
        _t("a", "/m/Turrican.lha::a.hip"),
        _t("b", "/m/Turrican.lha::b.hip", album="Turrican 2", album_source="songdb"),
        _t("c", "/m/Turrican.lha::c.hip", game_by_folder="Some Folder"),
        _t("d", "/m/Other.lha::d.hip"),
    ])
    res = await gt.apply_archive_games(force=True)
    # "named" counts the tracks that SHOW the archive name: b's is dropped, "Turrican 2" starts with it
    assert (res["updated"], res["named"], res["primary"]) == (3, 2, 2)
    a, b, c, d = (store.get_track(x) for x in "abcd")
    assert (a["game"], a["game_source"], a["game_by_archive"]) == ("Turrican", "archive", "Turrican")
    assert (b["game"], b["game_source"]) == ("Turrican 2", "songdb")
    assert b.get("game_aliases") is None              # "Turrican 2" starts with it: no alias
    assert (c["game"], c["game_source"]) == ("Turrican", "archive")
    assert c["game_aliases"] == ["Some Folder"]
    assert d.get("game_by_archive") is None and not d.get("game")
    assert await gt.apply_archive_games() == {"skipped": "unchanged"}
    assert gt._last_result == res                     # a skip keeps the last result


async def test_a_new_track_or_list_runs_the_pass_again(store, small_index, monkeypatch):
    store.upsert_tracks_batch([_t("a", "/m/Turrican.lha::a.hip")])
    await gt.apply_archive_games(force=True)
    assert await gt.apply_archive_games() == {"skipped": "unchanged"}
    store.upsert_tracks_batch([_t("b", "/m/Turrican.lha::b.hip")])
    res = await gt.apply_archive_games()
    assert res["updated"] == 1 and store.get_track("b")["game"] == "Turrican"
    assert await gt.apply_archive_games() == {"skipped": "unchanged"}
    monkeypatch.setattr(gt, "_lists_sig", lambda: (("tosec", "x", 1),))
    assert "updated" in await gt.apply_archive_games()


async def test_a_track_that_arrives_during_the_pass_is_named_by_one_more(store, small_index, monkeypatch):
    store.upsert_tracks_batch([_t("a", "/m/Turrican.lha::a.hip")])
    real = gt.desired_slots
    calls = []

    def desired_then_scan(tracks, *, on=None, **kw):
        out = real(tracks, on=on, **kw)
        if not calls:                                # a scan lands while the first pass works
            store.upsert_tracks_batch([_t("b", "/m/Turrican.lha::b.hip")])
        calls.append(len(tracks))
        return out
    monkeypatch.setattr(gt, "desired_slots", desired_then_scan)
    await gt.apply_archive_games(force=True)
    assert gt._last_sig[0] != store.enrich_cursor()  # not past the scan: it missed a track
    await _drain()
    assert calls == [1, 2]
    assert store.get_track("b")["game"] == "Turrican"
    assert gt._last_sig is not None


async def test_switching_off_during_a_pass_stops_it_and_the_next_withdraws(store, small_index, monkeypatch):
    store.upsert_tracks_batch([_t(f"t{i}", f"/m/Turrican.lha::{i}.hip") for i in range(5)])
    monkeypatch.setattr(gt, "_CHUNK", 2)
    real = store.update_track_fields_batch
    writes = []

    def write_then_switch(items):
        writes.append(len(items))
        n = real(items)
        if len(writes) == 1:
            store.set_config(gt.CONFIG_KEY, False)   # the user switches it off mid-pass
        return n
    monkeypatch.setattr(store, "update_track_fields_batch", write_then_switch)
    res = await gt.apply_archive_games(force=True)
    assert res["updated"] == 2 and writes == [2]      # stopped after the first chunk
    await _drain()
    assert all(not store.get_track(f"t{i}").get("game_by_archive") for i in range(5))
    assert all(not store.get_track(f"t{i}").get("game") for i in range(5))


async def test_two_passes_never_overlap(store, small_index, monkeypatch):
    store.upsert_tracks_batch([_t("a", "/m/Turrican.lha::a.hip")])
    real = gt.desired_slots
    active = []
    peak = []

    def slow(tracks, *, on=None, **kw):
        active.append(1)
        peak.append(len(active))
        time.sleep(0.05)
        try:
            return real(tracks, on=on, **kw)
        finally:
            active.pop()
    monkeypatch.setattr(gt, "desired_slots", slow)
    r1, r2 = await asyncio.gather(gt.apply_archive_games(force=True), gt.apply_archive_games(force=True))
    assert max(peak) == 1
    assert r1["updated"] + r2["updated"] == 1


async def test_a_rescan_keeps_the_archive_name_and_the_setting_off_withdraws_it(store, small_index):
    from soniqboom.api import admin
    store.upsert_tracks_batch([_t("a", "/m/Turrican.lha::a.hip")])
    await gt.apply_archive_games(force=True)
    store.upsert_tracks_batch([_t("a", "/m/Turrican.lha::a.hip")])        # a rescan
    assert store.get_track("a")["game"] == "Turrican"
    r = await admin.update_settings({"game_from_archive_name": False}, _tok="x")
    assert r["archive_tracks_updated"] == 1 and r["archive_games_named"] == 0
    a = store.get_track("a")
    assert (a.get("game_by_archive"), a.get("game") or "", a.get("game_source")) == (None, "", None)
    r = await admin.update_settings({"game_from_archive_name": True}, _tok="x")
    assert (r["archive_games_named"], r["archive_games_primary"]) == (1, 1)
    assert store.get_track("a")["game"] == "Turrican"
    s = await admin.get_settings(_tok="x")
    assert s["game_from_archive_name"] is True
    r = await admin.update_settings({"game_from_archive_name": True}, _tok="x")   # unchanged
    assert "archive_tracks_updated" not in r


async def test_a_game_the_user_typed_is_kept(store, small_index):
    store.upsert_tracks_batch([_t("a", "/m/Turrican.lha::a.hip", game="My Game", user_edited=["game"])])
    await gt.apply_archive_games(force=True)
    a = store.get_track("a")
    assert (a["game"], a["game_by_archive"]) == ("My Game", "Turrican")


def test_desired_slots_withdraws_every_name_when_off(small_index):
    tracks = [_t("a", "/m/Turrican.lha::a.hip", game_by_archive="Turrican"),
              _t("b", "/m/Other.lha::b.hip"),
              _t("c", "/m/Other.lha::c.hip", game_by_archive="Stale")]
    assert gt.desired_slots(tracks, on=False) == {"a": None, "c": None}
    assert gt.desired_slots(tracks, on=True) == {"c": None}


async def test_schedule_coalesces_and_waits_out_a_scan(monkeypatch):
    runs = []

    async def fake_apply(force=False):
        runs.append(force)
        if len(runs) == 1:
            gt.schedule()                            # asked again while running
            gt.schedule()
        await asyncio.sleep(0)
        return {}
    monkeypatch.setattr(gt, "apply_archive_games", fake_apply)
    from soniqboom.core import scanner
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: False)
    gt.schedule()
    gt.schedule()
    await _drain()
    assert runs == [False, False]                    # one run, then one more for the calls meanwhile
    runs.clear()

    async def plain_apply(force=False):
        runs.append(force)
        return {}
    monkeypatch.setattr(gt, "apply_archive_games", plain_apply)
    scanning = [True]
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: scanning[0])
    monkeypatch.setattr(gt, "_SCAN_POLL_S", 0.01)
    gt.schedule()
    await asyncio.sleep(0.1)
    assert runs == [] and gt.pass_busy()             # waits while the scan runs …
    scanning[0] = False                              # … which may end with no pass of its own
    await _drain()
    assert runs == [False]


async def test_schedule_marks_the_runner_at_once(monkeypatch):
    started = []

    async def fake_apply(force=False):
        started.append(1)
        return {}
    monkeypatch.setattr(gt, "apply_archive_games", fake_apply)
    from soniqboom.core import scanner
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: False)
    gt.schedule()
    assert gt._running is True and len(gt._bg_tasks) == 1
    gt.schedule()                                    # joins the runner: no second task
    assert len(gt._bg_tasks) == 1
    await _drain()
    assert started == [1]


def test_enabled_falls_back_to_on(monkeypatch):
    def boom():
        raise RuntimeError("no store yet")
    monkeypatch.setattr("soniqboom.core.store.get_store", boom)
    assert gt.enabled() is True


def test_schedule_without_an_event_loop_does_nothing(monkeypatch):
    monkeypatch.setattr(gt, "_pending", False)
    gt.schedule()
    assert gt._running is False and gt._pending is False and gt.pass_busy() is False


async def test_a_failing_pass_does_not_stop_the_next(monkeypatch):
    runs = []

    async def boom(force=False):
        runs.append(1)
        if len(runs) == 1:
            gt.schedule()
            raise RuntimeError("x")
        return {}
    monkeypatch.setattr(gt, "apply_archive_games", boom)
    from soniqboom.core import scanner
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: False)
    gt.schedule()
    await _drain()
    assert len(runs) == 2


async def test_status_reports_distinct_titles_and_the_last_pass(store, small_index):
    store.upsert_tracks_batch([_t("a", "/m/Turrican.lha::a.hip"),
                               _t("b", "/m/Turrican.lha::b.hip", game="Mine", user_edited=["game"])])
    await gt.apply_archive_games(force=True)
    st = gt.status()
    assert st["titles"] == small_index.total()
    assert st["last_pass"]["named"] == 2 and st["last_pass"]["primary"] == 1
    assert st["enabled"] is True
    assert set(st["downloads"]) == {"tosec", "redump"}
    assert st["downloads"]["tosec"]["present"] is False


# ── DAT files ─────────────────────────────────────────────────────────────────

def test_dat_naming_conventions_are_cut():
    assert dat.tosec_title("Turrican II v1.1 (1991)(Rainbow Arts)(Disk 1 of 2)[cr X]") == "Turrican II"
    assert dat.tosec_title("Bundesliga Manager v1.3 rev1 (1990)") == "Bundesliga Manager"
    assert dat.nointro_title("Super Mario World (USA) (Rev 1)") == "Super Mario World"
    store = {"c64": set()}
    dat.emit(store, "c64", "Last Ninja, The + Last Ninja 2")
    assert store["c64"] == {"Last Ninja, The + Last Ninja 2", "Last Ninja, The",
                            "The Last Ninja", "Last Ninja 2"}
    store = {"amiga": set()}
    dat.emit(store, "amiga", "Pinball Dreams CD32 v1.2 ~ X")
    assert store["amiga"] == {"Pinball Dreams CD32 v1.2 ~ X", "Pinball Dreams CD32 v1.2",
                              "Pinball Dreams ~ X", "Pinball Dreams"}


@pytest.mark.parametrize("typ, games", [
    ("Games", True), ("Games - Adventure", True), ("CD - Games", True), ("Homebrew - Games", True),
    ("Demos", False), ("Applications", False), ("Compilations - Games", False),
    ("Games - Save Disks", False), ("Games - Addons & Patches", False), ("Collections - Games", False),
])
def test_tosec_games_trees_are_told_apart(typ, games):
    assert dat.tosec_is_games(typ) is games


def test_multi_game_parts_and_aliases_need_a_few_letters():
    store = {"c64": set()}
    dat.emit(store, "c64", "Io + Qix")
    assert store["c64"] == {"Io + Qix", "Qix"}       # a part needs three letters
    store = {"c64": set()}
    dat.emit(store, "c64", "X")
    assert store["c64"] == set()
    dat.emit(store, "c64", "Io")                     # a whole title needs two
    assert store["c64"] == {"Io"}


def test_no_intro_and_redump_keep_rules():
    assert dat._keep_entry("X (USA)", [], need_category=False) is True
    assert dat._keep_entry("X (USA)", [], need_category=True) is False
    assert dat._keep_entry("X (USA)", ["Games"], need_category=True) is True
    assert dat._keep_entry("X (USA)", ["Preproduction"], need_category=True) is True
    assert dat._keep_entry("X (USA)", ["Demos"], need_category=False) is False
    assert dat._keep_entry("[BIOS] X (USA)", ["Games"], need_category=False) is False


def _dat(games):
    body = "".join(f'<game name="{n}"><description>{n}</description>'
                   + (f"<category>{c}</category>" if c else "") + "</game>" for n, c in games)
    return f'<?xml version="1.0"?><datafile><header/>{body}</datafile>'


def _tosec_zip(path):
    """Every member the reader skips comes before the one games DAT."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("TOSEC/readme.txt", "x")
        z.writestr("TOSEC-PIX/Commodore C64 - Games (TOSEC-v2025-03-13).dat",
                   _dat([("Pic (1990)", None)]))
        z.writestr("Commodore C64 - Games (TOSEC-v2025-03-13).dat", _dat([("Top (1990)", None)]))
        z.writestr("TOSEC/Commodore C64 - Demos - [D64] (TOSEC-v2025-03-13).dat",
                   _dat([("Some Demo (1990)(Group)", None)]))
        z.writestr("TOSEC/Unknown Machine - Games (TOSEC-v2025-03-13).dat",
                   _dat([("Other (1990)", None)]))
        z.writestr("TOSEC/Commodore C64 - Games - [D64] (TOSEC-v2025-03-13).dat",
                   _dat([("Last Ninja 2 (1988)(System 3)[a]", None)]))


def test_a_tosec_pack_gives_its_games_only(tmp_path):
    p = tmp_path / "tosec.zip"
    _tosec_zip(p)
    got = dat.titles_from_tosec_pack(str(p))
    assert dict(got) == {"c64": {"Last Ninja 2"}}


def test_a_redump_datfile_keeps_categorised_games(tmp_path):
    p = tmp_path / "psx.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", "x")
        z.writestr("Sony - PlayStation - Datfile (2026-06-15).dat", _dat([
            ("Wipeout (Europe)", "Games"), ("Demo One (USA)", "Demos"), ("Mystery (Japan)", None)]))
        z.writestr("Second.DAT", _dat([("Ridge Racer (USA)", "Games")]))
    assert dict(dat.titles_from_redump_zip(str(p), "psx")) == {"psx": {"Wipeout", "Ridge Racer"}}


def test_an_oversized_dat_is_skipped(tmp_path, monkeypatch):
    p = tmp_path / "psx.zip"
    small = _dat([("Tekken (Europe)", "Games")])
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("big.dat", _dat([("Wipeout (Europe)", "Games")]) + " " * 200)
        z.writestr("small.dat", small)
    monkeypatch.setattr(dat, "MAX_DAT_BYTES", len(small))
    assert dict(dat.titles_from_redump_zip(str(p), "psx")) == {"psx": {"Tekken"}}
    t = tmp_path / "tosec.zip"
    with zipfile.ZipFile(t, "w") as z:
        z.writestr("TOSEC/Commodore C64 - Games - [D64] (TOSEC-v1).dat",
                   _dat([("Big (1990)", None)]) + " " * 200)
        z.writestr("TOSEC/Commodore C64 - Games - [T64] (TOSEC-v1).dat", _dat([("Cobra (1987)", None)]))
    monkeypatch.setattr(dat, "MAX_DAT_BYTES", len(_dat([("Cobra (1987)", None)])))
    assert dict(dat.titles_from_tosec_pack(str(t))) == {"c64": {"Cobra"}}


def test_no_intro_dats_are_picked_by_name(tmp_path):
    (tmp_path / "Nintendo - Super Nintendo Entertainment System (20260926).dat").write_text(
        _dat([("Super Mario World (USA)", "Games"), ("Test Cart (USA) (Program)", None),
              ("Mystery (Japan)", None), ("Some Demo (USA)", "Demos")]))
    (tmp_path / "Commodore - Amiga (20260926).dat").write_text(_dat([("Turrican (Europe)", None)]))
    got = dat.titles_from_nointro_dats([str(p) for p in tmp_path.iterdir()])
    # uncategorised No-Intro entries are kept; the SPS-credited Amiga DAT is left out
    assert dict(got) == {"snes": {"Super Mario World", "Mystery"}}


# ── downloads ─────────────────────────────────────────────────────────────────

async def _wait(source):
    for _ in range(400):
        job = gt._jobs.get(source)
        if job and job["state"] in ("done", "error"):
            await asyncio.sleep(0)
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("download did not finish")


def _tosec_fetch(pack, seen, *, newest_has_pack=True):
    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        seen.append(url)
        assert (max_bytes == gt._MAX_PAGE) == (dest is None)
        if url == gt._TOSEC_HOME:
            return (b'<a href="/downloads/category/58-2024-05-17">old</a>'
                    b'<a href="/downloads/category/59-2025-03-13">new</a>'
                    b'<a href="/downloads/category/59-2025-03-13">again</a>')
        if url.endswith("/downloads/category/59-2025-03-13"):
            if not newest_has_pack:
                return b"<p>coming soon</p>"
            return b'<a href="/downloads/category/59-2025-03-13?download=117:tosec-dat-pack-complete-4743">pack</a>'
        if url.endswith("/downloads/category/58-2024-05-17"):
            return (b'<a href="https://www.tosecdev.org/downloads/category/58-2024-05-17?'
                    b'download=99:tosec-dat-pack-complete-4000&amp;x=1">pack</a>')
        assert "download=" in url and dest is not None and deadline is not None
        dest.write_bytes(pack.read_bytes())
        if progress:
            progress(pack.stat().st_size, pack.stat().st_size)
        return None
    return fake_fetch


async def test_a_tosec_download_finds_the_newest_release(tmp_path, monkeypatch, store):
    pack = tmp_path / "pack.zip"
    _tosec_zip(pack)
    import threading
    seen = []
    gate = threading.Event()
    fetch = _tosec_fetch(pack, seen)

    def held(*a, **k):                                # the job stays running until both checks ran
        gate.wait(10)
        return fetch(*a, **k)
    monkeypatch.setattr(gt, "_fetch", held)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    job = gt.start_download("tosec")
    assert job["state"] in ("starting", "finding")    # its thread may have begun already
    assert gt.start_download("tosec") is job          # a second click joins the running one
    gate.set()
    assert (await _wait("tosec"))["state"] == "done"
    assert len(seen) == 3 and "download=117" in seen[-1]
    st = gt.status()
    d = st["downloads"]["tosec"]
    assert d["present"] and d["titles"] == 1 and d["release"] == "2025-03-13"
    assert d["bytes"] == d["total"] == pack.stat().st_size
    with gzip.open(gt._download_dir() / "tosec.tsv.gz", "rt") as fh:
        assert fh.read() == "c64\tLast Ninja 2\n"
    assert sorted(p.name for p in gt._download_dir().iterdir()) == ["sources", "tosec.json", "tosec.tsv.gz"]
    assert sorted(p.name for p in gt._sources_dir("tosec").iterdir()) == [
        "files.json", "tosec-dat-pack-2025-03-13.zip"]                   # the pack is kept
    assert d["files"] == {"bytes": pack.stat().st_size, "release": "2025-03-13", "resumable": False}
    assert gt.get_index().lookup("last ninja 2", ["c64"])[1] & gt._SOURCE_BIT["tosec"]
    assert gt.remove_download("tosec") is True
    assert not (gt._download_dir() / "tosec.tsv.gz").exists()
    assert gt.remove_download("tosec") is False


async def test_tosec_falls_back_to_an_earlier_release_with_a_pack(tmp_path, monkeypatch, store):
    pack = tmp_path / "pack.zip"
    _tosec_zip(pack)
    seen = []
    monkeypatch.setattr(gt, "_fetch", _tosec_fetch(pack, seen, newest_has_pack=False))
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    gt.start_download("tosec")
    assert (await _wait("tosec"))["state"] == "done"
    assert seen[-1] == ("https://www.tosecdev.org/downloads/category/58-2024-05-17?"
                        "download=99:tosec-dat-pack-complete-4000&x=1")
    assert gt._read_meta("tosec")["release"] == "2024-05-17"


@pytest.mark.parametrize("home, msg", [
    (b"<p>nothing</p>", "no TOSEC release"),
    (b'<a href="/downloads/category/59-2025-03-13">new</a>', "no DAT pack"),
])
async def test_tosec_without_a_release_or_pack_reports_it(monkeypatch, store, home, msg):
    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        return home if url == gt._TOSEC_HOME else b"<p>none</p>"
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    gt.start_download("tosec")
    job = await _wait("tosec")
    assert job["state"] == "error" and msg in job["message"]
    assert not (gt._download_dir() / "tosec.tsv.gz").exists()


async def test_a_tosec_pack_without_games_is_an_error(tmp_path, monkeypatch, store):
    pack = tmp_path / "pack.zip"
    with zipfile.ZipFile(pack, "w") as z:
        z.writestr("TOSEC/Commodore C64 - Demos (TOSEC-v1).dat", _dat([("D (1990)", None)]))
    monkeypatch.setattr(gt, "_fetch", _tosec_fetch(pack, []))
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    gt.start_download("tosec")
    job = await _wait("tosec")
    assert job["state"] == "error" and "no games" in job["message"]


_zip_clock = iter(range(10**6))


def _redump_zip(titles):
    """As redump.org serves it: built anew each time — its timestamp differs."""
    buf = io.BytesIO()
    n = next(_zip_clock)
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("list.dat", date_time=(2026, 1 + n // 28 % 12, 1 + n % 28, 0, 0, 0)),
                   _dat([(t, "Games") for t in titles]))
    return buf.getvalue()


async def test_a_redump_download_keeps_the_earlier_titles_of_a_system_that_failed(monkeypatch, store):
    lists = {"psx": ["Wipeout (Europe)"], "ss": ["Nights (Japan)"]}

    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        code = url.rstrip("/").rsplit("/", 1)[-1]
        assert deadline is not None and max_bytes == gt._MAX_REDUMP
        if code not in lists:
            raise OSError("down")
        dest.write_bytes(_redump_zip(lists[code]))
        return None
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    gt.start_download("redump")
    job = await _wait("redump")
    assert job["state"] == "done" and "except" in job["message"]
    assert gt.status()["downloads"]["redump"]["counts"] == {"psx": 1, "saturn": 1}
    # the next time the Saturn list is unreachable: its titles stay
    lists = {"psx": ["Wipeout (Europe)", "Tekken (Europe)"]}
    gt.start_download("redump")
    job = await _wait("redump")
    assert job["state"] == "done" and "Saturn" in job["message"]
    meta = gt._read_meta("redump")
    assert meta["counts"] == {"psx": 2, "saturn": 1} and "ss" in meta["systems_failed"]
    assert not list(gt._download_dir().glob("dl-*")) and not list(gt._download_dir().glob(".*part"))


async def test_a_redump_download_with_every_system_down_is_an_error(monkeypatch, store):
    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        raise OSError("down")
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    gt.start_download("redump")
    job = await _wait("redump")
    assert job["state"] == "error" and "no Redump list" in job["message"]


async def test_a_failed_download_reports_why_and_a_running_one_cannot_be_removed(monkeypatch, store):
    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        raise OSError("tosecdev.org unreachable")
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    gt._jobs["tosec"] = {"state": "downloading"}
    with pytest.raises(gt.DownloadRunning):
        gt.remove_download("tosec")
    gt._jobs.clear()
    gt.start_download("tosec")
    job = await _wait("tosec")
    assert job["state"] == "error" and "unreachable" in job["message"]
    with pytest.raises(ValueError):
        gt.start_download("nope")
    with pytest.raises(ValueError):
        gt.remove_download("nope")


class _Resp:
    def __init__(self, body, length=None):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_caps_the_size_and_the_time(tmp_path, monkeypatch):
    body = b"x" * (600 * 1024)
    monkeypatch.setattr(gt.urllib.request, "urlopen", lambda req, timeout: _Resp(body))
    assert gt._fetch("http://h/") == body
    got = []
    assert gt._fetch("http://h/", tmp_path / "f", progress=lambda g, t: got.append((g, t))) is None
    assert (tmp_path / "f").read_bytes() == body and got[-1] == (len(body), len(body))
    monkeypatch.setattr(gt, "_MAX_DOWNLOAD", 1000)
    with pytest.raises(ValueError):                  # announced too large
        gt._fetch("http://h/", tmp_path / "g")
    monkeypatch.setattr(gt.urllib.request, "urlopen", lambda req, timeout: _Resp(body, length=0))
    with pytest.raises(ValueError):                  # grows too large
        gt._fetch("http://h/", tmp_path / "g")
    with pytest.raises(ValueError):
        gt._fetch("http://h/")
    monkeypatch.setattr(gt, "_MAX_DOWNLOAD", 10**9)
    with pytest.raises(TimeoutError):
        gt._fetch("http://h/", tmp_path / "h", deadline=time.monotonic() - 1)


async def test_the_admin_endpoints(store, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.api import admin
    st = await admin.admin_game_titles_status(_tok="x")
    assert set(st["bundled"]) == {"wikidata", "nointro", "mame", "zxdb"}
    with pytest.raises(HTTPException) as e:
        await admin.admin_game_titles_download("nope", _tok="x")
    assert e.value.status_code == 404
    gt._jobs["redump"] = {"state": "downloading"}
    with pytest.raises(HTTPException) as e:
        await admin.admin_game_titles_remove("redump", _tok="x")
    assert e.value.status_code == 409


async def test_tosec_picks_releases_by_date_and_tries_the_three_newest(tmp_path, monkeypatch, store):
    pack = tmp_path / "pack.zip"
    _tosec_zip(pack)
    home = (b'<a href="/downloads/category/70-2019-01-01">oldest, highest id</a>'
            b'<a href="/downloads/category/55-2025-03-13">newest</a>'
            b'<a href="/downloads/category/56-2024-05-17">second</a>'
            b'<a href="/downloads/category/57-2023-02-02">third</a>')
    with_pack = {"57-2023-02-02"}
    seen = []

    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        seen.append(url)
        assert deadline is None or deadline > time.monotonic()
        if url == gt._TOSEC_HOME:
            return home
        rel = url.rsplit("/", 1)[-1].split("?")[0]
        if "download=" not in url:
            if rel in with_pack:
                return f'<a href="/downloads/category/{rel}?download=1:tosec-dat-pack-complete-1">p</a>'.encode()
            return b"<p>no pack yet</p>"
        dest.write_bytes(pack.read_bytes())
        return None
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    gt.start_download("tosec")
    assert (await _wait("tosec"))["state"] == "done"
    assert [u.rsplit("/", 1)[-1] for u in seen[1:4]] == ["55-2025-03-13", "56-2024-05-17", "57-2023-02-02"]
    assert gt._read_meta("tosec")["release"] == "2023-02-02"
    with_pack.clear()
    with_pack.add("70-2019-01-01")                    # only the fourth newest has a pack
    seen.clear()
    gt.start_download("tosec")
    job = await _wait("tosec")
    # no newer pack found: the kept one is used, and says why
    assert job["state"] == "done" and "no DAT pack" in job["message"] and "2023-02-02" in job["message"]
    assert not any("70-2019" in u for u in seen)
    gt.delete_files("tosec")
    gt.start_download("tosec")
    job = await _wait("tosec")
    assert job["state"] == "error" and "no DAT pack" in job["message"]


def test_a_written_list_leaves_out_empty_platforms():
    counts = gt._write_list("redump", {"amiga": set(), "arcade": {" "}, "psx": {"Wipeout", "Wipeout "}},
                            {"source": "x"})
    assert counts == {"psx": 1}
    assert gt._read_list("redump") == {"psx": {"Wipeout"}}
    assert gt._read_meta("redump")["titles"] == 1


async def test_an_archive_member_played_before_a_scan_gets_its_game(tmp_path, store, small_index,
                                                                    monkeypatch):
    import uuid
    from soniqboom.api import stream
    from soniqboom.core import data
    z = tmp_path / "Turrican.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.hip", b"x" * 64)
        zf.writestr("b.hip", b"y" * 64)
    path = f"{z}::a.hip"
    tid = str(uuid.uuid5(uuid.NAMESPACE_URL, path))

    async def dirs():
        return [{"path": str(tmp_path)}]
    monkeypatch.setattr(data, "list_scan_dirs", dirs)
    monkeypatch.setattr("soniqboom.core.scanner._extract_from_zip",
                        lambda p, t: __import__("soniqboom.models.track", fromlist=["TrackMeta"])
                        .TrackMeta(id=t, path=p, title="a", format="Jochen Hippel"))
    calls = []
    monkeypatch.setattr(gt, "schedule", lambda: calls.append(1))
    track = await stream._ingest_on_demand(tid, path)
    assert track is not None and store.get_track(tid) is not None
    assert calls == [1]
    loose = tmp_path / "loose.hip"
    loose.write_bytes(b"z" * 64)
    monkeypatch.setattr("soniqboom.core.metadata.extract",
                        lambda p, t: __import__("soniqboom.models.track", fromlist=["TrackMeta"])
                        .TrackMeta(id=t, path=str(p), title="l", format="Jochen Hippel"))
    assert await stream._ingest_on_demand(str(uuid.uuid5(uuid.NAMESPACE_URL, str(loose))), str(loose))
    assert calls == [1]                               # not in an archive: no pass


# ── a Modland file-name guess in the lists' spelling ──────────────────────────

@pytest.fixture
def spelling_index():
    return _index([
        ("wikidata", "amiga", "Back to the Future III"), ("mame", "amiga", "Mortal Kombat II"),
        ("tosec", "amiga", "KGB"), ("wikidata", "amiga", "Dragonstone"),
        ("wikidata", "c64", "Last Ninja 2"),
        ("tosec", "amiga", "Chuck Rock II"), ("wikidata", "amiga", "Chuck-Rock II"),
        ("tosec", "amiga", "Galactic Warrior Rats"), ("wikidata", "amiga", "GalacticWarrior Rats"),
    ])


@pytest.mark.parametrize("name, fmt, want", [
    ("Backtothefuture3", "ProTracker", "Back to the Future III"),     # spaces restored
    ("Mortal Kombat 2", "ProTracker", "Mortal Kombat II"),            # Roman numerals
    ("Kgb", "Jochen Hippel", "KGB"),                                  # case
    ("Chuckrock 2", "ProTracker", "Chuck-Rock II"),                   # the first list's spelling
    ("Galacticwarriorrats", "ProTracker", "GalacticWarrior Rats"),
    ("Galactic Warrior Rats", "ProTracker", "Galactic Warrior Rats"),  # the same title first
    ("Galactic Warriorrats", "ProTracker", "GalacticWarrior Rats"),
    ("Dragon Stone", "ProTracker", None),                             # the list joins its words
    ("Lastninja2", "ProTracker", None),                               # a C64 title only
    ("Lastninja2", "SID", "Last Ninja 2"),
    ("Backtothefuture3", "MP3", None),                                # no retro platform
    ("", "ProTracker", None), ("Hq2", "ProTracker", None),
])
def test_a_name_takes_the_lists_spelling(spelling_index, name, fmt, want):
    assert gt.list_spelling(name, fmt, spelling_index) == want


def test_the_squashed_table_is_built_once(spelling_index):
    first = spelling_index.squashed()
    assert spelling_index.squashed() is first
    assert first["amiga"]["galacticwarriorrats"] == (
        ("GalacticWarrior Rats", 2, "galacticwarrior rats"),
        ("Galactic Warrior Rats", 3, "galactic warrior rats"))


def _modland_index(tmp_path, rows):
    import sqlite3
    db = tmp_path / "modland.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.executemany("INSERT INTO mods VALUES (?,?)", rows)
    con.commit()
    con.close()
    return db


@pytest.fixture
def modland(store, tmp_path, monkeypatch):
    from soniqboom.core import scene_metadata as sm
    db = _modland_index(tmp_path, [
        ("a" * 32, "David Whittaker/David Whittaker/backtothefuture3-intro.dw"),
        ("b" * 32, "David Whittaker/David Whittaker/Back To The Future 3/title.dw"),
    ])
    monkeypatch.setattr(sm, "_db_path", lambda: db)
    monkeypatch.setattr(sm, "_last_auto_sig", None)
    sm._status["applying"] = False
    store.upsert_tracks_batch([
        _t("a", "/m/a.dw", "David Whittaker", title="intro", genre=["Amiga"], file_md5="a" * 32),
        _t("b", "/m/b.dw", "David Whittaker", title="title", genre=["Amiga"], file_md5="b" * 32),
    ])
    return sm


async def test_the_modland_apply_spells_a_file_name_guess_as_the_lists_do(modland, store,
                                                                          spelling_index, monkeypatch):
    monkeypatch.setattr(gt, "get_index", lambda: spelling_index)
    await modland.apply_to_library()
    a, b = store.get_track("a"), store.get_track("b")
    assert (a["game_by_modland_filename"], a["album"], a["album_source"], a["game"]) == (
        "Back to the Future III", "Back to the Future III", "modland-filename", "Back to the Future III")
    # a Modland game FOLDER keeps Modland's own spelling
    assert b["game_by_modland"] == modland.game_album_name("Back To The Future 3")
    assert b["game_by_modland"] != "Back to the Future III"


async def test_a_new_list_lets_the_next_automatic_apply_respell(modland, store, spelling_index,
                                                                monkeypatch):
    monkeypatch.setattr(gt, "get_index", lambda: gt.TitleIndex())          # no list knows it yet
    monkeypatch.setattr(gt, "_lists_sig", lambda: ())
    await modland.apply_to_library(auto=True)
    assert store.get_track("a")["album"] == "Backtothefuture3"
    assert (await modland.apply_to_library(auto=True)).get("skipped") == "unchanged"
    monkeypatch.setattr(gt, "get_index", lambda: spelling_index)            # TOSEC downloaded
    monkeypatch.setattr(gt, "_lists_sig", lambda: (("tosec", "x", 1),))
    res = await modland.apply_to_library(auto=True)
    assert "skipped" not in res
    a = store.get_track("a")
    assert (a["album"], a["game"]) == ("Back to the Future III", "Back to the Future III")
    assert (await modland.apply_to_library(auto=True)).get("skipped") == "unchanged"


def test_a_list_change_reruns_the_scene_enrichment(monkeypatch):
    from soniqboom.core import scanner
    calls = []
    monkeypatch.setattr(gt, "schedule", lambda: calls.append("pass"))
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda: calls.append("scene"))
    gt._lists_changed_unpatched()
    assert calls == ["pass", "scene"]

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", boom)
    gt._lists_changed_unpatched()
    assert calls == ["pass", "scene", "pass"]         # the archive pass runs anyway


# ── round-2 QA fixes ──────────────────────────────────────────────────────────

def test_two_lists_must_agree_on_one_platform():
    idx = _index([("wikidata", "nes", "Riot"), ("mame", "megadrive", "Riot")])
    assert gt.archive_game("/m/Riot.zip::01.vgz", "VGZ", idx) is None     # one list per platform
    idx.add("mame", "nes", "Riot")
    assert gt.archive_game("/m/Riot.zip::01.vgz", "VGZ", idx) == "Riot"
    t, bits, _bt, _b = idx.lookup("riot", ["megadrive", "nes"])
    assert gt._bits(bits) == 2                          # the NES entry, not a union


def test_dots_between_letters_and_missing_spaces_still_match():
    assert gt.title_key("Chase H.Q.") == gt.title_key("Chase HQ") == "chase hq"
    assert gt.title_key("R.C. Pro-Am") == "rc pro am"
    assert gt.title_key("Rock n Roll") == "rock and roll"          # a lone letter stays itself
    idx = _index([("wikidata", "amiga", "Mega Man 2"), ("wikidata", "amiga", "Last Ninja 2"),
                  ("wikidata", "amiga", "Dragonstone")])
    assert gt.archive_game("/m/Megaman2.zip::a.hip", "Jochen Hippel", idx) == "Mega Man 2"
    assert gt.archive_game("/m/LastNinja2.zip::a.hip", "Jochen Hippel", idx) == "Last Ninja 2"
    # a list title that joins the name's words does not match it
    assert gt.archive_game("/m/Dragon Stone.zip::a.hip", "Jochen Hippel", idx) is None


async def test_status_while_a_pass_is_due_does_not_rebuild_the_index(monkeypatch):
    built = []
    monkeypatch.setattr(gt, "get_index", lambda: built.append(1) or gt.TitleIndex())
    monkeypatch.setattr(gt, "_index", None)
    monkeypatch.setattr(gt, "_pending", True)
    st = gt.status()
    assert st["pass_busy"] is True and st["titles"] is None and built == []
    monkeypatch.setattr(gt, "_pending", False)
    st = gt.status()
    assert st["pass_busy"] is False and st["titles"] == 0 and built == [1]


async def test_the_counts_include_only_names_that_are_shown(store, small_index):
    store.upsert_tracks_batch([
        _t("a", "/m/Turrican.lha::a.hip"),                                   # the game
        _t("b", "/m/Turrican.lha::b.hip", album="Street Racer", album_source="songdb"),   # an alias
        _t("c", "/m/Turrican.lha::c.hip", album="Turrican II", album_source="songdb"),    # dropped
        _t("d", "/m/Turrican.lha::d.hip", game="", user_edited=["game"]),     # cleared by the user
    ])
    res = await gt.apply_archive_games(force=True)
    assert store.get_track("b")["game_aliases"] == ["Turrican"]
    assert not store.get_track("c").get("game_aliases")
    assert (res["named"], res["primary"]) == (2, 1)


def test_the_post_scan_and_start_up_hooks_schedule_the_pass(monkeypatch):
    from soniqboom.core import folder_album, scanner
    calls = []
    monkeypatch.setattr(gt, "schedule", lambda: calls.append("gt"))
    monkeypatch.setattr(folder_album, "schedule_after_scan", lambda: calls.append("fa"))
    scanner._schedule_folder_album_pass()
    assert calls == ["fa", "gt"]
    calls.clear()
    monkeypatch.setattr(scanner, "_ensure_scene_autoapply_runner", lambda: calls.append("scene"))
    scanner.schedule_startup_enrichment()
    assert calls[0] == "gt"


async def test_a_download_or_removal_reports_the_list_change(tmp_path, monkeypatch, store):
    changed = []
    monkeypatch.setattr(gt, "_lists_changed", lambda: changed.append(1))

    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        dest.write_bytes(_redump_zip(["Wipeout (Europe)"]))
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    gt.start_download("redump")
    assert (await _wait("redump"))["state"] == "done" and changed == [1]
    assert gt.remove_download("redump") is True and changed == [1, 1]
    assert gt.remove_download("redump") is False and changed == [1, 1]   # nothing was there


def test_leftovers_of_an_interrupted_download_are_removed():
    import os
    d = gt._download_dir()
    (d / "dl-tosec-abc").mkdir()
    (d / "dl-tosec-abc" / "tosec.zip").write_bytes(b"x" * 100)
    (d / ".tosec.tsv.gz.part").write_bytes(b"x")
    (d / "dl-redump-x").mkdir()
    (d / "dl-redump-now").mkdir()                  # this process's download: newer than its start
    old = gt._PROCESS_START - 3600
    for n in ("dl-tosec-abc", ".tosec.tsv.gz.part", "dl-redump-x"):
        os.utime(d / n, (old, old))
    gt.status()                                    # a status poll never removes (a download may start)
    assert (d / "dl-tosec-abc").exists()
    gt._jobs["redump"] = {"state": "downloading"}  # a running download is skipped
    gt.clean_leftovers()                           # at start-up
    assert not (d / "dl-tosec-abc").exists() and not (d / ".tosec.tsv.gz.part").exists()
    assert (d / "dl-redump-x").exists()
    gt._jobs.clear()
    gt.clean_leftovers()
    assert not (d / "dl-redump-x").exists() and (d / "dl-redump-now").exists()
    (d / "dl-tosec-def").mkdir()
    gt.remove_download("tosec")
    assert not (d / "dl-tosec-def").exists()


class _Slow(_Resp):
    def read(self, n=-1):
        time.sleep(0.02)
        return b"x" * 16


def test_fetching_a_page_has_a_deadline_too(monkeypatch):
    monkeypatch.setattr(gt.urllib.request, "urlopen", lambda req, timeout: _Slow(b"", length=0))
    with pytest.raises(TimeoutError):
        gt._fetch("http://h/", deadline=time.monotonic() + 0.1)


def test_dat_entries_are_read_in_linear_time():
    data = ('<datafile><game name="A (USA)"><category>Games</category></game>'
            '<game  cloneof="x" name="B &amp; C"><description>d</description></game>\n'
            '<game\n name="D"><rom/></game><game name="E">unclosed')
    assert list(dat.iter_games(data)) == [
        ("A (USA)", "<category>Games</category>"), ("B &amp; C", "<description>d</description>"),
        ("D", "<rom/>"), ("E", "unclosed")]
    hostile = '<game name="x">' * 200_000                      # 3 MB of unclosed entries
    t0 = time.perf_counter()
    assert sum(1 for _ in dat.iter_games(hostile)) == 200_000
    assert time.perf_counter() - t0 < 2.0
    t0 = time.perf_counter()
    assert list(dat.iter_games('<game name="' + "x" * 3_000_000)) == []
    assert time.perf_counter() - t0 < 2.0


def test_settings_switches_parse_text_as_well():
    from soniqboom.api.admin import _flag
    assert [_flag(v) for v in (True, 1, "true", "On", "yes", "1")] == [True] * 6
    assert [_flag(v) for v in (False, 0, None, "", "false", "off", "no", "0")] == [False] * 8


async def test_a_non_retro_archive_member_played_before_a_scan_runs_no_pass(tmp_path, store, monkeypatch):
    import uuid
    from soniqboom.api import stream
    from soniqboom.core import data
    from soniqboom.models.track import TrackMeta
    z = tmp_path / "Album.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("01.mp3", b"x" * 64)

    async def dirs():
        return [{"path": str(tmp_path)}]
    monkeypatch.setattr(data, "list_scan_dirs", dirs)
    monkeypatch.setattr("soniqboom.core.scanner._extract_from_zip",
                        lambda p, t: TrackMeta(id=t, path=p, title="01", format="MP3"))
    calls = []
    monkeypatch.setattr(gt, "schedule", lambda: calls.append(1))
    path = f"{z}::01.mp3"
    assert await stream._ingest_on_demand(str(uuid.uuid5(uuid.NAMESPACE_URL, path)), path)
    assert calls == []


# ── round-3 QA ────────────────────────────────────────────────────────────────

def test_the_lists_exact_title_comes_before_a_squashed_one():
    idx = _index([("wikidata", "amiga", "Gear Works"), ("tosec", "amiga", "Gearworks")])
    assert gt.list_spelling("Gearworks", "ProTracker", idx) == "Gearworks"
    assert gt.list_spelling("gear-works", "ProTracker", idx) == "Gear Works"


def test_a_squashed_archive_name_matches_only_a_title_with_more_words():
    idx = _index([("wikidata", "amiga", "Dragon Stone"), ("wikidata", "amiga", "Mega Man 2"),
                  ("tosec", "amiga", "Me Ga Man 2"), ("mame", "amiga", "Me Ga Man 2")])
    assert gt.archive_game("/m/Dragonst One.zip::a.hip", "Jochen Hippel", idx) is None   # same words, split elsewhere
    # the first list's title wins even if a later one has more sources
    assert gt.archive_game("/m/Megaman2.zip::a.hip", "Jochen Hippel", idx) == "Mega Man 2"


def test_a_long_or_hostile_name_is_cut_or_skipped():
    assert gt.display_title("Red Baron (Europe, v1.0) [a]") == "Red Baron"
    assert gt.display_title("(Europe)") == "(Europe)"
    assert gt.display_title("Foo (a (b))") == "Foo (a"
    t0 = time.perf_counter()
    for s in ("A" + "(" * 100_000, "A" + " " * 100_000 + "B", "A" + "( " * 50_000, "A" + ")" * 100_000):
        gt.title_key(s), gt.display_title(s), gt.base_key(s)
        dat.nointro_title(s), dat.tosec_title(s)
        dat.emit(collections.defaultdict(set), "psx", s)
    assert time.perf_counter() - t0 < 1.0
    assert dat._name("  Wipeout   (Europe) ") == "Wipeout (Europe)"
    assert dat._name("x" * 301) is None and dat._name("   ") is None
    store = collections.defaultdict(set)
    dat.emit(store, "psx", "x" * 301)
    assert not store


def test_a_list_line_with_a_hostile_title_is_skipped(tmp_path):
    p = tmp_path / "x.tsv.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("amiga\tTurrican\namiga\t" + "(" * 5000 + "\n")
    idx = gt.TitleIndex()
    assert idx.load_tsv("tosec", p) == 1


def test_an_oversized_zip_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "psx.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.dat", _dat([("Wipeout (Europe)", "Games")]))
        z.writestr("b.dat", _dat([("Tekken (Europe)", "Games")]))
    monkeypatch.setattr(dat, "MAX_ZIP_MEMBERS", 1)
    with pytest.raises(ValueError):
        dat.titles_from_redump_zip(str(p), "psx")
    monkeypatch.setattr(dat, "MAX_ZIP_MEMBERS", 50_000)
    monkeypatch.setattr(dat, "MAX_ZIP_BYTES", 10)
    with pytest.raises(ValueError):
        dat.titles_from_tosec_pack(str(p))


class _Trickle(_Resp):
    def read1(self, n=-1):
        time.sleep(0.05)
        return b"x"


def test_a_trickling_server_cannot_hold_a_download_past_its_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(gt.urllib.request, "urlopen", lambda req, timeout: _Trickle(b"", length=0))
    t0 = time.perf_counter()
    with pytest.raises(TimeoutError):
        gt._fetch("http://h/", tmp_path / "f", deadline=time.monotonic() + 0.3)
    assert time.perf_counter() - t0 < 1.0


async def test_status_says_when_the_pass_waits_for_a_scan(monkeypatch):
    from soniqboom.core import scanner
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: True)
    monkeypatch.setattr(gt, "_SCAN_POLL_S", 0.01)
    monkeypatch.setattr(gt, "_index", gt.TitleIndex())

    async def fake_apply(force=False):
        return {}
    monkeypatch.setattr(gt, "apply_archive_games", fake_apply)
    gt.schedule()
    await asyncio.sleep(0.05)
    st = gt.status()
    assert st["pass_busy"] and st["pass_waiting_for_scan"]
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: False)
    await _drain()
    assert gt.status()["pass_waiting_for_scan"] is False


def test_the_modland_file_name_guess_ranks_above_the_archive_name():
    from soniqboom.core.store import game_follow
    t = {"format": "Jochen Hippel", "game_by_modland_filename": "Turrican", "game_by_archive": "Gods"}
    g = game_follow(t)
    assert (g["game"], g["game_source"], g["game_aliases"]) == ("Turrican", "modland-filename", ["Gods"])
    t = {"format": "Jochen Hippel", "game_by_songdb": "Gods", "game_by_demozoo": "Turrican"}
    g = game_follow(t)
    assert (g["game"], g["game_source"]) == ("Turrican", "demozoo")


def test_the_modland_apply_version_was_raised_for_the_list_spelling():
    from soniqboom.core import scene_metadata as sm
    assert sm.MODLAND_APPLY_VERSION >= 7


# ── round-4 QA ────────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_the_worker_forkserver_is_started_and_named_for_the_reaper():
    import subprocess
    from multiprocessing import forkserver
    from soniqboom.core import scanner
    try:
        forkserver._forkserver._stop()                 # the one another test may have started
    except Exception:                                  # noqa: BLE001
        pass
    scanner.prepare_worker_forkserver()
    pid = forkserver._forkserver._forkserver_pid
    assert pid
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    assert "multiprocessing.forkserver" in cmd and "'soniqboom'" in cmd


def test_start_up_removes_download_leftovers_before_serving():
    import inspect
    from soniqboom import main
    src = inspect.getsource(main.startup)
    assert "begin_run()" in src and "clean_leftovers()" in src
    assert "clean_leftovers" not in inspect.getsource(
        __import__("soniqboom.core.scanner", fromlist=["x"]).schedule_startup_enrichment)


def test_only_the_innermost_archives_are_looked_at():
    idx = _index([("wikidata", "amiga", "Turrican"), ("mame", "amiga", "Turrican")])
    inner = "::".join(f"pack{i}.zip" for i in range(8))
    assert gt.archive_game(f"/m/Turrican.zip::{inner}::a.hip", "Jochen Hippel", idx) is None
    inner = "::".join(f"pack{i}.zip" for i in range(7))
    assert gt.archive_game(f"/m/Turrican.zip::{inner}::a.hip", "Jochen Hippel", idx) == "Turrican"


def test_very_long_names_are_cut_before_the_patterns():
    name = "Turrican " + "x" * 600
    assert gt.title_key(name) == gt.title_key(name[:gt._MAX_KEYED])
    assert len(gt.display_title("a" * 5000)) <= gt._MAX_KEYED


def test_a_written_list_skips_titles_no_list_would_have():
    counts = gt._write_list("tosec", {"amiga": {"Turrican", "y" * 301}}, {})
    assert counts == {"amiga": 1} and gt._read_list("tosec") == {"amiga": {"Turrican"}}


def test_the_socket_timeout_never_outlasts_the_deadline(monkeypatch):
    seen = []

    def fake_open(req, timeout):
        seen.append(timeout)
        return _Resp(b"ok")
    monkeypatch.setattr(gt.urllib.request, "urlopen", fake_open)
    gt._fetch("http://h/", timeout=300, deadline=time.monotonic() + 5)
    gt._fetch("http://h/", timeout=300)
    assert seen[0] <= 5 and seen[1] == gt._CONNECT_S           # to connect; the reads get 300


def test_the_demozoo_index_path_may_hold_any_character(tmp_path, monkeypatch):
    import sqlite3
    from soniqboom.core import demozoo
    d = tmp_path / "a #b %20 ü?"
    d.mkdir()
    db = d / "demozoo.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE music_game (releaser_id INTEGER, ptoks TEXT, game TEXT)")
    con.execute("INSERT INTO music_game VALUES (1, 'a', 'b')")
    con.commit()
    con.close()
    assert demozoo._game_rows(db) == 1


def test_dat_names_are_decoded():
    assert dat._name("Tom &amp; Jerry &#8211; The Movie") == "Tom & Jerry – The Movie"


async def test_text_switches_are_parsed_everywhere(store, monkeypatch):
    from soniqboom.api import admin
    from soniqboom.core import repair
    seen = {}

    def cand(include_remote=True):
        seen["albums"] = include_remote
        return []

    async def tags(include_remote=True):
        seen["tags"] = include_remote
        return []

    async def offline(_store):
        return set()
    monkeypatch.setattr(repair, "find_album_backfill_candidates", cand)
    monkeypatch.setattr(repair, "find_game_tag_candidates", tags)
    monkeypatch.setattr(repair, "_offline_local_roots", offline)
    monkeypatch.setattr(repair, "is_running", lambda: False)
    await admin.metadata_backfill_game_albums({"include_remote": "false"}, _tok="x")
    assert seen == {"albums": False, "tags": False}
    import soniqboom.config as cfg
    got = []
    monkeypatch.setattr(cfg, "set_service_enabled", lambda name, on: got.append(on))
    await admin.admin_services_set("subsonic", {"enabled": "false"}, _tok="x")
    assert got == [False]
    before = cfg.load_local_conf().get("renderers", {}).get("sid_filter", True)
    try:
        await admin.update_settings({"renderers": {"sid_filter": "false"}}, _tok="x")
        assert cfg.load_local_conf()["renderers"]["sid_filter"] is False
    finally:
        await admin.update_settings({"renderers": {"sid_filter": before}}, _tok="x")


def test_a_partial_redump_download_says_what_was_kept(monkeypatch):
    job = {}
    lists = {"psx": ["Wipeout (Europe)"]}

    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        code = url.rstrip("/").rsplit("/", 1)[-1]
        if code not in lists:
            raise OSError("down")
        dest.write_bytes(_redump_zip(lists[code]))
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    gt._download_redump(job)                          # nothing earlier to keep
    assert "kept" not in job["message"] and "Update again later" in job["message"]
    meta = gt._read_meta("redump")
    assert "ss" in meta["systems_failed"] and meta["systems_kept"] == []
    lists = {"psx": ["Wipeout (Europe)"], "ss": ["Nights (Japan)"]}
    gt._download_redump(job)
    lists = {"psx": ["Wipeout (Europe)"]}
    gt._download_redump(job)                          # Saturn down now, its titles kept
    assert "the earlier titles of Saturn are kept" in job["message"] and " ss" not in job["message"]
    assert gt._read_meta("redump")["systems_kept"] == ["ss"]


def test_a_tosec_pack_link_off_the_site_is_refused(monkeypatch):
    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        if url == gt._TOSEC_HOME:
            return b'<a href="/downloads/category/59-2025-03-13">new</a>'
        return b'<a href="http://evil.example/x?download=1:tosec-dat-pack-complete-1">p</a>'
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="elsewhere"):
        gt._download_tosec({})


# ── round-5 QA ────────────────────────────────────────────────────────────────

def test_start_up_calls_the_download_cleanup():
    import ast
    import inspect
    import textwrap
    from soniqboom import main
    tree = ast.parse(textwrap.dedent(inspect.getsource(main.startup)))
    calls = {getattr(n.func, "attr", getattr(n.func, "id", None))
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert {"clean_leftovers", "begin_run"} <= calls


def test_a_look_alike_tosec_host_is_refused(monkeypatch):
    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        if url == gt._TOSEC_HOME:
            return b'<a href="/downloads/category/59-2025-03-13">new</a>'
        return b'<a href="https://www.tosecdev.org.evil/x?download=1:tosec-dat-pack-complete-1">p</a>'
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="elsewhere"):
        gt._download_tosec({})


def test_a_redirect_off_the_site_is_refused(monkeypatch):
    class _Moved(_Resp):
        def geturl(self):
            return "https://evil.example/pack.zip"
    monkeypatch.setattr(gt.urllib.request, "urlopen", lambda req, timeout: _Moved(b"x"))
    with pytest.raises(ValueError, match="redirected"):
        gt._fetch("https://www.tosecdev.org/x", site="https://www.tosecdev.org/")
    assert gt._fetch("https://www.tosecdev.org/x") == b"x"               # no site given: allowed


async def test_an_interrupted_download_does_not_stay_downloading(monkeypatch, store):
    """The event loop closes (the app stops) with a download running: its job
    ends as "interrupted" at once, and stays so after its thread has ended."""
    import threading
    gate = threading.Event()

    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        gate.wait(5)
        raise OSError("stopped")
    monkeypatch.setattr(gt, "_fetch", fake_fetch)
    gt.start_download("redump")
    await asyncio.sleep(0.05)
    task = gt._job_tasks["redump"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gt._jobs["redump"]["state"] == "interrupted"
    gate.set()
    assert await asyncio.to_thread(gt.wait_for_downloads, 5)
    job = gt._jobs["redump"]
    assert job["state"] == "interrupted" and "stopped" in job["message"]
    assert not (gt._download_dir() / "redump.tsv.gz").exists()
    assert gt.remove_download("redump") is False                        # not 409




# ── kept downloads, stopping ──────────────────────────────────────────────────

async def test_a_removed_tosec_list_is_added_again_from_the_kept_pack(tmp_path, monkeypatch, store):
    pack = tmp_path / "pack.zip"
    _tosec_zip(pack)
    seen = []
    monkeypatch.setattr(gt, "_fetch", _tosec_fetch(pack, seen))
    gt.start_download("tosec")
    assert (await _wait("tosec"))["state"] == "done"
    assert gt.remove_download("tosec") is True
    assert gt.status()["downloads"]["tosec"]["files"]["release"] == "2025-03-13"   # kept
    seen.clear()
    gt.start_download("tosec", local=True)
    job = await _wait("tosec")
    assert job["state"] == "done" and seen == []                                  # no network
    assert "kept TOSEC release of 2025-03-13" in job["message"]
    assert gt._read_list("tosec") == {"c64": {"Last Ninja 2"}}


async def test_a_tosec_update_downloads_the_pack_only_for_a_newer_release(tmp_path, monkeypatch, store):
    pack = tmp_path / "pack.zip"
    _tosec_zip(pack)
    seen = []
    monkeypatch.setattr(gt, "_fetch", _tosec_fetch(pack, seen))
    gt.start_download("tosec")
    await _wait("tosec")
    seen.clear()
    mtime = (gt._download_dir() / "tosec.tsv.gz").stat().st_mtime_ns
    gt.start_download("tosec")                        # the same release: nothing to do
    job = await _wait("tosec")
    assert job["state"] == "done" and "up to date" in job["message"] and job["changed"] is False
    assert not any("download=" in u for u in seen) and len(seen) == 2              # two pages only
    assert (gt._download_dir() / "tosec.tsv.gz").stat().st_mtime_ns == mtime       # not rebuilt
    # a newer release: its pack is downloaded, the older one dropped
    meta = gt._read_sources_meta("tosec")
    older = gt._sources_dir("tosec") / "tosec-dat-pack-2024-01-01.zip"
    older.write_bytes(pack.read_bytes())
    gt._write_sources_meta("tosec", {**meta, "release": "2024-01-01", "file": older.name})
    (gt._sources_dir("tosec") / "tosec-dat-pack-2025-03-13.zip").unlink()
    seen.clear()
    gt.start_download("tosec")
    job = await _wait("tosec")
    assert job["state"] == "done" and job["message"] == "Downloaded the TOSEC release of 2025-03-13"
    assert sorted(p.name for p in gt._sources_dir("tosec").iterdir()) == [
        "files.json", "tosec-dat-pack-2025-03-13.zip"]


async def test_an_unreachable_site_uses_the_kept_tosec_pack(tmp_path, monkeypatch, store):
    pack = tmp_path / "pack.zip"
    _tosec_zip(pack)
    monkeypatch.setattr(gt, "_fetch", _tosec_fetch(pack, []))
    gt.start_download("tosec")
    await _wait("tosec")

    def offline(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        raise OSError("no route to host")
    monkeypatch.setattr(gt, "_fetch", offline)
    gt.start_download("tosec")
    job = await _wait("tosec")
    assert job["state"] == "done" and "no route to host" in job["message"] and "2025-03-13" in job["message"]
    gt.delete_files("tosec")                          # nothing kept: the failure is reported
    gt.start_download("tosec")
    job = await _wait("tosec")
    assert job["state"] == "error" and "no route to host" in job["message"]


async def test_a_damaged_kept_or_downloaded_pack_is_never_used(tmp_path, monkeypatch, store):
    pack = tmp_path / "pack.zip"
    _tosec_zip(pack)
    monkeypatch.setattr(gt, "_fetch", _tosec_fetch(pack, []))
    gt.start_download("tosec")
    await _wait("tosec")
    kept = gt._sources_dir("tosec") / "tosec-dat-pack-2025-03-13.zip"
    kept.write_bytes(b"x" * kept.stat().st_size)      # same size, not a zip
    gt.start_download("tosec", local=True)
    job = await _wait("tosec")
    assert job["state"] == "error" and "can't be used" in job["message"]
    assert gt.files_info("tosec") is None and not kept.exists()
    assert gt._read_list("tosec") == {"c64": {"Last Ninja 2"}}                     # the list stays
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    monkeypatch.setattr(gt, "_fetch", _tosec_fetch(bad, []))
    gt.start_download("tosec")
    job = await _wait("tosec")
    assert job["state"] == "error" and "can't be read" in job["message"]
    assert list(gt._sources_dir("tosec").glob("*")) == []                          # nothing half-kept


async def test_a_kept_pack_that_cannot_be_read_for_a_reason_other_than_damage_is_kept(tmp_path, monkeypatch, store):
    """Only a bad file is deleted (not a zip, damaged, no lists in it): an I/O
    error, a permission or a volume briefly gone leaves the pack kept."""
    pack = tmp_path / "pack.zip"
    _tosec_zip(pack)
    monkeypatch.setattr(gt, "_fetch", _tosec_fetch(pack, []))
    gt.start_download("tosec")
    await _wait("tosec")
    kept = gt._sources_dir("tosec") / "tosec-dat-pack-2025-03-13.zip"
    info = gt.files_info("tosec")
    real = dat.titles_from_tosec_pack
    for exc in (OSError(5, "Input/output error"), PermissionError(13, "Permission denied"), MemoryError()):
        def failing(path, exc=exc):
            raise exc
        monkeypatch.setattr(dat, "titles_from_tosec_pack", failing)
        gt.start_download("tosec", local=True)
        job = await _wait("tosec")
        assert job["state"] == "error" and "could not be read" in job["message"] and "it is kept" in job["message"]
        assert kept.exists() and gt.files_info("tosec") == info
    monkeypatch.setattr(dat, "titles_from_tosec_pack", real)
    gt.start_download("tosec", local=True)                # readable again: added from it
    job = await _wait("tosec")
    assert job["state"] == "done" and "kept TOSEC release" in job["message"]
    empty = tmp_path / "empty.zip"                         # a zip with no games lists: a bad file
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("TOSEC/readme.txt", "x")
    shutil.copyfile(empty, kept)
    meta = gt._read_sources_meta("tosec")
    gt._write_sources_meta("tosec", {**meta, "size": kept.stat().st_size})
    gt.start_download("tosec", local=True)
    job = await _wait("tosec")
    assert job["state"] == "error" and "can't be used" in job["message"] and not kept.exists()


async def test_a_kept_redump_list_is_deleted_only_when_it_is_bad(monkeypatch, store):
    lists = {c: [f"Game {c} (Europe)"] for c in dat.REDUMP_SYSTEMS}
    monkeypatch.setattr(gt, "_fetch", _redump_fetch(lists, []))
    gt.start_download("redump")
    await _wait("redump")
    src = gt._sources_dir("redump")
    (src / "psx.zip").write_bytes(b"x" * (src / "psx.zip").stat().st_size)   # damaged, same size
    real = dat.titles_from_redump_zip

    def reading(path, plat):
        if path.endswith("cdtv.zip"):
            raise OSError(5, "Input/output error")        # the disk, not the file
        return real(path, plat)
    monkeypatch.setattr(dat, "titles_from_redump_zip", reading)
    gt.start_download("redump", local=True)
    job = await _wait("redump")
    assert job["state"] == "done"
    assert (src / "cdtv.zip").exists() and "cdtv" in gt._read_sources_meta("redump")["systems"]
    assert not (src / "psx.zip").exists() and "psx" not in gt._read_sources_meta("redump")["systems"]
    meta = gt._read_meta("redump")
    assert sorted(meta["systems_failed"]) == ["cdtv", "psx"]
    listed = gt._read_list("redump")
    assert "Game cdtv" in listed[dat.REDUMP_SYSTEMS["cdtv"]]      # its earlier titles are used
    monkeypatch.setattr(dat, "titles_from_redump_zip", real)
    gt.start_download("redump", local=True)                        # readable again
    job = await _wait("redump")
    assert "cdtv" not in gt._read_meta("redump")["systems_failed"]


def _redump_fetch(lists, calls, gate=None):
    def fake_fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        code = url.rstrip("/").rsplit("/", 1)[-1]
        calls.append(code)
        if gate is not None:
            gate(code, ctl)
        if code not in lists:
            raise OSError("down")
        dest.write_bytes(_redump_zip(lists[code]))
    return fake_fetch


async def test_a_stopped_redump_download_continues_where_it_stopped(monkeypatch, store):
    import threading
    lists = {c: [f"Game {c} (Europe)"] for c in dat.REDUMP_SYSTEMS}
    calls = []
    hold = threading.Event()

    def gate(code, ctl):
        if len(calls) == 4:                       # the fourth system: wait to be stopped
            hold.wait(5)
    monkeypatch.setattr(gt, "_fetch", _redump_fetch(lists, calls, gate))
    gt.start_download("redump")
    for _ in range(200):
        if len(calls) >= 4:
            break
        await asyncio.sleep(0.02)
    assert gt.cancel_download("redump") is True
    assert gt._jobs["redump"]["state"] == "interrupted"
    hold.set()
    assert await asyncio.to_thread(gt.wait_for_downloads, 5)
    assert gt._jobs["redump"]["state"] == "interrupted"
    assert not (gt._download_dir() / "redump.tsv.gz").exists()                     # never written
    info = gt.files_info("redump")
    assert info["systems"] == 3 and info["resumable"] is True
    first = list(calls[:3])
    calls.clear()
    gt.start_download("redump")                       # continues: the three are not fetched again
    job = await _wait("redump")
    assert job["state"] == "done" and job["message"] == "Downloaded"
    assert calls == [c for c in dat.REDUMP_SYSTEMS if c not in first]
    assert gt.files_info("redump")["resumable"] is False
    assert sum(gt._read_meta("redump")["counts"].values()) == len(dat.REDUMP_SYSTEMS)
    calls.clear()
    gt.start_download("redump")                       # a finished one: an Update fetches all again
    await _wait("redump")
    assert calls == list(dat.REDUMP_SYSTEMS)


async def test_a_redump_system_that_fails_keeps_its_kept_file(monkeypatch, store):
    lists = {c: [f"Game {c} (Europe)"] for c in dat.REDUMP_SYSTEMS}
    calls = []
    monkeypatch.setattr(gt, "_fetch", _redump_fetch(lists, calls))
    gt.start_download("redump")
    await _wait("redump")
    del lists["ss"]
    gt.start_download("redump")
    job = await _wait("redump")
    assert job["state"] == "done" and "the earlier titles of Saturn are kept" in job["message"]
    assert gt._read_list("redump")["saturn"] == {"Game ss"}
    meta = gt._read_meta("redump")
    assert meta["systems_failed"] == ["ss"] and meta["systems_kept"] == ["ss"]
    lists.clear()                                     # redump.org unreachable
    gt.start_download("redump")
    job = await _wait("redump")
    assert job["state"] == "done" and "could not be reached" in job["message"]
    assert sum(gt._read_meta("redump")["counts"].values()) == len(dat.REDUMP_SYSTEMS)
    assert gt.remove_download("redump") is True
    calls.clear()
    gt.start_download("redump", local=True)           # added again from the kept files
    job = await _wait("redump")
    assert job["state"] == "done" and calls == [] and "Added from the kept Redump lists" in job["message"]


async def test_deleting_the_kept_files_keeps_the_list_and_is_refused_while_downloading(monkeypatch, store):
    from fastapi import HTTPException
    from soniqboom.api import admin
    lists = {"psx": ["Wipeout (Europe)"]}
    monkeypatch.setattr(gt, "_fetch", _redump_fetch(lists, []))
    gt.start_download("redump")
    await _wait("redump")
    size = gt.files_info("redump")["bytes"]
    meta_size = (gt._sources_dir("redump") / "files.json").stat().st_size
    assert gt.delete_files("redump") == size + meta_size
    assert gt.files_info("redump") is None and not gt._sources_dir("redump").exists()
    assert gt._read_list("redump") == {"psx": {"Wipeout"}}
    gt._jobs["redump"] = {"state": "downloading"}
    with pytest.raises(HTTPException) as e:
        await admin.admin_game_titles_delete_files("redump", _tok="x")
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        await admin.admin_game_titles_delete_files("nope", _tok="x")
    assert e.value.status_code == 404


def test_a_transfer_in_progress_is_cut_off_when_stopped(tmp_path):
    """A read blocked on a server that went silent returns at once when the
    download is stopped (its socket shut down) — not when the socket timeout
    (60 s here) expires."""
    import http.server
    import threading

    class Trickle(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            try:
                self.wfile.write(b"x" * 10)
                self.wfile.flush()
                time.sleep(20)                        # then silent: the read blocks
            except OSError:
                pass

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ctl = gt._Ctl()
        out = {}

        def run():
            t0 = time.monotonic()
            try:
                gt._fetch(f"http://127.0.0.1:{srv.server_port}/x", tmp_path / "f", timeout=60, ctl=ctl)
            except BaseException as exc:          # noqa: BLE001
                out["exc"] = exc
            out["s"] = time.monotonic() - t0
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)
        assert ctl.sock is not None
        ctl.stop()
        t.join(5)
        assert not t.is_alive()
        assert isinstance(out["exc"], gt._Stopped) and out["s"] < 2
    finally:
        srv.shutdown()


async def test_a_stopped_download_that_is_still_ending_writes_nothing(monkeypatch, store):
    """Stopped while stuck, its thread ends only later: a new download runs
    at once meanwhile, and what the old thread fetched afterwards is never
    kept nor listed."""
    import threading
    hold = threading.Event()
    calls = []
    first = {"ok": True}

    def fetch(url, dest=None, progress=None, timeout=120, deadline=None, max_bytes=None, site=None, ctl=None):
        code = url.rstrip("/").rsplit("/", 1)[-1]
        calls.append(code)
        if first["ok"] and code == "psx":
            first["ok"] = False
            hold.wait(5)                              # stuck (the stop can't cut a fake transfer)
            dest.write_bytes(_redump_zip(["Old Game (Europe)"]))
            return None
        if code != "psx":
            raise OSError("down")
        dest.write_bytes(_redump_zip(["New Game (Europe)"]))
    monkeypatch.setattr(gt, "_fetch", fetch)
    gt.start_download("redump")
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.02)
    old_ctl = gt._ctls["redump"]
    assert gt.cancel_download("redump") is True
    gt.start_download("redump")                       # at once, not after the stuck thread
    assert (await _wait("redump"))["state"] == "done"
    assert gt._read_list("redump")["psx"] == {"New Game"}
    hold.set()
    old_ctl.thread.join(5)
    assert not old_ctl.thread.is_alive()
    assert gt._read_list("redump")["psx"] == {"New Game"}                         # untouched
    with zipfile.ZipFile(gt._sources_dir("redump") / "psx.zip") as z:
        assert "New Game" in z.read(z.namelist()[0]).decode()
    assert not list(gt._sources_dir("redump").glob("*.part"))


def test_start_up_keeps_the_kept_files_and_removes_half_downloaded_ones():
    import os
    src = gt._sources_dir("tosec")
    src.mkdir(parents=True)
    (src / "tosec-dat-pack-2025-03-13.zip").write_bytes(b"x")
    (src / "tosec-dat-pack-2026-01-01.zip.part").write_bytes(b"x")
    old = gt._PROCESS_START - 3600
    for f in src.iterdir():
        os.utime(f, (old, old))
    gt.clean_leftovers()
    assert [p.name for p in src.iterdir()] == ["tosec-dat-pack-2025-03-13.zip"]


def test_the_shutdown_stops_downloads_and_waits_for_them():
    import ast
    import inspect
    import textwrap
    from soniqboom import main
    tree = ast.parse(textwrap.dedent(inspect.getsource(main.shutdown)))
    calls = {getattr(n.func, "attr", getattr(n.func, "id", None))
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert {"cancel_downloads"} <= calls
    src = inspect.getsource(main.shutdown)
    assert "wait_for_downloads" in src


def test_a_body_shorter_than_announced_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gt.urllib.request, "urlopen", lambda req, timeout: _Resp(b"x" * 10, length=100))
    with pytest.raises(ValueError, match="cut off"):
        gt._fetch("http://h/", tmp_path / "f")


def test_the_reads_get_the_long_timeout_once_connected(monkeypatch):
    got = []

    class _Sock:
        def settimeout(self, t):
            got.append(t)

    class _WithSock(_Resp):
        def __init__(self, body):
            super().__init__(body)
            self.fp = type("F", (), {"raw": type("R", (), {"_sock": _Sock()})()})()
    monkeypatch.setattr(gt.urllib.request, "urlopen", lambda req, timeout: _WithSock(b"ok"))
    gt._fetch("http://h/", timeout=300)
    assert got == [300]


async def test_a_redump_list_that_shrank_to_less_than_half_keeps_the_kept_one(monkeypatch, store):
    lists = {c: [f"Game {c} {i} (Europe)" for i in range(10)] for c in dat.REDUMP_SYSTEMS}
    monkeypatch.setattr(gt, "_fetch", _redump_fetch(lists, []))
    gt.start_download("redump")
    await _wait("redump")
    lists["psx"] = ["Only One (Europe)"]               # redump.org served a degenerate list
    gt.start_download("redump")
    job = await _wait("redump")
    assert job["state"] == "done" and "PlayStation" in job["message"]
    assert len(gt._read_list("redump")["psx"]) == 10
    assert "psx" in gt._read_meta("redump")["systems_kept"]


async def test_a_redump_update_with_every_list_unchanged_rebuilds_nothing(monkeypatch, store):
    lists = {c: [f"Game {c} (Europe)"] for c in dat.REDUMP_SYSTEMS}
    monkeypatch.setattr(gt, "_fetch", _redump_fetch(lists, []))
    changed = []
    gt.start_download("redump")
    await _wait("redump")
    monkeypatch.setattr(gt, "_lists_changed", lambda: changed.append(1))
    mtime = (gt._download_dir() / "redump.tsv.gz").stat().st_mtime_ns
    gt.start_download("redump")
    job = await _wait("redump")
    assert job["state"] == "done" and job["changed"] is False and "up to date" in job["message"]
    assert (gt._download_dir() / "redump.tsv.gz").stat().st_mtime_ns == mtime and changed == []
    lists["ss"] = ["Nights (Japan)", "Panzer Dragoon (Europe)"]
    gt.start_download("redump")                       # one changed: rebuilt
    job = await _wait("redump")
    assert job.get("changed", True) and changed == [1]
    assert gt._read_list("redump")["saturn"] == {"Nights", "Panzer Dragoon"}


async def test_an_update_that_fetches_every_system_clears_the_could_not_be_fetched_note(monkeypatch, store):
    """One system failed on an Update (its kept list used, the note says so);
    the next Update fetches every system unchanged: the list is built again
    and the note is gone — never "up to date" with the note left standing."""
    lists = {c: [f"Game {c} (Europe)"] for c in dat.REDUMP_SYSTEMS}
    down = set()

    def fetch(url, dest=None, **kw):
        code = url.rstrip("/").rsplit("/", 1)[-1]
        if code in down:
            raise OSError("timed out")
        dest.write_bytes(_redump_zip(lists[code]))
    monkeypatch.setattr(gt, "_fetch", fetch)
    gt.start_download("redump")
    await _wait("redump")
    down.add("cdtv")
    gt.start_download("redump")
    job = await _wait("redump")
    assert "except 1 of" in job["message"]
    assert gt._read_meta("redump")["systems_failed"] == ["cdtv"]
    assert gt.status()["downloads"]["redump"]["systems_failed"] == ["cdtv"]
    down.clear()
    gt.start_download("redump")                       # every system back, every list the same
    job = await _wait("redump")
    assert job["state"] == "done" and job["message"] == "Downloaded"
    meta = gt._read_meta("redump")
    assert meta["systems_failed"] == [] and meta["systems_kept"] == []
    assert not gt.status()["downloads"]["redump"].get("systems_failed")
    assert "Game cdtv" in gt._read_list("redump")[dat.REDUMP_SYSTEMS["cdtv"]]
    gt.start_download("redump")                       # and now it is up to date
    job = await _wait("redump")
    assert job["changed"] is False and "up to date" in job["message"]


async def test_the_stop_button_stops_a_download(monkeypatch, store):
    import threading
    from soniqboom.api import admin
    hold = threading.Event()
    lists = {c: [f"Game {c} (Europe)"] for c in dat.REDUMP_SYSTEMS}
    calls = []

    def gate(code, ctl):
        if len(calls) == 3:
            hold.wait(5)
    monkeypatch.setattr(gt, "_fetch", _redump_fetch(lists, calls, gate))
    gt.start_download("redump")
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.02)
    st = await admin.admin_game_titles_stop("redump", _tok="x")
    d = st["downloads"]["redump"]
    assert d["state"] == "interrupted" and d["message"].startswith("Stopped after 2 of 12 systems")
    assert d["files"]["resumable"] is True and d["files"]["done"] == 2
    hold.set()
    assert await asyncio.to_thread(gt.wait_for_downloads, 5)
    assert (await admin.admin_game_titles_stop("redump", _tok="x"))["downloads"]["redump"]["state"] == "interrupted"
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await admin.admin_game_titles_stop("nope", _tok="x")


async def test_a_late_stop_of_the_previous_run_leaves_this_runs_download_alone(monkeypatch, store):
    import threading
    from soniqboom.core import scanner
    hold = threading.Event()
    lists = {"psx": ["Wipeout (Europe)"]}
    calls = []
    monkeypatch.setattr(gt, "_fetch", _redump_fetch(lists, calls, lambda c, ctl: hold.wait(5)))
    monkeypatch.setattr(scanner, "_run_epoch", 7)
    gt.start_download("redump")
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.02)
    assert gt.cancel_downloads(epoch=6) == []         # the previous run's stop, ending late
    assert gt._jobs["redump"]["state"] == "downloading"
    assert gt._ctls["redump"].thread.daemon is True
    assert gt.cancel_downloads(epoch=7) == ["redump"]
    assert gt.wait_for_downloads(0.1) is False        # stuck in the (fake) transfer
    hold.set()
    assert await asyncio.to_thread(gt.wait_for_downloads, 5)


def test_deleting_the_files_leaves_anything_else_in_the_folder():
    d = gt._sources_dir("redump")
    d.mkdir(parents=True)
    (d / "psx.zip").write_bytes(b"x" * 10)
    (d / "files.json").write_text("{}")
    (d / ".psx.zip.1-2.part").write_bytes(b"x")
    (d / "notes.txt").write_text("mine")
    assert gt.delete_files("redump") == 10 + 2 + 1
    assert [p.name for p in d.iterdir()] == ["notes.txt"]


def test_a_download_whose_list_is_written_completes_rather_than_stops():
    ctl = gt._Ctl()
    ctl.committed = True
    gt._ctls["redump"] = ctl
    gt._jobs["redump"] = {"state": "reading", "message": "Reading the Redump lists"}
    assert gt.cancel_download("redump") is False
    assert gt._jobs["redump"]["state"] == "reading" and not ctl.cancel.is_set()


def test_zips_with_the_same_files_but_other_timestamps_are_the_same_list(tmp_path):
    """redump.org builds its zip anew for each request: only the timestamps
    inside differ."""
    def make(p, when, text):
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(zipfile.ZipInfo("list.dat", date_time=when), text)
    make(tmp_path / "a.zip", (2026, 6, 15, 11, 55, 46), "same dat")
    make(tmp_path / "b.zip", (2026, 9, 27, 10, 0, 0), "same dat")
    make(tmp_path / "c.zip", (2026, 9, 27, 10, 0, 0), "new dat!")
    assert (tmp_path / "a.zip").read_bytes() != (tmp_path / "b.zip").read_bytes()
    assert gt._same_zip_content(tmp_path / "a.zip", tmp_path / "b.zip") is True
    assert gt._same_zip_content(tmp_path / "a.zip", tmp_path / "c.zip") is False
    (tmp_path / "d.zip").write_bytes(b"not a zip")
    assert gt._same_zip_content(tmp_path / "a.zip", tmp_path / "d.zip") is False
