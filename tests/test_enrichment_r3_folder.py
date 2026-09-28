# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Round-3 folder-album fixes (``core/folder_album.py``):

* composer / handle folders are refused although Modland filled the artist:
  a handle spelled differently (squashed, one edit apart, 8.3-cut, or the
  handle next to another name) counts as the track's person;
* sibling vote: under an index of composer folders the remaining children
  are refused (``AHXSONGS.LHA::Mr Tickle`` is Xeron under an alias), and an
  archive whose top-level folders are mostly not albums is the release —
  its tracks take the archive's name (``bcompo5.zip::weird`` → "bcompo5");
* compo size+format folders ("64kbMOD"), tool names with a version
  ("Scream Tracker 3") and format names written with/without spaces are
  container words, while real albums ("Gold of the Aztecs", "Dune II",
  "COMPOS10", "Big Demo") keep their folder album."""
from __future__ import annotations

import pytest

from soniqboom.core import folder_album as fa


async def _no_refresh(_ids):
    """Stand-in for ``folder_album.refresh_album_caches`` (which would touch
    the real data dir's browse cache file)."""
    return None

ROOT = "/lib"
ROOTS = frozenset({ROOT})
PEOPLE = ["Jerry", "Tommy", "Pink", "Xeron", "Freqvibez", "Jazz (NL)", "Curt Cool",
          "Luv Kohli", "Goto80", "Technix", "Frank Klepacki", "DJ Owl", "Pedro"]


def _t(tid, path, artist="Someone Else", **kw):
    t = {"id": tid, "path": path, "title": f"song {tid}", "artist": artist, "album": "",
         "album_artist": "", "composer": "", "format": "ProTracker", "genre": []}
    t.update(kw)
    return t


def _args(**kw):
    args = dict(roots=ROOTS,
                format_keys=fa._format_keys_static() | frozenset(
                    fa.format_name_keys(["Sound Monitor", "Screamtracker 3"])),
                author_keys=frozenset(), artist_keys=fa.person_keys(PEOPLE))
    args.update(kw)
    return args


def _collect(tracks, **kw):
    return {tid: u["album"] for tid, u, _ in fa.collect_folder_updates(tracks, **_args(**kw))}


# ── handle spelled differently = the track's person ──────────────────────────

@pytest.mark.parametrize("folder,artist", [
    ("curtcool", "Curt Cool"),              # squashed-equal
    ("freQvibes", "Freqvibez"),             # one edit
    ("goto8o", "Goto80"),
    ("leviatha", "Leviathan"),              # 8.3 cut
    ("JaZz^Jolly", "Jazz (NL)"),            # handle next to another name
    ("pennelin_(crawdaddy)", "Crawdaddy"),
])
def test_a_differently_spelled_handle_is_refused(folder, artist):
    assert _collect([_t("1", f"{ROOT}/x/{folder}/a.mod", artist=artist)]) == {}


@pytest.mark.parametrize("path,artist,album", [
    (f"{ROOT}/x/synergy_demo/a.ym", "Scavenger / Synergy", "synergy demo"),
    (f"{ROOT}/dalezy/dalezy_chiptunes_1997-1999.zip::captain.xm", "Dalezy",
     "dalezy chiptunes 1997-1999"),
    (f"{ROOT}/x/eltopo-dancemania.zip::foodchain.mod", "El Topo", "eltopo-dancemania"),
    (f"{ROOT}/x/crux_tnt.zip::Tunturi.xm", "Crux", "crux tnt"),
])
def test_artist_named_releases_keep_their_album(path, artist, album):
    """"<artist> Demo" dirs and "<artist>-<release>" archives are releases."""
    assert _collect([_t("1", path, artist=artist)]) == {"1": album}


# ── sibling votes ────────────────────────────────────────────────────────────

AHX = f"{ROOT}/disks/ab-ahx1/AHXSONGS.LHA::"
AHX_TRACKS = [
    _t("jerry", AHX + "Jerry\\AHX.Addiction.ahx", artist="Jerry", format="AHX"),
    _t("tommy", AHX + "Tommy\\AHX.Second Try.ahx", artist="Tommy", format="AHX"),
    _t("pink", AHX + "Pink\\AHX.Pinky.ahx", artist="Pink", format="AHX"),
    _t("tickle", AHX + "Mr Tickle\\AHX.64k Is All You Need.ahx", artist="Xeron", format="AHX"),
    _t("freq", AHX + "freQvibes\\AHX.Easy Job.ahx", artist="Freqvibez", format="AHX"),
    _t("jazz", AHX + "JaZz^Jolly\\Misc\\AHX.Adrenalyn.ahx", artist="Jazz (NL)", format="AHX"),
]


def test_an_archive_of_composer_folders_is_the_release():
    got = _collect(AHX_TRACKS)
    assert got == {t["id"]: "AHXSONGS" for t in AHX_TRACKS}
    assert "Mr Tickle" not in got.values()


def test_a_lone_uncredited_alias_folder_still_needs_a_release_signal():
    assert _collect([_t("1", AHX + "Mr Tickle\\AHX.a.ahx", artist="", format="AHX")]) == {}


def test_a_directory_index_of_handles_refuses_the_remaining_children():
    idx = f"{ROOT}/compilations/cta-modules"
    got = _collect([
        _t("cc", f"{idx}/curtcool/aftermid.mod", artist="Curt Cool"),
        _t("lk", f"{idx}/luvkohli/luv-argh.s3m", artist="Luv Kohli"),
        _t("g8", f"{idx}/goto8o/goto8o-balsam.mod", artist="Goto80"),
        _t("tx", f"{idx}/teknix/teknix-android.mod", artist="Technix"),   # 2 edits away
        _t("bd", f"{idx}/big_demo/zoolook.mod", artist="Rob Hubbard"),  # a release name
    ])
    assert got == {"bd": "big demo"}


def test_compo_category_folders_take_the_archive_name():
    z5 = f"{ROOT}/compos/big_chipcompo/bcompo5.zip::"
    z2 = f"{ROOT}/compos/big_chipcompo/bcompo2.zip::"
    got = _collect([
        _t("w", z5 + "weird/FF_2MASG.XM", artist="Funky Fish"),     # categories: several
        _t("w2", z5 + "weird/vibe-ishould.it", artist="Vibe"),      # artists each
        _t("h", z5 + "happy/FF_JOTR.XM", artist="Funky Fish"),
        _t("h2", z5 + "happy/dl-unity.xm", artist="Dodging Liquid"),
        _t("j", z5 + "jazz/HOPRELA.XM", artist="Zefyros"),       # "jazz" is a person key
        _t("r", z5 + "remix/cerror.mod", artist="Cerror"),       # generic → archive already
        _t("m", z2 + "medium/TURHA.XM", artist="JDruid"),
        _t("b", z2 + "big/VN-HNH.IT", artist="Nula"),
    ])
    assert got == {i: "bcompo5" for i in ("w", "w2", "h", "h2", "j", "r")} | \
        {"m": "bcompo2", "b": "bcompo2"}


def test_game_and_compo_folders_keep_their_album():
    """Negative cases: a game dir under a publisher dir, compo dirs inside a
    compo pack whose siblings are albums too."""
    ww = f"{ROOT}/compilations/cta-adlib/Westwood"
    cz = f"{ROOT}/compos/composita/composita.zip::"
    got = _collect([
        _t("d0", f"{ww}/Dune II/DUNE0.ADL", artist="Frank Klepacki", format="AdLib"),
        _t("d1", f"{ww}/Dune II/DUNE1.ADL", artist="Frank Klepacki", format="AdLib"),
        _t("k1", f"{ww}/Kyrandia/K1.ADL", artist="Frank Klepacki", format="AdLib"),
        _t("c10", cz + "COMPOS10/COPPA.IT", artist="DJ Owl"),
        _t("c11", cz + "COMPOS11/X.IT", artist="Pedro"),
        _t("c12", cz + "c12dixanabo/Y.IT", artist="Dixan & Nabo"),
    ])
    assert got == {"d0": "Dune II", "d1": "Dune II", "k1": "Kyrandia",
                   "c10": "COMPOS10", "c11": "COMPOS11", "c12": "c12dixanabo"}


def test_chunked_collect_with_one_state_equals_one_call():
    """The vote spans chunks: a caller feeding the library in chunks passes one
    state and takes the patches from ``finalize_folder_updates``."""
    tracks = AHX_TRACKS + [_t(f"g{i}", f"{ROOT}/Games/Turrican {i % 3}/t{i}.mod")
                           for i in range(9)]
    whole = fa.collect_folder_updates(tracks, **_args())
    rc, dc, state = {}, {}, fa.new_folder_state()
    for i in range(0, len(tracks), 2):
        assert fa.collect_folder_updates(tracks[i:i + 2], **_args(), retro_cache=rc,
                                         dir_cache=dc, state=state) == []
    assert fa.finalize_folder_updates(state) == whole
    assert {u["album"] for _, u, _ in whole} == {"AHXSONGS", "Turrican 0", "Turrican 1",
                                                 "Turrican 2"}


# ── container words ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "64kbMOD", "64kMODrmx", "4 64krmxMOD", "Scream Tracker 3", "Scream_Tracker_3",
    "Pro Tracker 3.15", "Fast Tracker 2", "medium", "Big", "tiny",
])
def test_compo_and_tool_folders_are_generic(name):
    assert fa._generic(fa.clean_folder_name(name))


