# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retro metadata mapping: derived genres (core/genre_derive.py), their
Subsonic / browse surfaces, multi-value tags (NUL-joined ID3v2.4 frames) and
no lyrics write-back from Subsonic lookups."""
from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import subprocess
import types
import xml.etree.ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soniqboom.api import subsonic
from soniqboom.api import tracks as tracks_api
from soniqboom.core import demozoo, genre_derive, metadata, repair, subsonic_state
from soniqboom.core import subsonic_index as sx
from soniqboom.core.store import TrackStore, track_genres

ROOT = "/lib"


def _t(tid, path, fmt, genre=None, **kw):
    d = {"id": tid, "path": f"{ROOT}/{path}", "title": kw.pop("title", tid), "artist": "",
         "album": "", "format": fmt, "genre": list(genre or []), "added_at": 1_700_000_000,
         "duration": 100.0, "year": 1990}
    d.update(kw)
    return d


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    s.upsert_scan_dir(ROOT, {})
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)

    async def _no_refresh(ids):
        return None
    monkeypatch.setattr("soniqboom.core.folder_album.refresh_album_caches", _no_refresh)
    return s


def _lib(s: TrackStore) -> None:
    s.upsert_tracks_batch([
        # HVSC game tune (folder only), a SID whose game a source named, a demo
        _t("hg", "C64Music/GAMES/A-F/Commando.sid", "SID", ["Chiptune", "C64"], title="Commando",
           artist="Rob Hubbard"),
        _t("hm", "C64Music/MUSICIANS/H/Hubbard_Rob/Delta.sid", "SID", ["Chiptune", "C64"],
           title="Delta", artist="Rob Hubbard", game_by_songdb="Delta"),
        _t("hd", "C64Music/DEMOS/0-9/Party_Tune.sid", "SID", ["Chiptune", "C64"],
           artist="Jeroen Tel", scene_group="Maniacs of Noise • Fairlight"),
        # scene modules: Demozoo use + party, cracktro folder, group only
        _t("m1", "mods/a/zone.mod", "ProTracker", ["Tracker", "Module"], artist="Moby",
           scene_group="Razor 1911", scene_use=["Demo"], scene_party="Assembly 1994"),
        _t("m2", "scene/cracktros/crk.xm", "FastTracker 2", ["Tracker", "Module"]),
        _t("m3", "mods/b/song.mod", "ProTracker", ["Tracker", "Module"], artist="Lizardking",
           scene_group="Vibrants"),
        # console rip with a header game, untagged chip tune, AdLib
        _t("sp", "snes/smw/01.spc", "SPC", ["Chiptune"], album="Super Mario World",
           album_source="tag"),
        _t("ay", "zx/x.ay", "AY", ["Chiptune"]),
        _t("ad", "dos/x.imf", "AdLib IMF", ["Chiptune"]),
        # NOT derived: mainstream file, retro file with a real genre, a typed genre
        _t("mp", "pop/a.mp3", "MP3", ["Rock"], artist="Band", album="LP"),
        _t("mx", "pop/b.flac", "FLAC", [], artist="Band", album="LP"),
        _t("rg", "mods/c/x.mod", "ProTracker", ["Tracker", "Module", "Techno"]),
        _t("ue", "mods/d/x.mod", "ProTracker", [], user_edited=["genre"]),
        # unsupported / unknown format: nothing, no error
        _t("un", "misc/x.bin", "", []),
    ])


# ── derivation rules ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("t, names, want", [
    (dict(format="SID", path="/lib/C64Music/GAMES/A-F/Commando.sid", genre=["Chiptune", "C64"]),
     True, ["Game", "SID", "Commando"]),
    (dict(format="SID", path="/lib/C64Music/GAMES/A-F/Commando.sid", genre=["Chiptune", "C64"]),
     False, ["Game", "SID"]),
    (dict(format="SID", path="/lib/C64Music/DEMOS/x.sid", genre=["Chiptune", "C64"],
          scene_group="Fairlight • Triad"), True, ["Demo", "Scene", "SID", "Fairlight", "Triad"]),
    (dict(format="SID", path="/lib/x/y.sid", genre=["Chiptune", "C64"]), True, ["SID"]),
    (dict(format="ProTracker", path="/lib/m/x.mod", genre=["Tracker", "Module"],
          scene_group="Razor 1911"), True, ["Scene", "Amiga", "Razor 1911"]),
    (dict(format="ScreamTracker 3", path="/lib/m/x.s3m", genre=["Tracker", "Module"]), True, None),
    (dict(format="FastTracker 2", path="/lib/m/x.xm", genre=[], title="Fairlight Crack-Intro"),
     True, ["Cracktro", "Scene"]),
    (dict(format="FastTracker 2", path="/lib/scene/musicdisks/md.zip::a.xm", genre=[]),
     True, ["Music Disk", "Scene"]),
    (dict(format="TFMX Pro", path="/lib/x", genre=["Amiga", "Module"], scene_use=["Cracktro"],
          scene_party="The Party 1991"), True, ["Cracktro", "Scene", "Amiga", "The Party 1991"]),
    (dict(format="Jochen Hippel ST", path="/lib/x", genre=["Amiga", "Module"]), True, ["Atari ST"]),
    (dict(format="NSF", path="/lib/x.nsf", genre=["Chiptune"], game="Mega Man 2"), True,
     ["Game", "NES", "Mega Man 2"]),
    (dict(format="VGM", path="/lib/x.vgz", genre=["Chiptune"]), True, ["Game"]),
    (dict(format="PSF2", path="/lib/x.psf2", genre=["Chiptune", "Game Rip"]), True,
     ["Game", "PlayStation 2"]),
    (dict(format="MIDI", path="/lib/games/sierra/kq.mid", genre=["MIDI"]), True, ["Game", "MIDI"]),
    (dict(format="AdLib IMF", path="/lib/x.imf", genre=["Chiptune"]), True, ["AdLib"]),
    # a group / game name equal to a vocabulary word adds nothing twice
    (dict(format="NSF", path="/lib/x.nsf", genre=[], game="NES"), True, ["Game", "NES"]),
    # fill-only: a real genre, a typed (even cleared) genre, a mainstream file
    (dict(format="SID", path="/lib/x.sid", genre=["Chiptune", "C64", "Electronic"]), True, None),
    (dict(format="SID", path="/lib/x.sid", genre=[], user_edited=["genre"]), True, None),
    (dict(format="MP3", path="/lib/games/x.mp3", genre=[], game="Doom"), True, None),
    # unsupported / empty metadata: nothing, never an error
    (dict(format="", path="", genre=None), True, None),
    (dict(format="SID", genre=None, game=None, scene_group=None, scene_use="junk"), True, ["SID"]),
])
def test_derivation_rules(t, names, want):
    assert genre_derive.derive(t, names=names, roots=(ROOT,)) == want


def test_folders_above_the_scan_root_say_nothing():
    """A share named "Demo:" or a music folder named "Games" is no category
    for every tune in it — only folders below the scan root count."""
    root = "ftp://nas/Music/Demo:"
    t = dict(format="ProTracker", path=f"{root}/groups/fairlight/x.zip::a.mod", genre=[])
    assert genre_derive.derive(t, roots=(root,)) == ["Amiga"]
    t["path"] = f"{root}/demos/groups/fairlight/x.zip::a.mod"
    assert genre_derive.derive(t, roots=(root,)) == ["Demo", "Scene", "Amiga"]
    t = dict(format="SID", path="/Games/x/y.sid", genre=[])
    assert genre_derive.derive(t, roots=("/Games",)) == ["SID"]


# ── the pass ─────────────────────────────────────────────────────────────────

async def test_the_pass_writes_fill_only_and_the_genres_browse(store):
    _lib(store)
    rev = dict(store._track_rev)
    res = await genre_derive.apply_derived_genres()
    g = {tid: store.get_track(tid).get("genre_derived") for tid in store._tracks}
    assert g["hg"] == ["Game", "SID", "Commando"]
    assert g["hm"] == ["Game", "SID", "Delta"]
    assert g["hd"] == ["Demo", "Scene", "SID", "Maniacs of Noise", "Fairlight"]
    assert g["m1"] == ["Demo", "Scene", "Amiga", "Razor 1911", "Assembly 1994"]
    assert g["m2"] == ["Cracktro", "Scene"]
    assert g["m3"] == ["Scene", "Amiga", "Vibrants"]
    assert g["sp"] == ["Game", "SNES", "Super Mario World"]
    assert g["ay"] == ["ZX Spectrum"] and g["ad"] == ["AdLib"]
    for tid in ("mp", "mx", "rg", "ue", "un"):
        assert g[tid] is None, tid
    # the file's own genre list is never touched
    assert store.get_track("hg")["genre"] == ["Chiptune", "C64"]
    assert store.get_track("mp")["genre"] == ["Rock"]
    assert res["updated"] == 9 and res["derived"] == 9
    assert all(store._track_rev[t] > rev[t] for t in g if g[t])     # Subsonic memo key moved
    assert store._track_rev["mp"] == rev["mp"]
    # every genre a track shows can be browsed
    names = {r["genre"]: r["count"] for r in store.aggregate_genres()}
    assert names["Game"] == 3 and names["Scene"] == 4 and names["SID"] == 3
    assert names["Commando"] == 1 and names["Rock"] == 1 and names["Chiptune"] == 6
    for tid in g:
        for gen in track_genres(store.get_track(tid)):
            assert tid in store.filter_track_ids(genre=gen), (tid, gen)
    # [No Genre] lists only tracks that show none
    assert set(store.filter_track_ids(untagged="genre")) == {"mx", "ue", "un"}
    assert store.verify_indexes()["index_ok"]
    # a second run with nothing written since does nothing
    assert await genre_derive.apply_derived_genres() == {"skipped": "unchanged"}


async def test_sources_change_and_withdraw(store):
    _lib(store)
    await genre_derive.apply_derived_genres()
    # a real genre appears (the user types one): withdrawn, browse follows
    store.update_track_fields("m3", {"genre": ["Techno"], "user_edited": ["genre"]})
    # the song database's game is withdrawn: Game and the name go
    store.update_track_fields("hm", {"game_by_songdb": None})
    # a Demozoo group is stamped on a module
    store.update_track_fields("m2", {"scene_group": "Fairlight"})
    res = await genre_derive.apply_derived_genres()          # incremental
    assert res["updated"] == 3 and res["derived"] == 8
    assert store.get_track("m3").get("genre_derived") is None
    assert store.get_track("hm")["genre_derived"] == ["SID"]
    assert store.get_track("m2")["genre_derived"] == ["Cracktro", "Scene", "Fairlight"]
    assert "Vibrants" not in {r["genre"] for r in store.aggregate_genres()}
    assert not store.filter_track_ids(genre="Delta")
    assert store.verify_indexes()["index_ok"]


async def test_the_setting_adds_and_removes_the_names(store):
    _lib(store)
    await genre_derive.apply_derived_genres()
    store.set_config(genre_derive.CONFIG_KEY, False)
    res = await genre_derive.apply_derived_genres()          # inputs changed → full
    assert store.get_track("hg")["genre_derived"] == ["Game", "SID"]
    assert store.get_track("m1")["genre_derived"] == ["Demo", "Scene", "Amiga"]
    assert store.get_track("ay")["genre_derived"] == ["ZX Spectrum"]   # unchanged
    assert res["updated"] == 6
    gen = {r["genre"] for r in store.aggregate_genres()}
    assert not gen & {"Commando", "Razor 1911", "Assembly 1994", "Super Mario World"}
    store.set_config(genre_derive.CONFIG_KEY, True)
    await genre_derive.apply_derived_genres(force=True)
    assert store.get_track("hg")["genre_derived"] == ["Game", "SID", "Commando"]


async def test_a_rescan_keeps_them_and_is_no_change(store):
    from soniqboom.core.scanner import _same_track_content
    _lib(store)
    await genre_derive.apply_derived_genres()
    old = dict(store.get_track("hg"))
    fresh = _t("hg", "C64Music/GAMES/A-F/Commando.sid", "SID", ["Chiptune", "C64"],
               title="Commando", artist="Rob Hubbard")
    assert _same_track_content(old, dict(fresh))
    store.upsert_tracks_batch([fresh])
    assert store.get_track("hg")["genre_derived"] == ["Game", "SID", "Commando"]
    assert "hg" in store.filter_track_ids(genre="Game")


async def test_a_user_edit_rederives_at_once(store):
    _lib(store)
    await genre_derive.apply_derived_genres()
    store.update_track_fields("ay", {"game": "Dizzy", "user_edited": ["game"]})
    assert genre_derive.rederive_now(["ay"]) == 1
    assert store.get_track("ay")["genre_derived"] == ["Game", "ZX Spectrum", "Dizzy"]


async def test_the_store_listener_schedules_a_pass(store, monkeypatch):
    calls = []
    monkeypatch.setattr(genre_derive, "schedule", lambda: calls.append(1))
    monkeypatch.setattr(genre_derive, "_POKE_DELAY_S", 0.01)
    store.set_enrich_listener(genre_derive.poke)
    try:
        store.upsert_track(_t("x", "a.sid", "SID"))
        store.update_track_fields("x", {"title": "y"})
        await asyncio.sleep(0.05)
        assert calls == [1]                      # one debounced run for the burst
        store.update_track_fields("x", {"genre_derived": ["SID"]})   # its own output: exempt
        await asyncio.sleep(0.05)
        assert calls == [1]
    finally:
        store.set_enrich_listener(None)


async def test_the_pass_at_library_scale_is_sliced_and_complete(store):
    """270k tracks: every retro track derived, the loop never held for long."""
    import time
    rows = []
    for i in range(270_000):
        k = i % 6
        if k == 0:
            rows.append(_t(f"t{i}", f"C64Music/GAMES/A/G{i}.sid", "SID", ["Chiptune", "C64"]))
        elif k == 1:
            rows.append(_t(f"t{i}", f"mods/m{i}.mod", "ProTracker", ["Tracker", "Module"],
                           scene_group=f"Group {i % 500}"))
        elif k == 2:
            rows.append(_t(f"t{i}", f"nes/{i}.nsf", "NSF", ["Chiptune"], game=f"Game {i % 4000}"))
        else:
            rows.append(_t(f"t{i}", f"pop/{i}.mp3", "MP3", ["Rock"]))
    store.enter_batch_mode()
    store.upsert_tracks_batch(rows)
    store.exit_batch_mode()
    from soniqboom.core.store import freeze_long_lived_heap
    freeze_long_lived_heap("the test library")        # as the server does after its load
    gaps = []
    stop = False

    async def probe():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
    task = asyncio.create_task(probe())
    import gc
    try:
        t0 = time.perf_counter()
        res = await genre_derive.apply_derived_genres()
        secs = time.perf_counter() - t0
    finally:
        stop = True
        await task
        gc.unfreeze()
    assert res["derived"] == 135_000 and res["updated"] == 135_000
    print(f"\n270k pass: {secs:.2f}s, max loop gap {max(gaps) * 1000:.1f} ms, "
          f"genres {len(store.aggregate_genres())}")
    assert max(gaps) < 0.25


# ── multi-value tags ─────────────────────────────────────────────────────────

def _ffmpeg(path, *args):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=f=440:d=1", *args, str(path)], capture_output=True)
    if r.returncode:
        pytest.skip(f"ffmpeg can't write {path.name}")


def test_id3v24_multi_value_frames_are_split(tmp_path):
    from mutagen.id3 import ID3, TALB, TCOM, TCON, TIT2, TPE1, TPE2
    f = tmp_path / "multi.mp3"
    _ffmpeg(f, "-c:a", "libmp3lame", "-id3v2_version", "0", "-write_id3v1", "0")
    tags = ID3()
    tags.add(TCON(encoding=3, text=["Rock", "Indie", "17"]))
    tags.add(TPE1(encoding=3, text=["A", "B"]))
    tags.add(TPE2(encoding=3, text=["X", "Y"]))
    tags.add(TCOM(encoding=3, text=["C1", "C2"]))
    tags.add(TIT2(encoding=3, text=["T1", "T2"]))
    tags.add(TALB(encoding=3, text=["Al"]))
    tags.save(f, v2_version=4)
    d = metadata.extract(f, "m1").model_dump()
    assert d["genre"] == ["Rock", "Indie"]              # "17" is Rock (ID3v1 ref), once
    assert d["artist"] == "A • B" and d["album_artist"] == "X • Y"
    assert d["composer"] == "C1 • C2" and d["title"] == "T1 / T2" and d["album"] == "Al"
    assert not any("\x00" in str(v) for v in d.values())


def test_flac_repeated_comments_are_kept(tmp_path):
    from mutagen.flac import FLAC
    f = tmp_path / "multi.flac"
    _ffmpeg(f, "-c:a", "flac")
    a = FLAC(f)
    a["artist"] = ["A", "B"]
    a["genre"] = ["Jazz", "Funk\x00Soul"]
    a["title"] = ["Song"]
    a.save()
    d = metadata.extract(f, "f1").model_dump()
    assert d["artist"] == "A • B" and d["genre"] == ["Jazz", "Funk", "Soul"]
    assert d["title"] == "Song"


async def test_stored_nul_values_are_repaired_once(store):
    store.upsert_tracks_batch([
        _t("n1", "pop/n1.mp3", "MP3", ["Rock\x00Indie"], artist="A\x00B", title="T"),
        _t("n2", "pop/n2.mp3", "MP3", ["17\x00(RX)"], album_artist="X\x00Y", composer="C\x00D"),
        _t("ok", "pop/ok.mp3", "MP3", ["Pop"], artist="Solo"),
    ])
    rev = dict(store._track_rev)
    res = await repair.repair_multi_values_once(store)
    assert res["repaired"] == 2
    n1, n2 = store.get_track("n1"), store.get_track("n2")
    assert n1["genre"] == ["Rock", "Indie"] and n1["artist"] == "A • B"
    assert n2["genre"] == ["Rock", "Remix"] and n2["album_artist"] == "X • Y"
    assert n2["composer"] == "C • D"
    assert store._track_rev["ok"] == rev["ok"] and store._track_rev["n1"] > rev["n1"]
    assert {r["genre"] for r in store.aggregate_genres()} == {"Rock", "Indie", "Remix", "Pop"}
    assert store.verify_indexes()["index_ok"]
    # label / ISRC / comment frames too; a raw ID3v1 genre reference of an
    # ID3-tagged file (no NUL) is decoded, a FLAC's "17" is left alone
    store.upsert_tracks_batch([
        _t("n4", "pop/n4.mp3", "MP3", ["(17)"], label="Ocean\x00Imagine",
           comment="a\x00b", isrc="X1\x00X2"),
        _t("n5", "pop/n5.flac", "FLAC", ["17"]),
    ])
    res = await repair.repair_multi_values_once(store, force=True)
    n4 = store.get_track("n4")
    assert res["repaired"] == 1 and n4["genre"] == ["Rock"]
    assert (n4["label"], n4["comment"], n4["isrc"]) == ("Ocean / Imagine", "a\nb", "X1 / X2")
    assert store.get_track("n5")["genre"] == ["17"]
    # warm: done once — a NUL value that appears later (an old AOF record) is
    # left to a forced run, which repairs exactly it
    store.upsert_tracks_batch([_t("n3", "pop/n3.mp3", "MP3", ["Ska\x00Dub"])])
    assert await repair.repair_multi_values_once(store) == {"skipped": True}
    res = await repair.repair_multi_values_once(store, force=True)
    assert res["repaired"] == 1 and store.get_track("n3")["genre"] == ["Ska", "Dub"]


async def test_repair_timing_on_a_270k_library(store):
    import time
    rows = [_t(f"t{i}", f"p/{i}.mp3", "MP3", ["Rock\x00Pop"] if i % 50 == 0 else ["Rock"],
               artist="A\x00B" if i % 50 == 0 else "A") for i in range(270_000)]
    store.enter_batch_mode()
    store.upsert_tracks_batch(rows)
    store.exit_batch_mode()
    t0 = time.perf_counter()
    res = await repair.repair_multi_values_once(store)
    secs = time.perf_counter() - t0
    print(f"\n270k repair: {res['repaired']} repaired in {secs:.2f}s")
    assert res["repaired"] == 5400
    t0 = time.perf_counter()
    assert await repair.repair_multi_values_once(store) == {"skipped": True}
    assert time.perf_counter() - t0 < 0.01


# ── Subsonic ─────────────────────────────────────────────────────────────────

@pytest.fixture
def ss(store, monkeypatch, tmp_path):
    _lib(store)
    store.upsert_tracks_batch([
        _t("ma", "pop/multi.mp3", "MP3", ["Rock"], artist="Alpha • Beta", album="Duo",
           title="Both"),
        _t("lb", "c64/loose.sid", "SID", ["Chiptune", "C64"], artist="Composer X",
           album="Some Game", label="Ocean, Imagine", songdb_fields=["label"]),
        _t("nul", "pop/nul.mp3", "MP3", ["Rock"], artist="Old", album="Old",
           comment="bad\x00value\x01"),
    ])
    store.set_config("subsonic_folder_albums", False)
    monkeypatch.setattr(subsonic, "get_store", lambda: store)
    monkeypatch.setattr(subsonic, "_CACHE_DEBOUNCE_SEC", 0.0)
    subsonic_state.reset_state(tmp_path / "state.json")
    app = FastAPI()
    app.include_router(subsonic.router)
    user = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: user)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", lambda *a, **k: user)
    asyncio.get_event_loop_policy()
    c = TestClient(app)

    def get(method, fmt="json", **params):
        r = c.get(f"/rest/{method}", params={**params, "f": fmt})
        assert r.status_code == 200, r.text
        return r
    get.store = store
    return get


def _j(r):
    body = r.json()["subsonic-response"]
    assert body["status"] == "ok", body
    return body


def test_subsonic_before_the_pass_shows_the_file_genres(ss):
    song = _j(ss("getSong.view", id="hg"))["song"]
    assert song["genre"] == "Chiptune" and [g["name"] for g in song["genres"]] == ["Chiptune", "C64"]


async def test_subsonic_songs_genres_and_browsing(ss):
    await genre_derive.apply_derived_genres()
    song = _j(ss("getSong.view", id="hg"))["song"]
    assert song["genre"] == "Game"
    assert [g["name"] for g in song["genres"]] == ["Game", "SID", "Commando", "Chiptune", "C64"]
    x = ET.fromstring(ss("getSong.view", fmt="xml", id="hg").content)
    ns = {"s": "http://subsonic.org/restapi"}
    xs = x.find("s:song", ns)
    assert xs.get("genre") == "Game"
    assert [g.get("name") for g in xs.findall("s:genres", ns)] == [g["name"] for g in song["genres"]]
    mp = _j(ss("getSong.view", id="mp"))["song"]
    assert mp["genre"] == "Rock" and [g["name"] for g in mp["genres"]] == ["Rock"]
    # getGenres lists every genre a song shows, with browsable counts
    genres = {g["value"]: g for g in _j(ss("getGenres.view"))["genres"]["genre"]}
    for name in ("Game", "Demo", "Cracktro", "Scene", "SID", "Amiga", "Commando", "Razor 1911"):
        assert name in genres, name
    assert genres["Game"]["songCount"] == 3
    by_genre = _j(ss("getSongsByGenre.view", genre="Game", count=50))["songsByGenre"]["song"]
    assert {s["id"] for s in by_genre} == {"hg", "hm", "sp"}
    assert genres["Game"]["albumCount"] >= 1
    albums = _j(ss("getAlbumList2.view", type="byGenre", genre="Game", size=50))
    assert {a["name"] for a in albums["albumList2"]["album"]} >= {"Super Mario World"}
    rnd = _j(ss("getRandomSongs.view", genre="Cracktro", size=10))["randomSongs"]["song"]
    assert [s["id"] for s in rnd] == ["m2"]
    # a composer contributor for a retro tune, none for a mainstream one
    assert song["contributors"] == [{"role": "composer", "artist": {
        "id": sx.artist_id("rob hubbard"), "name": "Rob Hubbard"}}]
    assert mp["contributors"] == []
    # a console rip's artist is often its developer: no composer role
    store = ss.store
    store.update_track_fields("sp", {"artist": "Nintendo"})
    assert _j(ss("getSong.view", id="sp"))["song"]["contributors"] == []


def test_subsonic_multi_artist_and_labels(ss):
    song = _j(ss("getSong.view", id="ma"))["song"]
    assert song["artist"] == "Alpha • Beta"
    assert song["artists"] == [{"id": sx.artist_id("alpha"), "name": "Alpha"},
                               {"id": sx.artist_id("beta"), "name": "Beta"}]
    names = {a["name"] for i in _j(ss("getArtists.view"))["artists"]["index"] for a in i["artist"]}
    assert {"Alpha", "Beta"} <= names
    art = _j(ss("getArtist.view", id=sx.artist_id("beta")))["artist"]
    assert [a["name"] for a in art["album"]] == ["Duo"] and art["albumCount"] == 1
    al = _j(ss("getAlbum.view", id=sx.album_id("composer x", "some game")))["album"]
    assert al["recordLabels"] == [{"name": "Ocean"}, {"name": "Imagine"}]
    lst = _j(ss("getAlbumList2.view", type="alphabeticalByName", size=100))["albumList2"]["album"]
    duo = next(a for a in lst if a["name"] == "Duo")
    assert duo["recordLabels"] == []
    # a placeholder part of a multi-artist name is no artist of its own
    ss.store.update_track_fields("ma", {"artist": "Alpha \u2022 <?>"})
    song = _j(ss("getSong.view", id="ma"))["song"]
    assert song["artists"] == [{"id": sx.artist_id("alpha"), "name": "Alpha"}]


def test_subsonic_xml_and_json_say_the_same_about_a_control_character(ss):
    js = _j(ss("getSong.view", id="nul"))["song"]
    x = ET.fromstring(ss("getSong.view", fmt="xml", id="nul").content)
    xs = x.find("{http://subsonic.org/restapi}song")
    assert js["comment"] == xs.get("comment") == "badvalue"


# ── lyrics: never written into a file from a Subsonic lookup ─────────────────

@pytest.fixture
def lyr(store, monkeypatch, tmp_path):
    f = tmp_path / "song.mp3"
    _ffmpeg(f, "-c:a", "libmp3lame", "-id3v2_version", "0", "-write_id3v1", "0")
    store.upsert_track({"id": "ly", "path": str(f), "title": "Song", "artist": "Band",
                        "album": "", "format": "MP3", "genre": [], "duration": 1.0})
    monkeypatch.setattr(subsonic, "get_store", lambda: store)

    async def _get_track(tid):
        t = store.get_track(tid)
        return types.SimpleNamespace(**t) if t else None
    monkeypatch.setattr(tracks_api, "get_track", _get_track)
    monkeypatch.setattr(tracks_api, "_lyrics_cache", {})
    monkeypatch.setattr(tracks_api, "_lyrics_miss", {})
    monkeypatch.setattr(tracks_api, "_lyrics_wb_pending", set())

    async def _cfg(key, default=None):
        return True if key == "lyrics_writeback" else default
    monkeypatch.setattr("soniqboom.core.data.get_config", _cfg)
    calls = []

    async def online(*a):
        calls.append(a)
        return {"lyrics": "la la la", "synced": False, "source": "LRCLib"}, True
    monkeypatch.setattr(tracks_api, "_resolve_online_lyrics", online)
    return f, calls


async def _settle():
    for _ in range(40):
        await asyncio.sleep(0.05)


EDITOR = types.SimpleNamespace(id="e1", username="ed", role="edit", enabled=True)
ADMIN = types.SimpleNamespace(id="a1", username="ad", role="admin", enabled=True)
READER = types.SimpleNamespace(id="r1", username="ro", role="readonly", enabled=True)


def _state(f, store):
    return (f.read_bytes(), f.stat().st_mtime_ns, store._track_rev["ly"])


async def test_subsonic_lyrics_lookups_never_write_tags(store, lyr):
    f, calls = lyr
    before = _state(f, store)
    got = await subsonic._lyrics_for_track_id("ly")
    assert got["text"] == "la la la" and len(calls) == 1
    await _settle()
    assert _state(f, store) == before
    # a second Subsonic call (from the cache) writes nothing either
    await subsonic._lyrics_for_track_id("ly")
    await _settle()
    assert _state(f, store) == before
    # a read-only web user served from that cache: still nothing written
    res = await tracks_api.get_lyrics("ly", user=READER)
    assert res["lyrics"] == "la la la" and len(calls) == 1
    await _settle()
    assert _state(f, store) == before
    # an edit user's web lookup offers the cached lyrics to the write-back
    res = await tracks_api.get_lyrics("ly", user=EDITOR)
    assert res["lyrics"] == "la la la" and len(calls) == 1      # served from the cache
    await _settle()
    assert metadata.extract_lyrics(f) == "la la la"


@pytest.mark.parametrize("user", [EDITOR, ADMIN])
async def test_an_edit_users_web_lookup_writes_back(store, lyr, user):
    f, calls = lyr
    res = await tracks_api.get_lyrics("ly", user=user)
    assert res["lyrics"] == "la la la"
    await _settle()
    assert metadata.extract_lyrics(f) == "la la la"


@pytest.mark.parametrize("user", [READER, None])
async def test_a_read_only_or_anonymous_web_lookup_never_writes(store, lyr, user):
    f, calls = lyr
    before = _state(f, store)
    res = await tracks_api.get_lyrics("ly", user=user)
    assert res["lyrics"] == "la la la" and len(calls) == 1
    await _settle()
    assert _state(f, store) == before
    assert metadata.extract_lyrics(f) is None


async def test_the_lyrics_route_passes_the_signed_in_user(store, lyr, monkeypatch):
    """Over HTTP: the route resolves the session user (``current_user``) and
    a read-only account writes nothing, an edit account writes."""
    import httpx
    from soniqboom.api import users as users_api
    f, _calls = lyr
    who = {"u": READER}

    async def _cur():
        return who["u"]
    app = FastAPI()
    app.include_router(tracks_api.router, prefix="/api")
    app.dependency_overrides[users_api.current_user] = _cur
    before = _state(f, store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/api/tracks/ly/lyrics")
        assert r.status_code == 200 and r.json()["lyrics"] == "la la la"
        await _settle()
        assert _state(f, store) == before
        who["u"] = EDITOR
        assert (await c.get("/api/tracks/ly/lyrics")).json()["lyrics"] == "la la la"
        await _settle()
    assert metadata.extract_lyrics(f) == "la la la"


# ── Demozoo: how a tune was used ─────────────────────────────────────────────

def _dump(tmp_path):
    """Composer 'tel' (500): "Zone" (music 1, 1991) is the soundtrack of a
    1991 demo and entered "Assembly 1991"; "Crack" (2, 1990) of a 1990
    cracktro; "Old Hit" (3, 1985) of a 1988 cracktro (a reuse); "Disk Tune"
    (4, 1992) is in a 1992 music disk; "Twice" (5 and 6, 1993) is two
    productions, one used by a 1993 demo, one by nothing."""
    dump = (
        "COPY public.demoscene_releaser (id, name, first_name, surname, is_group) FROM stdin;\n"
        "500\ttel\tJeroen\tTel\tf\n\\.\n"
        "COPY public.demoscene_nick (id, releaser_id, name, abbreviation) FROM stdin;\n"
        "5000\t500\ttel\t\\N\n\\.\n"
        "COPY public.demoscene_nickvariant (id, nick_id, name) FROM stdin;\n\\.\n"
        "COPY public.demoscene_membership (id, member_id, group_id) FROM stdin;\n\\.\n"
        "COPY public.productions_production (id, title, supertype, release_date_date) FROM stdin;\n"
        "1\tZone\tmusic\t1991-03-01\n2\tCrack\tmusic\t1990-01-01\n3\tOld Hit\tmusic\t1985-01-01\n"
        "4\tDisk Tune\tmusic\t1992-01-01\n5\tTwice\tmusic\t1993-01-01\n6\tTwice\tmusic\t1993-05-01\n"
        "11\tBig Demo\tproduction\t1991-08-01\n12\tCrk\tproduction\t1990-02-01\n"
        "13\tCrk 2\tproduction\t1988-02-01\n14\tMusic Disk 1\tproduction\t1992-02-01\n"
        "15\tDemo 93\tproduction\t1993-02-01\n\\.\n"
        "COPY public.productions_production_author_nicks (id, production_id, nick_id) FROM stdin;\n"
        "1\t1\t5000\n2\t2\t5000\n3\t3\t5000\n4\t4\t5000\n5\t5\t5000\n6\t6\t5000\n\\.\n"
        "COPY public.productions_productiontype (id, name, path, depth, numchild, \"position\", internal_name) FROM stdin;\n"
        "1\tDemo\t0001\t1\t0\t1\tdemo\n2\tCracktro\t0002\t1\t0\t2\tcracktro\n"
        "3\tMusic Disk\t0003\t1\t0\t3\tmusicdisk\n4\t64K Intro\t0004\t1\t0\t4\t64k\n\\.\n"
        "COPY public.productions_production_types (id, production_id, productiontype_id) FROM stdin;\n"
        "1\t11\t1\n2\t12\t2\n3\t13\t2\n4\t14\t3\n5\t15\t4\n\\.\n"
        "COPY public.productions_soundtracklink (id, production_id, soundtrack_id) FROM stdin;\n"
        "1\t11\t1\n2\t12\t2\n3\t13\t3\n4\t15\t5\n\\.\n"
        "COPY public.productions_packmember (id, pack_id, member_id, \"position\") FROM stdin;\n"
        "1\t14\t4\t1\n\\.\n"
        "COPY public.parties_party (id, name) FROM stdin;\n7\tAssembly 1991\n\\.\n"
        "COPY public.parties_competition (id, party_id, name) FROM stdin;\n70\t7\tMusic\n\\.\n"
        "COPY public.parties_competitionplacing (id, competition_id, production_id, ranking, \"position\") FROM stdin;\n"
        "1\t70\t1\t1\t1\n\\.\n"
    )
    p = tmp_path / "dump.sql.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write(dump)
    return p


def test_use_kinds():
    assert [demozoo.use_kind(n) for n in ("Demo", "64K Intro", "Cracktro", "BBStro", "Game",
                                          "Music Disk", "Diskmag", "Tracked Music", None)] == [
        "Demo", "Demo", "Cracktro", "Cracktro", "Game", "Music Disk", "Diskmag", None, None]


async def test_demozoo_stamps_use_and_party_and_the_genres_follow(store, tmp_path, monkeypatch):
    db = tmp_path / "demozoo.sqlite"
    monkeypatch.setattr(demozoo, "_db_path", lambda: db)
    st = demozoo.refresh_index(dump_path=_dump(tmp_path))
    assert st["error"] is None and st["uses"] == 4
    monkeypatch.setattr(demozoo, "_last_apply_sig", None)
    demozoo._status["applying"] = False
    mk = lambda tid, title: _t(tid, f"mods/{tid}.mod", "ProTracker", ["Tracker", "Module"],  # noqa: E731
                               title=title, artist="tel")
    store.upsert_tracks_batch([mk("z", "Zone"), mk("c", "Crack"), mk("o", "Old Hit"),
                               mk("d", "Disk Tune"), mk("w", "Twice"),
                               mk("s", "Old Hit"), _t("x", "pop/x.mp3", "MP3", [], title="Zone",
                                                      artist="tel")])
    store.update_track_fields("s", {"scene_use": ["Demo"]})       # stale: withdrawn
    await demozoo.apply_to_library(force=True)
    g = lambda tid: store.get_track(tid)                           # noqa: E731
    assert (g("z")["scene_use"], g("z")["scene_party"]) == (["Demo"], "Assembly 1991")
    assert g("c")["scene_use"] == ["Cracktro"] and not g("c").get("scene_party")
    assert not g("o").get("scene_use")                  # a later cracktro reused it
    assert g("d")["scene_use"] == ["Music Disk"]
    assert not g("w").get("scene_use")                  # two productions disagree
    assert not g("s").get("scene_use") and not g("x").get("scene_use")
    await genre_derive.apply_derived_genres()
    assert g("z")["genre_derived"] == ["Demo", "Scene", "Amiga", "Assembly 1991"]
    assert g("c")["genre_derived"] == ["Cracktro", "Scene", "Amiga"]
    assert g("o")["genre_derived"] == ["Amiga"]
    # Reset removes them; the genres follow on the next pass
    n, batch = demozoo.reset_enrichment()
    store.update_track_fields_batch(batch)
    assert not g("z").get("scene_use") and not g("z").get("scene_party")
    await genre_derive.apply_derived_genres()
    assert g("z")["genre_derived"] == ["Amiga"]
    # a rescan keeps a stamp while composer and title are the same
    await demozoo.apply_to_library(force=True)
    store.upsert_tracks_batch([mk("z", "Zone")])
    assert g("z")["scene_use"] == ["Demo"]
    store.upsert_tracks_batch([mk("z", "Zone Remix")])
    assert not g("z").get("scene_use")


# ── ID stability when a tag gains a second value ("A" → "A • B") ─────────────

def _flac(path, **tags):
    from mutagen.flac import FLAC
    _ffmpeg(path, "-c:a", "flac")
    a = FLAC(path)
    for k, v in tags.items():
        a[k] = v
    a.save()


def _scan_id(path) -> str:
    """The track id the scanner gives a local file (``scanner`` uses uuid5 of
    the path string)."""
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(path)))


def _rescan(store, paths):
    rows = []
    for p in paths:
        d = metadata.extract(p, _scan_id(p)).model_dump()
        d.pop("embedding", None)
        rows.append(d)
    store.upsert_tracks_batch(rows)


@pytest.fixture
def ids_env(store, monkeypatch, tmp_path):
    store.set_config("subsonic_folder_albums", False)
    monkeypatch.setattr(subsonic, "get_store", lambda: store)
    monkeypatch.setattr(subsonic, "_CACHE_DEBOUNCE_SEC", 0.0)
    subsonic_state.reset_state(tmp_path / "state.json")
    app = FastAPI()
    app.include_router(subsonic.router)
    user = types.SimpleNamespace(id="u1", username="alice", role="admin", enabled=True)
    monkeypatch.setattr(subsonic, "_require_user", lambda *a, **k: user)
    monkeypatch.setattr(subsonic, "_require_user_no_cookie", lambda *a, **k: user)
    from soniqboom.core import data as data_mod
    monkeypatch.setattr(data_mod, "get_store", lambda: store)
    c = TestClient(app)

    def ok(method, **params):
        r = c.get(f"/rest/{method}", params={**params, "f": "json"})
        body = r.json()["subsonic-response"]
        assert body["status"] == "ok", body
        return body
    d = tmp_path / "lib"
    d.mkdir()
    return store, ok, d


def _snapshot(ok, store):
    arts = {a["name"]: a["id"] for i in ok("getArtists.view")["artists"]["index"]
            for a in i["artist"]}
    albums = {(a["name"], a["artist"]): (a["id"], a["songCount"])
              for a in ok("getAlbumList2.view", type="alphabeticalByName", size=50)
              ["albumList2"]["album"]}
    s2 = ok("getStarred2.view")["starred2"]
    starred = ({a["id"] for a in s2["artist"]}, {a["id"] for a in s2["album"]},
               {s["id"] for s in s2["song"]})
    pls = ok("getPlaylists.view")["playlists"]["playlist"]
    entries = [s["id"] for s in ok("getPlaylist.view", id=pls[0]["id"])["playlist"]["entry"]]
    web_albums = {r["album"]: r["count"] for r in store.aggregate_albums()}
    return arts, albums, starred, entries, web_albums


def test_a_tag_gaining_a_second_artist_keeps_every_id(ids_env):
    """Before: the old extractor kept only the first ARTIST / ALBUMARTIST
    ("A").  After: the files say "A • B".  Artist A, the album, the stars and
    the playlist entries are the same; B is a new artist of its own."""
    store, ok, d = ids_env
    p1, p2, p3 = d / "1.flac", d / "2.flac", d / "3.flac"
    for p, title in ((p1, "One"), (p2, "Two")):
        _flac(p, artist=["A"], albumartist=["A"], album=["LP"], title=[title])
    _flac(p3, artist=["A"], album=["Solo"], title=["Three"])
    _rescan(store, [p1, p2, p3])                       # the before-state ("A")
    a_id, lp_id = sx.artist_id("a"), sx.album_id("a", "lp")
    ok("star.view", id=[_scan_id(p1)], albumId=lp_id, artistId=a_id)
    ok("createPlaylist.view", name="Mix", songId=[_scan_id(p1), _scan_id(p2), _scan_id(p3)])
    before = _snapshot(ok, store)
    assert before[0]["A"] == a_id and before[1][("LP", "A")] == (lp_id, 2)

    # the files gain a second value; ONE of the album's tracks is rescanned
    for p in (p1, p2):
        _flac(p, artist=["A", "B"], albumartist=["A", "B"], album=["LP"],
              title=["One" if p == p1 else "Two"])
    _flac(p3, artist=["A", "B"], album=["Solo"], title=["Three"])
    _rescan(store, [p1])
    assert store.get_track(_scan_id(p1))["album_artist"] == "A • B"
    mid = _snapshot(ok, store)
    assert mid[1][("LP", "A")] == (lp_id, 2)            # still ONE album, same id
    # web: the Album Artists / Artists views list A (both tracks), no "A • B"
    aa = {r["album_artist"]: r["count"] for r in store.aggregate_album_artists()}
    assert aa == {"A": 2, "B": 1}
    assert {r["album"]: r["count"] for r in store.aggregate_albums(album_artist="A")} == {"LP": 2}
    assert {r["artist"] for r in store.aggregate_artists()} == {"A", "B"}
    assert store.verify_indexes()["index_ok"]
    assert mid[0]["A"] == a_id and mid[2] == before[2] and mid[3] == before[3]
    assert mid[4] == before[4]                          # web Albums view: no split

    _rescan(store, [p2, p3])                            # every file re-read
    after = _snapshot(ok, store)
    assert {_scan_id(p) for p in (p1, p2, p3)} == set(store._tracks)   # track ids from paths
    assert after[0]["A"] == a_id and "B" in after[0] and "A • B" not in after[0]
    assert after[1] == before[1]                        # album ids and song counts
    assert after[2] == before[2]                        # stars resolve (artist, album, song)
    assert after[3] == before[3]                        # playlist entries
    assert after[4] == before[4]
    assert {r["album"]: r["count"] for r in store.aggregate_albums(album_artist="A")} == {"LP": 2}
    assert {r["album"]: r["count"] for r in store.aggregate_albums(artist="B")} == {"LP": 2, "Solo": 1}
    assert len(store.filter_track_ids(artist="A \u2022 B")) == 3   # the joined value still filters
    song = ok("getSong.view", id=_scan_id(p1))["song"]
    assert song["artistId"] == a_id and song["albumId"] == lp_id
    assert song["artist"] == "A • B" and song["displayAlbumArtist"] == "A • B"
    assert [a["id"] for a in song["artists"]] == [a_id, sx.artist_id("b")]
    # B's page lists the albums it appears on; its top songs find its tracks
    b = ok("getArtist.view", id=sx.artist_id("b"))["artist"]
    assert {al["id"] for al in b["album"]} == {lp_id, sx.album_id("a", "solo")}
    top = ok("getTopSongs.view", artist="B", count=10)["topSongs"].get("song", [])
    assert {s["id"] for s in top} == {_scan_id(p) for p in (p1, p2, p3)}


async def test_the_nul_repair_moves_subsonic_stars_to_the_new_ids(ids_env, tmp_path):
    """MP3s stored NUL-joined by older builds had ids hashed on "a\x00b":
    the repair moves the stars and ratings set on them."""
    store, ok, _d = ids_env
    dh = store.store_hash_lookup("/lib/pop")
    store.upsert_tracks_batch([
        _t("n1", "pop/1.mp3", "MP3", ["Rock"], artist="A\x00B", album_artist="A\x00B",
           album="LP", dir_hash=dh),
        _t("n2", "pop/2.mp3", "MP3", ["Rock"], artist="C", album="Other"),
    ])
    old_artist, old_album = sx.artist_id("a\x00b"), sx.album_id("a\x00b", "lp")
    st = subsonic_state.get_state()
    await st.star("u1", "artist", [old_artist])
    await st.star("u1", "album", [old_album, sx.album_id("c", "other")])
    await st.rate("u1", "album", old_album, 4)
    res = await repair.repair_multi_values_once(store, force=True)
    assert res["repaired"] == 1
    s2 = ok("getStarred2.view")["starred2"]
    assert {a["id"] for a in s2["artist"]} == {sx.artist_id("a")}
    assert {a["id"] for a in s2["album"]} == {sx.album_id("a", "lp"), sx.album_id("c", "other")}
    assert st.ratings("u1", "album") == {sx.album_id("a", "lp"): 4}
    # persisted
    subsonic_state.reset_state(tmp_path / "state.json")
    assert sx.artist_id("a") in subsonic_state.get_state().starred("u1", "artist")


def test_placeholder_first_and_legacy_lyrics_match(ss, monkeypatch):
    store = ss.store
    store.update_track_fields("ma", {"album_artist": "<?> \u2022 Gamma"})
    song = _j(ss("getSong.view", id="ma"))["song"]
    assert song["displayAlbumArtist"] == "Gamma"
    assert song["albumArtists"] == [{"id": sx.artist_id("gamma"), "name": "Gamma"}]
    seen = []

    async def _fake(tid):
        seen.append(tid)
        return {"text": "x", "synced": False, "artist": "a", "title": "Both"}
    monkeypatch.setattr(subsonic, "_lyrics_for_track_id", _fake)
    _j(ss("getLyrics.view", artist="Beta", title="Both"))
    assert seen == ["ma"]                       # an artist of "Alpha • Beta" matches


async def test_lyrics_fetched_while_the_setting_is_off_are_written_once_it_is_on(store, lyr,
                                                                              monkeypatch):
    f, calls = lyr
    on = {"v": False}

    async def _cfg(key, default=None):
        return on["v"] if key == "lyrics_writeback" else default
    monkeypatch.setattr("soniqboom.core.data.get_config", _cfg)
    before = _state(f, store)
    await tracks_api.get_lyrics("ly", user=EDITOR)          # setting off: no write
    await tracks_api.get_lyrics("ly", user=EDITOR)          # cache hit, still off
    await _settle()
    assert _state(f, store) == before and len(calls) == 1
    on["v"] = True
    await tracks_api.get_lyrics("ly", user=EDITOR)          # on now: the cached lyrics go in
    await _settle()
    assert metadata.extract_lyrics(f) == "la la la"
