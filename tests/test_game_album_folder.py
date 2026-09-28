# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub #13 — the opt-in "album from folder name for retro formats" pass
(``core/folder_album.py``): cleaning, the conservative stoplist, on / off /
revert, fill-only + user-edit safety, the post-scan hook gate.

Pure in-memory: a TrackStore per test; the Modland name sets and the cache
invalidation are monkeypatched (the real data dir is never touched)."""
from __future__ import annotations

import pytest

from soniqboom.core import folder_album as fa
from soniqboom.core import scene_metadata as sm
from soniqboom.core.store import TrackStore

ROOT = "/music/amiga"
ROOTS = frozenset({ROOT})


@pytest.mark.parametrize("raw,clean", [
    ("Gold_of_the_Aztecs", "Gold of the Aztecs"),
    ("Gold.of.the.Aztecs", "Gold of the Aztecs"),
    ("Dr. Robotnik's Mean Bean Machine", "Dr. Robotnik's Mean Bean Machine"),
    ("  Turrican   II ", "Turrican II"),
])
def test_clean_folder_name(raw, clean):
    assert fa.clean_folder_name(raw) == clean


@pytest.mark.parametrize("path,expect", [
    (f"{ROOT}/Gold_of_the_Aztecs/dw.intro", ("Gold_of_the_Aztecs", "", False)),
    (f"{ROOT}/Games/Gold_of_the_Aztecs/dw.intro", ("Gold_of_the_Aztecs", "Games", False)),
    (f"{ROOT}/dw.intro", None),                                   # directly in the root
    (f"{ROOT}/Gold_of_the_Aztecs.lha::dw.intro", ("Gold_of_the_Aztecs", "", True)),
    (f"{ROOT}/Pack.zip::Gold_of_the_Aztecs/dw.intro", ("Gold_of_the_Aztecs", "Pack", False)),
    (f"{ROOT}/Pack.lha::Jerry\\AHX.tune.ahx", ("Jerry", "Pack", False)),  # LHA backslash dirs
    # single-file wrapper archive → skipped outward
    (f"{ROOT}/Turrican/title.mod.zip::title.mod", ("Turrican", "", False)),
    ("ftp://host/share/Game X/a.mod", ("Game X", "share", False)),
])
def test_folder_candidate(path, expect):
    roots = ROOTS | {"ftp://host"}
    assert fa.folder_candidate(path, roots) == expect


def _t(tid, path, **kw):
    t = {"id": tid, "path": path, "title": "song", "artist": "Some Composer", "album": "",
         "album_artist": "", "composer": "", "format": "ProTracker", "genre": []}
    t.update(kw)
    return t


def _collect(tracks, **kw):
    args = dict(roots=ROOTS, format_keys=fa._format_keys_static() | {fa.name_key("Protracker")},
                author_keys=frozenset({fa.name_key("Jester")}),
                artist_keys=fa.person_keys(["Chris Huelsbeck", "Kai Lehmann (Ass It)"]))
    args.update(kw)
    return {tid: upd["album"] for tid, upd, _ in fa.collect_folder_updates(tracks, **args)}


def test_collect_fills_a_game_folder():
    got = _collect([_t("1", f"{ROOT}/Gold_of_the_Aztecs/dw.intro", format="David Whittaker",
                       artist="David Whittaker")])
    assert got == {"1": "Gold of the Aztecs"}


def test_uncredited_track_needs_a_release_signal():
    """No artist ⇒ a plain folder is too often the composer ("AHXSONGS/Mr
    Tickle/…"); only an archive's own name or a child of a release index
    ("Games/<X>") counts."""
    nobody = dict(artist="", format="AHX")
    assert _collect([_t("1", f"{ROOT}/Mr Tickle/a.ahx", **nobody)]) == {}
    assert _collect([_t("2", f"{ROOT}/AHXSONGS.LHA::Mr Tickle\\AHX.a.ahx", **nobody)]) == {}
    assert _collect([_t("3", f"{ROOT}/<?>/a.ahx", **{**nobody, "artist": "<?>"})]) == {}
    assert _collect([_t("4", f"{ROOT}/Games/Gold_of_the_Aztecs/dw.intro", **nobody)]) == \
        {"4": "Gold of the Aztecs"}
    assert _collect([_t("5", f"{ROOT}/disks/e9-emsn4.zip::WIDE.XM", **nobody)]) == \
        {"5": "e9-emsn4"}


@pytest.mark.parametrize("path,kw", [
    (f"{ROOT}/Music/x.mod", {}),                                  # generic word
    (f"{ROOT}/mods/x.mod", {}),
    (f"{ROOT}/Unsorted/x.mod", {}),
    (f"{ROOT}/Disk 2/x.mod", {}),                                 # album part
    (f"{ROOT}/A-F/x.mod", {}),                                    # letter bucket
    (f"{ROOT}/1991/x.mod", {}),                                   # numbers only
    (f"{ROOT}/old mods/x.mod", {}),                               # all-generic words
    (f"{ROOT}/64kb/x.it", {}),                                    # compo size category
    (f"{ROOT}/MOD - 4 channels/x.mod", {}),
    (f"{ROOT}/Protracker/x.mod", {}),                             # a format name
    (f"{ROOT}/Protracker/4-Mat/x.mod", {}),                       # Format/Author/file
    (f"{ROOT}/Artists/jogeir/x.mod", {}),                         # Artists/<person>/file
    (f"{ROOT}/Jester/x.mod", {}),                                 # a Modland author
    (f"{ROOT}/Huelsbeck_Chris/x.mod", {}),                        # a library artist
    (f"{ROOT}/Ass_It/x.mod", {}),                                 # handle of "Real (Handle)"
    (f"{ROOT}/Rob Hubbard/x.mod", {"artist": "Rob Hubbard"}),     # this track's artist
    (f"{ROOT}/Hubbard/x.mod", {"artist": "Rob Hubbard"}),         # words ⊆ artist
    (f"{ROOT}/song/x.mod", {"title": "Song"}),                    # its own title
    (f"{ROOT}/HSC-Tracker/x.hsc", {"format": "HSC AdLib"}),       # a tool name
    (f"{ROOT}/gerard, jean-s. (jess)/x.ym", {"format": "YM"}),    # "Surname, First (handle)"
    ("/music/C64Music/MUSICIANS/A/Avalon/Worktunes/x.sid",        # anything in HVSC
     {"format": "SID"}),
    (f"{ROOT}/Game/x.mp3", {"format": "MP3"}),                    # not a retro format
    (f"{ROOT}/Game/x.mod", {"album": "Real Album"}),              # album already set
    (f"{ROOT}/Game/x.mod", {"user_edited": ["album"]}),           # user cleared it
])
def test_collect_stoplist_refuses(path, kw):
    assert _collect([_t("1", path, **kw)]) == {}


def test_amiga_music_folder_is_generic_but_a_game_below_it_is_not():
    tracks = [_t("1", f"{ROOT}/Amiga/x.mod"), _t("2", f"{ROOT}/Amiga/Turrican II/y.mod")]
    assert _collect(tracks) == {"2": "Turrican II"}


# ── apply / revert on a store ────────────────────────────────────────────────

@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    monkeypatch.setattr(sm, "modland_name_keys", lambda: (frozenset(), frozenset()))
    calls = []
    async def _refresh(ids):
        calls.append(1)
    monkeypatch.setattr(fa, "refresh_album_caches", _refresh)
    s.invalidations = calls
    s.upsert_scan_dir(ROOT)
    fa._last_seq = None
    return s


def _seed(s):
    s.upsert_tracks_batch([
        _t("dw", f"{ROOT}/Gold_of_the_Aztecs/dw.intro", title="intro", format="David Whittaker"),
        _t("mod", f"{ROOT}/Turrican II/title.mod", title="title"),
        _t("tag", f"{ROOT}/Turrican II/x.spc", format="SPC", album="Header Game", album_source="tag"),
        _t("mp3", f"{ROOT}/Turrican II/y.mp3", format="MP3"),
        _t("loose", f"{ROOT}/loose.mod"),
    ])


async def test_disabled_by_default_and_on_off_revert(store):
    _seed(store)
    assert (await fa.apply_folder_albums())["skipped"] == "disabled"
    assert store.get_track("dw")["album"] == ""
    store.set_config(fa.CONFIG_KEY, True)
    res = await fa.apply_folder_albums()
    # two albums, plus the folder name of the header-game rip (its game's
    # other name — ``game_by_folder``)
    assert res["updated"] == 3
    assert store.get_track("tag")["game_aliases"] == ["Turrican II"]
    assert store.get_track("dw")["album"] == "Gold of the Aztecs"
    assert store.get_track("dw")["album_source"] == "folder"
    assert store.get_track("mod")["album"] == "Turrican II"
    assert store.get_track("tag")["album"] == "Header Game"      # tag untouched
    assert store.get_track("mp3")["album"] == ""                 # not retro
    assert store.get_track("loose")["album"] == ""               # sits in the root
    assert store.invalidations == [1]
    assert "mod" in store.filter_track_ids(album="Turrican II")
    # unchanged library → the pass skips its scan entirely
    assert (await fa.apply_folder_albums())["skipped"] == "unchanged"
    # the user edits one album, then the option goes off: only OUR albums go
    store.update_track_fields("mod", {"album": "My Name", "user_edited": ["album"]})
    reverted = await fa.revert_album_source(fa.SOURCE_FOLDER)
    assert reverted == 1
    assert store.get_track("dw")["album"] == "" and store.get_track("dw")["album_source"] is None
    assert store.get_track("mod")["album"] == "My Name"
    assert store.get_track("tag")["album"] == "Header Game"


async def test_folder_album_never_replaces_a_modland_one(store):
    _seed(store)
    store.update_track_fields("dw", {"album": "Gold of the Aztecs", "album_source": "modland-filename"})
    store.set_config(fa.CONFIG_KEY, True)
    await fa.apply_folder_albums()
    assert store.get_track("dw")["album_source"] == "modland-filename"


def _spy_batch_mode(store, monkeypatch) -> list[int]:
    depths: list[int] = []
    real = store.enter_batch_mode

    def spy():
        real()
        depths.append(store._batch_depth)
    monkeypatch.setattr(store, "enter_batch_mode", spy)
    return depths


async def test_medium_batch_stays_incremental_and_consistent(store, monkeypatch):
    depths = _spy_batch_mode(store, monkeypatch)
    store.set_config(fa.CONFIG_KEY, True)
    store.upsert_tracks_batch([_t(f"t{i}", f"{ROOT}/Turrican {i % 7}/x{i}.mod", title=f"s{i}")
                               for i in range(1200)])
    res = await fa.apply_folder_albums()
    assert res["updated"] == 1200
    assert depths == []                              # below the threshold: no batch mode
    assert store._batch_depth == 0 and not store._batch_mode
    assert store.verify_indexes()["index_ok"] is True
    assert len(store.filter_track_ids(album="Turrican 3")) == len([i for i in range(1200) if i % 7 == 3])


async def test_large_batch_uses_batch_mode_and_stays_consistent(store, monkeypatch):
    """The batch-mode branch itself (the real threshold is 10 000 — lowered
    here so several chunks, their loop yields and the async exit all run)."""
    monkeypatch.setattr(fa, "_BATCH_MODE_THRESHOLD", 100)
    monkeypatch.setattr(fa, "_WRITE_CHUNK", 64)
    depths = _spy_batch_mode(store, monkeypatch)
    store.set_config(fa.CONFIG_KEY, True)
    store.upsert_tracks_batch([_t(f"t{i}", f"{ROOT}/Turrican {i % 7}/x{i}.mod", title=f"s{i}")
                               for i in range(1200)])
    res = await fa.apply_folder_albums()
    assert res["updated"] == 1200
    assert depths == [1]
    assert store._batch_depth == 0 and not store._batch_mode and not store._sorted_dirty
    assert store.verify_indexes()["index_ok"] is True
    assert len(store.filter_track_ids(album="Turrican 3")) == len([i for i in range(1200) if i % 7 == 3])
    # The revert goes through the same helper.
    depths.clear()
    assert await fa.revert_album_source(fa.SOURCE_FOLDER) == 1200
    assert depths == [1]
    assert store._batch_depth == 0 and not store._batch_mode and not store._sorted_dirty
    assert store.verify_indexes()["index_ok"] is True
    assert store.filter_track_ids(album="Turrican 3") == []


# ── round-1 review fixes ─────────────────────────────────────────────────────

async def test_rescan_tag_and_user_edit_landing_mid_write_survive(store, monkeypatch):
    """The compare-and-set runs again before every chunk's write: a rescan's
    real tag or a hand edit that lands after the first chunk is never
    overwritten by the folder guess computed before it."""
    store.set_config(fa.CONFIG_KEY, True)
    store.upsert_tracks_batch([_t(f"t{i}", f"{ROOT}/Turrican II/x{i}.mod", title=f"s{i}")
                               for i in range(200)])
    real = store.update_track_fields_batch
    calls = []

    def racing(items):
        n = real(items)
        if not calls:
            # (a) a rescan upsert carrying a real album tag
            fresh = dict(store.get_track("t199"), album="Real Tag Album", album_source="tag")
            store.upsert_tracks_batch([fresh])
            # (b) a hand edit (PUT /meta) of another track's album
            store.update_track_fields("t198", {"album": "My Own Name", "user_edited": ["album"]})
        calls.append(len(items))
        return n
    monkeypatch.setattr(store, "update_track_fields_batch", racing)
    res = await fa.apply_folder_albums()
    assert len(calls) > 1, "the write must be chunked for this test to mean anything"
    assert (store.get_track("t199")["album"], store.get_track("t199")["album_source"]) == \
        ("Real Tag Album", "tag")
    t198 = store.get_track("t198")
    assert (t198["album"], t198["user_edited"]) == ("My Own Name", ["album"])
    assert t198.get("album_source") is None
    # 198 albums; the two others still get their folder name as a game name
    assert res["updated"] == 200
    assert (store.get_track("t199")["game_by_folder"], t198["game_by_folder"]) == \
        ("Turrican II", "Turrican II")
    assert store.verify_indexes()["index_ok"] is True


async def test_tracks_added_during_a_pass_are_picked_up_by_the_next(store, monkeypatch):
    store.set_config(fa.CONFIG_KEY, True)
    store.upsert_tracks_batch([_t(f"t{i}", f"{ROOT}/Turrican II/x{i}.mod", title=f"s{i}")
                               for i in range(120)])
    real = fa.collect_folder_updates
    added = []

    def collect_and_race(tracks, **kw):
        out = real(tracks, **kw)
        if not added:                        # a concurrent scan adds a track mid-pass
            store.upsert_tracks_batch([_t("late", f"{ROOT}/Turrican II/late.mod", title="late")])
            added.append(1)
        return out
    monkeypatch.setattr(fa, "collect_folder_updates", collect_and_race)
    assert (await fa.apply_folder_albums())["updated"] == 120
    assert store.get_track("late")["album"] == ""
    res2 = await fa.apply_folder_albums()
    assert res2.get("skipped") != "unchanged"
    assert store.get_track("late")["album"] == "Turrican II"
    # …and an unchanged library still skips after that productive pass.
    assert (await fa.apply_folder_albums())["skipped"] == "unchanged"


@pytest.mark.parametrize("path,kw,expect", [
    (f"{ROOT}/artists/x/oldies/a.zip::a.it", {}, None),
    (f"{ROOT}/Amiga Soundtracker/x.mod", {}, None),
    (f"{ROOT}/compo/x.mod", {}, None),
    (f"{ROOT}/Release/x.mod", {}, None),
    (f"{ROOT}/P/PI.zip::piano2.xm.zip::piano.xm", {}, None),     # "PI" — too short
    (f"{ROOT}/Q/decem.s3m.zip::december.s3m", {}, None),          # wrapper → "Q"
    (f"{ROOT}/compos/big_chipcompo/bcompo6.zip::song.xm", {"artist": ""}, "bcompo6"),
])
def test_container_words_and_module_wrappers_are_refused(path, kw, expect):
    got = _collect([_t("1", path, **kw)])
    assert got == ({} if expect is None else {"1": expect})


def test_post_scan_hook_is_a_noop_while_disabled(store, monkeypatch):
    started = []
    monkeypatch.setattr(fa.asyncio, "get_running_loop",
                        lambda: started.append(1) or (_ for _ in ()).throw(RuntimeError))
    fa.schedule_after_scan()
    assert started == [] and fa._running is False


# ── review follow-ups ────────────────────────────────────────────────────────

def test_remote_scan_root_with_colon_is_recognised():
    roots = frozenset({"ftp://10.0.0.88/Music/Demo"})
    assert fa.folder_candidate("ftp://10.0.0.88/Music/Demo:/x.mod", roots) is None
    assert fa.folder_candidate("ftp://10.0.0.88/Music/Demo:/Turrican II/x.mod", roots) == \
        ("Turrican II", "", False)


def test_composer_then_game_layout_even_for_composer_named_replayers():
    """The issue's own layout: Whittaker_David/Gold_of_the_Aztecs/DW.intro —
    the grandparent is a person (and ALSO a uade format name)."""
    got = _collect([_t("1", f"{ROOT}/Whittaker_David/Gold_of_the_Aztecs/DW.intro",
                       artist="", format="David Whittaker")],
                   format_keys=fa._format_keys_static() | {fa.name_key("David Whittaker")},
                   author_keys=frozenset({fa.name_key("David Whittaker")}))
    assert got == {"1": "Gold of the Aztecs"}
    # …but a real Modland-mirror layout (Format/Author/file) stays refused
    assert _collect([_t("2", f"{ROOT}/Protracker/Jogeir/x.mod")]) == {}


def test_accent_folding_and_new_folder():
    assert _collect([_t("1", f"{ROOT}/Hulsbeck/x.mod", artist="Chris Hülsbeck")]) == {}
    assert _collect([_t("2", f"{ROOT}/Downloads/New folder/x.mod")]) == {}


def test_generic_dir_inside_an_archive_falls_back_to_the_archive_name():
    got = _collect([_t("1", f"{ROOT}/Turrican.lha::music/title.mod", artist="")])
    assert got == {"1": "Turrican"}


async def test_post_scan_hook_runs_the_pass_when_enabled(store, monkeypatch):
    from soniqboom.core import scanner
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: False)
    _seed(store)
    store.set_config(fa.CONFIG_KEY, True)
    fa.schedule_after_scan()
    for _ in range(50):
        if not fa._running:
            break
        await fa.asyncio.sleep(0.01)
    assert store.get_track("dw")["album_source"] == "folder"


async def test_post_scan_hook_waits_for_a_running_scan(store, monkeypatch):
    from soniqboom.core import scanner
    monkeypatch.setattr(scanner, "is_scanning", lambda path=None: True)
    _seed(store)
    store.set_config(fa.CONFIG_KEY, True)
    fa.schedule_after_scan()
    for _ in range(50):
        if not fa._running:
            break
        await fa.asyncio.sleep(0.01)
    assert store.get_track("dw")["album"] == ""