@pytest.mark.parametrize("name", ["Gold of the Aztecs", "Big Demo", "Big Trouble",
                                  "Tiny Toon Adventures", "weird", "happy"])
def test_ordinary_names_are_not_generic(name):
    assert not fa._generic(fa.clean_folder_name(name))


def test_format_names_match_with_or_without_spaces():
    assert _collect([_t("1", f"{ROOT}/SoundMonitor/a.bp", format="SoundMon")]) == {}
    assert _collect([_t("2", f"{ROOT}/Scream Tracker 3/x.s3m")]) == {}
    assert _collect([_t("3", f"{ROOT}/Gold_of_the_Aztecs/dw.intro")]) == \
        {"3": "Gold of the Aztecs"}


def test_an_archive_of_game_folders_keeps_them():
    """Two non-album folders beside two single-composer game folders: the
    games are not categories, so the archive is not taken as the release."""
    z = f"{ROOT}/packs/Hits.zip::"
    got = _collect([
        _t("t1", z + "Turrican/title.mod", artist="Chris Huelsbeck"),
        _t("t2", z + "Turrican/ingame.mod", artist="Chris Huelsbeck"),
        _t("l1", z + "Lotus/race.mod", artist="Barry Leitch"),
        _t("p", z + "Xeron/tune.mod", artist="Xeron"),
        _t("r", z + "remix/x.mod", artist="Someone"),
    ])
    assert got == {"t1": "Turrican", "t2": "Turrican", "l1": "Lotus", "r": "Hits"}


# ── one entry per folder = a compo pack; re-judging earlier passes ───────────

CTC = f"{ROOT}/compos/ctc/ctc2003ent.zip::"
CTC_TRACKS = [
    _t("au", CTC + "aurora/AURORA.IT", artist=""),
    _t("bp", CTC + "b_planet/B_PLANET.IT", artist="Butch"),
    _t("nb", CTC + "notabene/NOTABENE.IT", artist="Dedanis"),
    _t("tf", CTC + "theflow/THEFLOW.IT", artist="Djkor"),
    _t("ns", CTC + "ns-til21/UNTIL21.IT", artist="Nyquist"),    # entry, differently named
]


def test_a_pack_of_one_entry_folders_is_the_release():
    """Folders named after their only tune are compo entries: every track —
    the differently named entry too — takes the pack's name."""
    assert _collect(CTC_TRACKS) == {t["id"]: "ctc2003ent" for t in CTC_TRACKS}


def _applied(tracks, patches):
    by = {tid: u for tid, u, _ in patches}
    return [dict(t, **by[t["id"]]) if t["id"] in by else t for t in tracks]


@pytest.mark.parametrize("tracks", [CTC_TRACKS, AHX_TRACKS, [
    # an archive of one game and two uncredited entry folders: the second pass
    # used to skip the game's stamped tracks and flip the vote
    _t("g1", f"{ROOT}/p/Hits2003.zip::Turrican/title.mod", artist="Chris Huelsbeck"),
    _t("g2", f"{ROOT}/p/Hits2003.zip::Turrican/ingame.mod", artist="Chris Huelsbeck"),
    _t("e1", f"{ROOT}/p/Hits2003.zip::aurora/AURORA.IT", artist="Butch"),
    _t("e2", f"{ROOT}/p/Hits2003.zip::theflow/THEFLOW.IT", artist="Djkor"),
]])
def test_a_second_pass_over_the_result_changes_nothing(tracks):
    first = fa.collect_folder_updates(tracks, **_args())
    assert fa.collect_folder_updates(_applied(tracks, first), **_args()) == []


def test_albums_of_earlier_passes_are_judged_again():
    """A folder album an older pass stamped is kept while the verdict holds,
    rewritten when it changed and cleared when the folder no longer
    qualifies (never a user-edited one)."""
    stamped = dict(album_source=fa.SOURCE_FOLDER)
    tracks = [
        _t("keep", f"{ROOT}/Games/Turrican/a.mod", album="Turrican", **stamped),
        _t("alias", AHX + "Mr Tickle\\AHX.a.ahx", artist="", album="Mr Tickle",
           format="AHX", **stamped),                                # no longer qualifies
        _t("hvsc", f"{ROOT}/C64Music/MUSICIANS/H/Hubbard_Rob/x.sid", album="Hubbard Rob",
           format="SID", **stamped),
        _t("edited", AHX + "Pink\\AHX.b.ahx", artist="Pink", album="Pinky",
           format="AHX", user_edited=["album"], **stamped),
        _t("moved", CTC + "ns-til21/UNTIL21.IT", artist="Nyquist", album="ns-til21",
           **stamped),                                              # now the pack's name
        *CTC_TRACKS[:4],
    ]
    got = {tid: (u, e) for tid, u, e in fa.collect_folder_updates(tracks, **_args())}
    assert "keep" not in got and "edited" not in got
    for tid, old in (("alias", "Mr Tickle"), ("hvsc", "Hubbard Rob")):
        assert got[tid] == ({"album": "", "album_source": None},
                            {"album": old, "album_source": fa.SOURCE_FOLDER})
    assert got["moved"][0] == {"album": "ctc2003ent", "album_source": fa.SOURCE_FOLDER}
    assert got["moved"][1] == {"album": "ns-til21", "album_source": fa.SOURCE_FOLDER}


async def test_apply_is_idempotent_and_reverts_what_it_no_longer_derives(monkeypatch):
    from soniqboom.core import scene_metadata as sm
    from soniqboom.core.store import TrackStore
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(fa, "refresh_album_caches", _no_refresh)
    monkeypatch.setattr(sm, "modland_name_keys", lambda: (frozenset(), frozenset()))
    monkeypatch.setattr(fa, "_last_seq", None)
    s.upsert_scan_dir(ROOT)
    s.upsert_tracks_batch([dict(t, added_at=1) for t in CTC_TRACKS + [
        _t("tk", f"{ROOT}/Games/Turrican/a.mod"),
        _t("old", AHX + "Mr Tickle\\AHX.a.ahx", artist="", album="Mr Tickle",
           format="AHX", album_source=fa.SOURCE_FOLDER)]])
    res = await fa.apply_folder_albums(force=True)
    assert res["updated"] == len(CTC_TRACKS) + 2
    assert s.get_track("old")["album"] == "" and s.get_track("old")["album_source"] is None
    assert s.get_track("ns")["album"] == "ctc2003ent"
    assert (await fa.apply_folder_albums(force=True))["updated"] == 0
