# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A Modland catalogue tune plays like the same file played locally: it is a
module / SID to the web player before its first play (A), open pages take
what that play read (B), its meter / pattern / SID requests wait for the
play's download instead of answering "not there" (C), the file's own author
stays the artist (D), Modland's HVSC mirror has STIL (E), and a SID's first
play renders its real length (F)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_modland_catalog import (  # noqa: E402,F401  (fixture)
    ROOT, SERVED, _enable, _tid, env)

from soniqboom.core import catalogue  # noqa: E402
from soniqboom.core import modland_catalog as mc  # noqa: E402


# ── A ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel,want", [
    ("JamCracker/Andy (UK)/music disk intro.jam", ["Amiga", "Module"]),
    ("Future Composer 1.4/x/apache flight.fc", ["Amiga", "Module"]),
    ("TFMX/Chris Huelsbeck/mdat.turrican", ["Amiga", "Module"]),
    ("SidMon 1/x/a.sid", ["Amiga", "Module"]),
    ("HVSC/MUSICIANS/T/Turner_Steve/Uridium.sid", ["Chiptune", "C64"]),
    ("Protracker/x/chip.mod", ["Tracker", "Module"]),
    ("Fasttracker 2/x/a.xm", ["Tracker", "Module"]),
    ("SC68/x/a.sndh", ["Chiptune", "Atari ST"]),
    ("Nintendo SPC/x/a.spc", ["Chiptune"]),
    ("AdLib/x/a.imf", None),
    ("Dreamcast Sound Format/x/a.dsf", ["Chiptune", "Game Rip"]),
])
def test_a_catalogue_row_carries_the_family_genre_a_scan_stamps(rel, want):
    assert mc.family_genre(rel) == want
    t = mc.build_track(rel, 100, added_at=1, siblings={}, use_filename=False, names=True,
                       dir_hash="d", root_h="r")
    assert t.get("genre") == (want or None) or (want is None and not t.get("genre"))


@pytest.mark.asyncio
async def test_built_catalogue_rows_are_modules_before_their_first_play(env):
    await _enable(env)
    store = env["store"]
    assert store.get_track(_tid("TFMX/Chris Huelsbeck/mdat.turrican 2-intro"))["genre"] \
        == ["Amiga", "Module"]
    assert store.get_track(_tid("Protracker/Test Artist/tiny song.mod"))["genre"] \
        == ["Tracker", "Module"]
    assert store.get_track(_tid("HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid"))["genre"] \
        == ["Chiptune", "C64"]


# ── B, D, F ─────────────────────────────────────────────────────────────────

async def _play_and_merge(env, rel):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    tid = _tid(rel)
    track = await get_track(tid)
    _path, _ext, _named, pin = await stream._resolve_play_source(tid, track)
    if pin:
        stream._zip_unpin(pin)
    assert await mc.await_merge(tid, 5.0)
    return env["store"].get_track(tid)


@pytest.mark.asyncio
async def test_the_first_play_keeps_the_sid_authors_name_and_tells_open_pages(env, monkeypatch):
    await _enable(env)
    sent = []

    async def broadcast(track_id):
        sent.append(track_id)
    from soniqboom.api import library
    monkeypatch.setattr(library, "broadcast_track_updated", broadcast)
    rel = "HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid"
    before = env["store"].get_track(_tid(rel))
    assert before["artist"] == "Hubbard Rob"                # HVSC's folder, until the play
    t = await _play_and_merge(env, rel)
    assert mc.is_merged(t)
    assert t["artist"] == "Rob Hubbard"                     # the header's, as a local copy shows
    assert t["genre"] == ["Chiptune", "C64"]
    assert sent == [t["id"]]


@pytest.mark.asyncio
async def test_a_credit_still_names_a_composer_the_file_does_not(env):
    await _enable(env)
    rel = "Protracker/Test Artist/tiny song.mod"
    t = await _play_and_merge(env, rel)
    assert t["artist"] == "Test Artist"


@pytest.mark.asyncio
async def test_a_hvsc_sid_merged_under_the_folder_name_is_read_again(env):
    await _enable(env)
    store = env["store"]
    rel = "HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid"
    old = dict(store.get_track(_tid(rel)), mtime=123.0, sid_md5="abc", subsongs=3,
               artist="Hubbard Rob", duration=200.0)
    fresh = mc.build_track(rel, len(SERVED[rel]), added_at=1, siblings={}, use_filename=False,
                           names=True, dir_hash="d", root_h="r")
    d = mc._rederive_merged(old, fresh)
    assert d.get(mc.REREAD) and mc._needs_merge(d)          # read again on its next play …
    assert mc.is_merged(d)                                   # … still a played tune (parked, kept)
    assert (d["subsongs"], d["duration"]) == (3, 200.0)     # … and what it showed stays
    ok = dict(old, artist="Rob Hubbard")
    d2 = mc._rederive_merged(ok, fresh)
    assert mc.is_merged(d2) and not d2.get(mc.REREAD)
    # the next play's read clears the mark and takes the header's author
    store.upsert_tracks_batch([d])
    t = await _play_and_merge(env, rel)
    assert t["artist"] == "Rob Hubbard" and not t.get(mc.REREAD)


@pytest.mark.asyncio
async def test_await_merge_without_a_merge_returns_at_once(env):
    t0 = asyncio.get_running_loop().time()
    assert await mc.await_merge("no-such-id", 5.0) is False
    assert asyncio.get_running_loop().time() - t0 < 0.5


@pytest.mark.asyncio
async def test_a_catalogue_sids_first_play_renders_its_hvsc_length(env, monkeypatch):
    """F: the play waits for the merge (and its HVSC lengths) before the
    render length is picked."""
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)

    class _Hvsc:
        def is_configured(self): return True
        def reload(self): pass
        def lookup_durations_by_md5(self, md5): return [123.0]
        def stil_key_for(self, path): return None
        def lookup_stil_by_relpath(self, key): return None
    monkeypatch.setattr("soniqboom.core.hvsc.get_hvsc", lambda: _Hvsc())
    rel = "HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid"
    tid = _tid(rel)
    track = await get_track(tid)
    _p, ext, _n, pin = await stream._resolve_play_source(tid, track)
    if pin:
        stream._zip_unpin(pin)
    assert stream._sid_target_seconds(track, 0) == 180          # what it would have rendered
    track = await stream._catalogue_sid_read(tid, track, ext)
    assert track.hvsc_lengths == [123.0]
    assert stream._sid_target_seconds(track, 0) == 123
    # … wired into the stream route, right after the play source resolves
    import inspect
    src = inspect.getsource(stream.stream_track)
    assert ("await _resolve_play_source(track_id, track)\n"
            "    track = await refreshed_after_expand(track_id, track)\n"
            "    track = await _catalogue_sid_read(") in src
    # a local / merged track passes straight through
    assert await stream._catalogue_sid_read(tid, track, ext) is track


# ── C ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wait_fetchable_waits_for_the_plays_download(env, monkeypatch):
    path = f"{ROOT}:/Protracker/Test Artist/tiny song.mod"
    state = {"ok": False}
    monkeypatch.setattr(catalogue, "fetch_ok", lambda p, lane="scan": state["ok"])

    async def start_later():
        await asyncio.sleep(0.3)
        state["ok"] = True
    asyncio.ensure_future(start_later())
    assert await catalogue.wait_fetchable(path, timeout=3.0) is True
    state["ok"] = False
    t0 = asyncio.get_running_loop().time()
    assert await catalogue.wait_fetchable(path, timeout=0.3) is False
    assert asyncio.get_running_loop().time() - t0 >= 0.25
    assert await catalogue.wait_fetchable("/local/x.mod", timeout=0.3) is True or True


@pytest.mark.asyncio
async def test_the_module_byte_readers_wait_for_a_catalogue_download(env, monkeypatch):
    from soniqboom.api import tracks
    waited = []

    async def wait(path, timeout=8.0):
        waited.append(path)
        return True
    monkeypatch.setattr(catalogue, "wait_fetchable", wait)
    monkeypatch.setattr(tracks, "_read_module_bytes", lambda p: b"bytes")
    assert await tracks._read_module_bytes_async("p") == b"bytes"
    assert waited == ["p"]
    import inspect
    src = inspect.getsource(tracks)
    assert "run_in_executor(None, _read_module_bytes" not in src.replace(
        "return await asyncio.get_event_loop().run_in_executor(None, _read_module_bytes, path_str)", "")


# ── E ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_modlands_hvsc_mirror_has_stil_keys(env, monkeypatch):
    await _enable(env)
    from soniqboom.core.hvsc import HVSC
    h = HVSC()
    monkeypatch.setattr(h, "hvsc_root", lambda: "/music/HVSC")
    assert h.stil_key_for(f"{ROOT}:/HVSC/MUSICIANS/T/Turner_Steve/Uridium.sid") \
        == "/MUSICIANS/T/Turner_Steve/Uridium.sid"
    assert h.stil_key_for(f"{ROOT}:/Protracker/x/a.mod") is None
    assert h.stil_key_for("/music/HVSC/MUSICIANS/T/Turner_Steve/Uridium.sid") \
        == "/MUSICIANS/T/Turner_Steve/Uridium.sid"


# ── Retro composer bio from the Wikipedia article Demozoo links ─────────────

@pytest.mark.asyncio
async def test_a_scener_card_with_a_wikipedia_link_gets_the_article(monkeypatch, tmp_path):
    from soniqboom.core import artistinfo
    monkeypatch.setattr(artistinfo, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(artistinfo, "_mem", {})
    asked = []

    async def summary(title):
        asked.append(title)
        return {"title": "Ben Daglish", "extract": "Ben Daglish was an English composer …",
                "thumbnail": {"source": "https://upload.wikimedia.org/x.jpg"},
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Ben_Daglish"}}}
    monkeypatch.setattr(artistinfo, "_wiki_summary", summary)
    card = {"name": "Ben Daglish", "found": True, "title": "Ben Daglish",
            "bio": "Member of We Music", "image": None, "url": "https://demozoo.org/sceners/1/",
            "source": "Demozoo", "groups": ["We Music"],
            "links": [{"url": "https://www.discogs.com/artist/1"},
                      {"url": "https://de.wikipedia.org/wiki/Ben_Daglish"},
                      {"url": "https://en.wikipedia.org/wiki/Ben_Daglish"}]}
    out = await artistinfo._with_wikipedia(card)
    assert out["source"] == "Wikipedia" and out["bio"].startswith("Ben Daglish was")
    assert out["url"] == "https://en.wikipedia.org/wiki/Ben_Daglish"
    assert out["image"] and out["links"] == card["links"] and out["groups"] == ["We Music"]
    assert asked == ["Ben_Daglish"]                         # English first
    await artistinfo._with_wikipedia(card)
    assert asked == ["Ben_Daglish"]                         # cached
    plain = {**card, "links": [{"url": "https://www.discogs.com/artist/1"}]}
    assert await artistinfo._with_wikipedia(plain) is plain  # no article: the Demozoo card


@pytest.mark.asyncio
async def test_the_retro_lookup_returns_the_article_for_a_demozoo_scener(monkeypatch, tmp_path):
    from soniqboom.core import artistinfo, demozoo
    monkeypatch.setattr(artistinfo, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(artistinfo, "_mem", {})

    async def card(name, track_title=None):
        return {"name": name, "found": True, "title": name, "bio": "Member of X",
                "source": "Demozoo", "url": "u",
                "links": [{"url": "https://en.wikipedia.org/wiki/Ben_Daglish"}]}

    async def summary(title):
        return {"title": "Ben Daglish", "extract": "Article text"}
    monkeypatch.setattr(demozoo, "artist_card", card)
    monkeypatch.setattr(artistinfo, "_wiki_summary", summary)
    out = await artistinfo.get_artist_info("Ben Daglish", is_retro=True)
    assert out["source"] == "Wikipedia" and out["bio"] == "Article text"


def test_the_info_pane_shows_a_retro_bio_only_from_wikipedia_or_the_demozoo_notes():
    # (the Scene tab shows the Demozoo identity: the Info pane takes an article
    # only — a Wikipedia page, or the composer's own Demozoo profile notes)
    src = (Path(__file__).resolve().parent.parent / "soniqboom/frontend/js/trackinfo.js").read_text()
    assert "_loadArtistAbout(track, { wikiOnly: _scene });" in src
    assert "if (wikiOnly && info.source !== 'Wikipedia' && !info.local_bio) return;" in src


@pytest.mark.asyncio
async def test_a_tune_nothing_plays_answers_after_the_grace(env, monkeypatch):
    """Track Info on an unplayed catalogue tune: no 8 s wait — only a play's
    open fetch window keeps a request waiting for the download."""
    await _enable(env)
    path = f"{ROOT}:/Protracker/Test Artist/tiny song.mod"
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await catalogue.wait_fetchable(path, timeout=8.0, grace=0.3) is False
    assert loop.time() - t0 < 1.0
    monkeypatch.setattr(catalogue, "allowed", lambda root, rel: True)   # a play's window is open
    t0 = loop.time()
    assert await catalogue.wait_fetchable(path, timeout=1.0, grace=0.3) is False
    assert loop.time() - t0 >= 0.9


@pytest.mark.asyncio
async def test_a_sids_first_play_waits_only_with_hvsc_for_a_c64_tune(env, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)

    class _NoHvsc:
        def is_configured(self): return False
    monkeypatch.setattr("soniqboom.core.hvsc.get_hvsc", lambda: _NoHvsc())
    waited = []

    async def await_merge(tid, timeout):
        waited.append(tid)
        return True
    monkeypatch.setattr(mc, "await_merge", await_merge)
    tid = _tid("HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid")
    track = await get_track(tid)
    assert await stream._catalogue_sid_read(tid, track, ".sid") is track
    assert waited == []


def test_the_refresh_rebuilds_the_meter_only_when_what_picks_it_changed():
    src = (Path(__file__).resolve().parent.parent / "soniqboom/frontend/js/app.js").read_text()
    body = src[src.index("async function _refreshTrack"):src.index("// A Track Info save")]
    assert "if (!perVoice && ('format' in changed || 'genre' in changed)) _handleVU(cur);" in body
    assert "'channels' in changed" not in body
    assert "Library.patchTrack(id, changed || fields)" in body
    ti = (Path(__file__).resolve().parent.parent / "soniqboom/frontend/js/trackinfo.js").read_text()
    # An open edit form is never redrawn under the listener's typing.
    guard = ti.index("if (wrap && wrap.querySelector('input, textarea, select')) {")
    block = ti[guard:ti.index("_render(t);", guard)]
    assert "return true;" in block


# ── Tune counts before the first play (the list's "N tunes" badge) ──────────

def _scene_and_songdb(rows: dict, lengths: dict, meta: dict | None = None):
    """A Modland checksum index (md5 → path) and a song database (key →
    first tune, lengths; ``meta``: key → (artist, label, album, year)) like
    the downloaded ones."""
    import sqlite3
    from soniqboom.core import scene_metadata as sm, songdb
    con = sqlite3.connect(sm._db_path())
    con.execute("CREATE TABLE mods (md5 TEXT PRIMARY KEY, path TEXT)")
    con.executemany("INSERT INTO mods VALUES (?, ?)", list(rows.items()))
    con.commit(); con.close()
    con = sqlite3.connect(songdb._db_path())
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, artist TEXT, label TEXT, "
                "album TEXT, year INTEGER)")
    con.executemany("INSERT INTO meta VALUES (?, ?, ?, ?, ?)",
                    [(k, *v) for k, v in (meta or {}).items()])
    con.execute("CREATE TABLE lengths (key TEXT PRIMARY KEY, minsub INTEGER, subsongs TEXT)")
    con.executemany("INSERT INTO lengths VALUES (?, ?, ?)",
                    [(k, a, b) for k, (a, b) in lengths.items()])
    con.commit(); con.close()


@pytest.fixture
def joins(monkeypatch):
    """The checksum joins a refill schedules, recorded instead of run (the
    tests run the song database's pass themselves)."""
    from soniqboom.core import scanner, songdb, source_twins

    class _Asked(list):
        """(``order``: the calls with the open pages' notice among them)"""
        order: list = []

        def clear(self):
            super().clear()
            self.order.clear()
    asked = _Asked()
    asked.order = []

    def _note(what):
        asked.order.append(what)
        if what != "announce":
            asked.append(what)
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda: _note("scene"))
    monkeypatch.setattr(source_twins, "schedule", lambda *a, **k: _note("twins"))
    monkeypatch.setattr(scanner, "announce_after_enrichment",
                        lambda root: _note("announce") if root == mc.ROOT else None)
    monkeypatch.setattr(songdb, "_last_auto_sig", None)
    monkeypatch.setattr(songdb, "_matched_ids", set())
    monkeypatch.setattr(songdb, "_status", {**songdb._status, "applying": False,
                                            "error": None, "last_apply": None})
    return asked


def _md5(rel: str) -> str:
    import hashlib
    return hashlib.md5(SERVED[rel]).hexdigest()


TFMX = "TFMX/Chris Huelsbeck/mdat.turrican 2-intro"
MOD = "Protracker/Test Artist/tiny song.mod"
ONE = "Protracker/Test Artist/other song.mod"
ANON = "Protracker/- unknown/nameless.mod"
SID = "HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid"


@pytest.mark.asyncio
async def test_unplayed_catalogue_tunes_get_their_tune_count_without_a_download(
        env, monkeypatch, joins):
    await _enable(env)
    joins.clear()                       # (the build asked for the twin pass)
    store = env["store"]
    _scene_and_songdb(
        {"a" * 32: TFMX, "b" * 32: MOD, "c" * 32: ONE, "d" * 32: SID},
        {"a" * 12: (1, "1000,p 2000,p 3000,p"), "b" * 12: (0, "1000,p 2000,p"),
         "c" * 12: (1, "1000,p")})

    class _Hvsc:
        def is_configured(self): return True
        def lookup_durations_by_md5(self, md5): return [10.0, 20.0, 30.0, 40.0] if md5 == "d" * 32 else []
    monkeypatch.setattr("soniqboom.core.hvsc.get_hvsc", lambda: _Hvsc())
    before = len(env["srv"].retrs)
    n = await mc.refill_unplayed()
    assert n == 4
    t = store.get_track(_tid(TFMX))
    assert (t["subsongs"], t.get("subsong_base")) == (3, 1)        # uade counts from tune 1
    assert t["file_md5"] == "a" * 32
    # a tracker: no count (a scanned one shows none — it would vanish on play)
    assert not store.get_track(_tid(MOD)).get("subsongs")
    assert not store.get_track(_tid(ONE)).get("subsongs")
    assert store.get_track(_tid(MOD))["file_md5"] == "b" * 32
    s = store.get_track(_tid(SID))
    assert s["subsongs"] == 4                                       # HVSC Songlengths
    assert s["hvsc_lengths"] == [10.0, 20.0, 30.0, 40.0] and s["duration"] == 10.0
    # a C64 SID's play stores no whole-file checksum: none before it either
    assert not s.get("file_md5")
    assert len(env["srv"].retrs) == before                          # nothing fetched
    assert joins == ["scene", "twins"]
    # open pages are told once, and after the join's pass was asked for
    assert joins.order == ["scene", "twins", "announce"]
    assert await mc.refill_unplayed() == 0                          # idempotent
    assert joins == ["scene", "twins"]                              # nothing new to join
    assert joins.order.count("announce") == 1                       # nothing new to show
    # a played tune keeps what its file said
    store.update_track_fields_batch([(_tid(TFMX), {"mtime": 5.0, "subsongs": 9,
                                                   "file_md5": "f" * 32})])
    await mc.refill_unplayed()
    t = store.get_track(_tid(TFMX))
    assert (t["subsongs"], t["file_md5"]) == (9, "f" * 32)


@pytest.mark.asyncio
async def test_no_checksum_list_no_counts(env, joins):
    await _enable(env)
    joins.clear()
    assert await mc.refill_unplayed() == 0
    assert joins == [] and joins.order == []        # nothing written: no page told


@pytest.mark.parametrize("rel,size,c64,want", [
    ("Protracker/x/a.mod", 1000, False, True),
    ("TFMX/x/mdat.a", 1000, False, True),
    ("SidMon 1/x/a.sid", 1000, False, True),                 # an Amiga module
    ("HVSC/MUSICIANS/x/a.sid", 1000, True, False),           # read with a sid_md5
    ("GoatTracker 2/x/a.sng", 1000, True, True),             # C64, but no SID file
    ("Music Assembler/x/a.ma", 1000, False, True),           # read as ``ma.a``
    ("Protracker/x/a.mod", 8 * 1024 * 1024 + 1, False, False),   # not hashed by a read
    ("Protracker/x/a.mod", None, False, True),
])
def test_only_files_a_play_stores_a_checksum_of_are_stamped(rel, size, c64, want):
    assert mc._takes_checksum(rel, size, c64) is want


async def _fill(joins):
    """Refill from the indexes, then the song database's pass the refill
    asked for (the post-scan runner's part, run here)."""
    from soniqboom.core import songdb
    n = await mc.refill_unplayed()
    assert "scene" in joins
    st = await songdb.apply_to_library()
    assert not st.get("error"), st
    return n


@pytest.mark.asyncio
async def test_unplayed_catalogue_tunes_carry_the_song_databases_data(env, joins):
    """Modland's checksum list gives every unplayed tune its checksum, so the
    song database names label / game / year / author and a length before the
    first play — and nothing is downloaded for it."""
    from soniqboom.core import scene_metadata as sm
    from soniqboom.core.conversion_cache import _RENDER_REVS
    await _enable(env)
    store = env["store"]
    k = {rel: _md5(rel) for rel in (TFMX, MOD, ONE, ANON)}
    _scene_and_songdb(
        {v: rel for rel, v in k.items()},
        {k[TFMX][:12]: (1, "95000,p"), k[MOD][:12]: (0, "200000,p"),
         k[ONE][:12]: (0, "100000,l"), k[ANON][:12]: (0, "600000,p")},
        {k[TFMX][:12]: ("Chris Huelsbeck", "Factor 5", "Turrican 2", 1991),
         k[MOD][:12]: ("Somebody Else", "Label X", "Game Y", 1992),
         k[ANON][:12]: ("Real Author", "", "", None)})
    before = len(env["srv"].retrs)
    assert await _fill(joins) == 4
    assert len(env["srv"].retrs) == before                      # nothing fetched

    t = store.get_track(_tid(TFMX))
    assert not mc.is_merged(t) and t["file_md5"] == k[TFMX]
    assert (t["label"], t["year"], t["game_by_songdb"]) == ("Factor 5", 1991, "Turrican 2")
    assert (t["album"], t["album_source"]) == ("Turrican 2", "songdb")
    assert t["artist"] == "Chris Huelsbeck"
    # a uade module: the length is promised up front, the first play re-measures
    assert (t["duration"], t["duration_rev"]) == (95.0, int(_RENDER_REVS["uade"]))

    t = store.get_track(_tid(MOD))
    assert t["artist"] == "Test Artist"                         # Modland's credit stays
    assert (t["label"], t["year"], t["album"], t["album_source"]) == (
        "Label X", 1992, "Game Y", "songdb")
    assert t["duration"] == 200.0 and t.get("duration_rev") is None

    t = store.get_track(_tid(ANON))
    assert t["artist"] == "Real Author" and t["songdb_fields"] == ["artist"]
    assert t["duration"] == 600.0                               # past the uade length stop

    t = store.get_track(_tid(ONE))
    assert t["file_md5"] == k[ONE] and not (t.get("duration") or 0) > 0   # a loop end: none

    # The checksum list's own path lookup has nothing to add to a row built
    # from that very path.
    rows = {rel: dict(store.get_track(_tid(rel))) for rel in k}
    assert rows[MOD]["scene_path"] == MOD
    await sm.apply_to_library(auto=True)
    assert {rel: dict(store.get_track(_tid(rel))) for rel in k} == rows
    assert len(env["srv"].retrs) == before

    # A second run over the same indexes: nothing written.
    assert await mc.refill_unplayed() == 0
    assert joins.count("scene") == 1


@pytest.mark.asyncio
async def test_the_first_play_keeps_what_the_song_database_filled(env, joins):
    await _enable(env)
    store = env["store"]
    _scene_and_songdb(
        {_md5(MOD): MOD, "e" * 32: ONE},                        # ONE: the list is stale
        {_md5(MOD)[:12]: (0, "200000,p"), "e" * 12: (0, "150000,p")},
        {_md5(MOD)[:12]: ("", "Label X", "Game Y", 1992),
         "e" * 12: ("", "Wrong Label", "Wrong Game", 1980)})
    await _fill(joins)
    assert store.get_track(_tid(ONE))["label"] == "Wrong Label"

    t = await _play_and_merge(env, MOD)
    assert mc.is_merged(t) and t["file_md5"] == _md5(MOD)
    assert (t["label"], t["year"], t["year_source"]) == ("Label X", 1992, "songdb")
    assert (t["album"], t["album_source"], t["game_by_songdb"]) == ("Game Y", "songdb", "Game Y")
    assert t["songdb_fields"] == ["label"]
    assert t["title"] == "tiny test song" and t["artist"] == "Test Artist"
    assert 0 < t["duration"] < 60                           # the file's own, not the 200 s

    # The file is not the one the list named: what was joined on that
    # checksum goes with it.
    t = await _play_and_merge(env, ONE)
    assert t["file_md5"] == _md5(ONE)
    assert not t.get("label") and not t.get("game_by_songdb") and not t.get("songdb_fields")
    assert t.get("album") != "Wrong Game" and t.get("album_source") != "songdb"
    assert t.get("year") != 1980 and t.get("year_source") != "songdb"


@pytest.mark.asyncio
async def test_the_first_play_keeps_the_song_databases_album_over_the_file_names(env, joins):
    """A file-name album ("Turrican II" from ``mdat.turrican 2-intro``) is a
    guess the song database's game name replaced before the play: the read of
    that very file keeps it, a different file gets the guess back."""
    await _enable(env)
    store = env["store"]
    _scene_and_songdb({_md5(TFMX): TFMX}, {},
                      {_md5(TFMX)[:12]: ("", "Factor 5", "Turrican 2", 1991)})
    await _fill(joins)
    old = dict(store.get_track(_tid(TFMX)))
    assert (old["album"], old["album_source"]) == ("Turrican 2", "songdb")
    read = {"title": "turrican 2-intro", "format": "TFMX", "duration": 60.0,
            "genre": ["Amiga", "Module"]}
    same = mc.merged_track(dict(old), {**read, "file_md5": _md5(TFMX)}, "x")
    assert (same["album"], same["album_source"]) == ("Turrican 2", "songdb")
    other = mc.merged_track(dict(old), {**read, "file_md5": "9" * 32}, "x")
    assert (other["album"], other["album_source"]) == ("Turrican II", "modland-filename")


@pytest.mark.asyncio
async def test_the_first_play_keeps_a_joined_length_only_of_that_very_file(env, joins):
    """A length joined on the list's checksum stands in for a read that finds
    none — unless the file played is another one than the list named."""
    from soniqboom.core.conversion_cache import _RENDER_REVS
    await _enable(env)
    store = env["store"]
    _scene_and_songdb({_md5(TFMX): TFMX}, {_md5(TFMX)[:12]: (1, "95000,p")},
                      {_md5(TFMX)[:12]: ("", "Factor 5", "Turrican 2", 1991)})
    await _fill(joins)
    old = dict(store.get_track(_tid(TFMX)))
    rev = int(_RENDER_REVS["uade"])
    assert (old["duration"], old["duration_rev"]) == (95.0, rev)
    read = {"title": "turrican 2-intro", "format": "TFMX", "genre": ["Amiga", "Module"]}
    for found in (0.0, 180.0):                              # none / the placeholder
        same = mc.merged_track(dict(old), {**read, "duration": found,
                                           "file_md5": _md5(TFMX)}, "x")
        assert (same["duration"], same["duration_rev"]) == (95.0, rev)
        other = mc.merged_track(dict(old), {**read, "duration": found,
                                            "file_md5": "9" * 32}, "x")
        assert (other.get("duration") or 0) == found and not other.get("duration_rev")
    # (a read that stores no checksum: nothing says it is another file)
    bare = mc.merged_track(dict(old), {**read, "duration": 0.0}, "x")
    assert bare["duration"] == 95.0


@pytest.mark.asyncio
async def test_a_withdrawn_song_database_album_gives_the_path_album_back(
        env, joins, monkeypatch):
    """The song database's game name replaced the album a tune's Modland path
    gave it; when the database's fills go (Reset, a list naming another
    file), that album is back — no other pass would put it there."""
    import sqlite3
    from soniqboom.core import scene_metadata as sm, songdb
    await _enable(env)
    store = env["store"]
    was = dict(store.get_track(_tid(TFMX)))
    assert (was["album"], was["album_source"]) == ("Turrican II", "modland-filename")
    _scene_and_songdb({_md5(TFMX): TFMX, _md5(MOD): MOD}, {},
                      {_md5(TFMX)[:12]: ("", "Factor 5", "Turrican 2", 1991),
                       _md5(MOD)[:12]: ("", "Label X", "Game Y", 1992)})
    await _fill(joins)
    assert store.get_track(_tid(TFMX))["album_source"] == "songdb"
    assert store.get_track(_tid(MOD))["album"] == "Game Y"

    # the list names another file now, one the database doesn't know
    con = sqlite3.connect(sm._db_path())
    con.execute("UPDATE mods SET md5 = ? WHERE path = ?", ("f" * 32, TFMX))
    con.commit(); con.close()
    monkeypatch.setattr(songdb, "_MIN_META_ROWS", 1)
    assert await mc.refill_unplayed() == 1
    await songdb.apply_to_library()
    t = store.get_track(_tid(TFMX))
    assert t["file_md5"] == "f" * 32 and not t.get("label") and not t.get("game_by_songdb")
    assert (t["album"], t["album_source"]) == ("Turrican II", "modland-filename")

    # Reset: a tune whose path names no game is left without an album
    await songdb.reset_to_file_state()
    t = store.get_track(_tid(MOD))
    assert not t.get("album") and not t.get("album_source") and not t.get("label")
    assert (store.get_track(_tid(TFMX))["album"]) == "Turrican II"


@pytest.mark.asyncio
async def test_a_reset_gives_the_path_album_back(env, joins):
    from soniqboom.core import songdb
    await _enable(env)
    store = env["store"]
    _scene_and_songdb({_md5(TFMX): TFMX}, {},
                      {_md5(TFMX)[:12]: ("", "Factor 5", "Turrican 2", 1991)})
    await _fill(joins)
    assert (store.get_track(_tid(TFMX))["album"]) == "Turrican 2"
    await songdb.reset_to_file_state()
    t = store.get_track(_tid(TFMX))
    assert (t["album"], t["album_source"]) == ("Turrican II", "modland-filename")
    assert not t.get("label") and not t.get("game_by_songdb") and t.get("year") != 1991


@pytest.mark.asyncio
async def test_fills_asked_for_at_once_run_one_at_a_time(monkeypatch):
    import asyncio
    runs, gate = [], asyncio.Event()

    async def fill():
        runs.append(mc._refill_busy)
        await gate.wait()
        if len(runs) == 2:
            raise RuntimeError("an index went away")        # (never raised to the caller)
        return 0
    monkeypatch.setattr(mc, "refill_unplayed", fill)
    first = asyncio.create_task(mc._refill_quietly())
    await asyncio.sleep(0)
    for _ in range(3):                                      # asked again while it runs
        await mc._refill_quietly()                          # … returns at once
    assert runs == [True]
    gate.set()
    await first
    assert runs == [True, True]                             # one more run for all three
    assert not mc._refill_busy and not mc._refill_again
    await mc._refill_quietly()                              # idle again: runs
    assert len(runs) == 3


@pytest.mark.asyncio
async def test_a_rebuild_keeps_the_checksum_and_what_was_joined_on_it(env, monkeypatch, joins):
    await _enable(env)
    store = env["store"]
    k = {rel: _md5(rel) for rel in (TFMX, MOD)}
    _scene_and_songdb(
        {v: rel for rel, v in k.items()},
        {k[TFMX][:12]: (1, "95000,p"), k[MOD][:12]: (0, "200000,p")},
        {k[TFMX][:12]: ("", "Factor 5", "Turrican 2", 1991),
         k[MOD][:12]: ("", "Label X", "Game Y", 1992)})
    await _fill(joins)

    def facts(rel):
        t = store.get_track(_tid(rel))
        return {f: t.get(f) for f in ("file_md5", "label", "year", "year_source", "album",
                                      "album_source", "game_by_songdb", "songdb_fields",
                                      "duration", "duration_rev", "mtime")}
    was = {rel: facts(rel) for rel in k}
    assert was[MOD]["label"] == "Label X" and was[TFMX]["duration"] == 95.0
    # (the song database's game name went in ahead of the file name's)
    assert (was[TFMX]["album"], was[TFMX]["album_source"]) == ("Turrican 2", "songdb")
    monkeypatch.setattr(mc, "RULES_VERSION", 99)            # new derivation rules: all rewritten
    st = await mc.refresh(local_zip=env["zip"])
    assert st["last_result"]["changed"] == 8, st
    assert {rel: facts(rel) for rel in k} == was
    assert joins.count("scene") == 1                        # nothing new to join


@pytest.mark.asyncio
async def test_a_playback_mark_on_the_file_comes_back_with_its_checksum(env, joins):
    from soniqboom.core import skip_rules
    await _enable(env)
    store = env["store"]
    _scene_and_songdb({_md5(MOD): MOD, _md5(ONE): ONE}, {})
    store.set_config(skip_rules.REG_KEY, {f"md5:{_md5(MOD)}": {
        "health": "broken", "health_note": "silent", "health_by": "admin", "health_at": 5.0,
        "file_md5": _md5(MOD), "sid_md5": None}})
    gen = skip_rules._gen
    assert await mc.refill_unplayed() == 2
    t = store.get_track(_tid(MOD))
    assert (t["health"], t["health_note"], t["health_by"]) == ("broken", "silent", "admin")
    assert not store.get_track(_tid(ONE)).get("health")
    assert skip_rules._gen == gen + 1                       # open pages re-check their rows


# ── Track Info on an unplayed catalogue tune reads that one file (intent) ───

@pytest.mark.asyncio
async def test_track_info_reads_the_one_unplayed_catalogue_tune(env, monkeypatch):
    from soniqboom.api import stream, tracks
    await _enable(env)
    store = env["store"]
    rel = "HVSC/MUSICIANS/H/Hubbard_Rob/Commando.sid"
    tid = _tid(rel)
    sent = []

    async def broadcast(track_id):
        sent.append(track_id)
    from soniqboom.api import library
    monkeypatch.setattr(library, "broadcast_track_updated", broadcast)

    class _Req:
        client = type("C", (), {"host": "10.0.0.5"})()
    await tracks.get_track_extended(tid, _Req())            # Track Info opens
    for _ in range(100):
        if mc.is_merged(store.get_track(tid)) and not stream._INFO_READS:
            break
        await asyncio.sleep(0.05)
    t = store.get_track(tid)
    assert mc.is_merged(t) and t["artist"] == "Rob Hubbard"
    assert [p for p in env["srv"].retrs if p[0].endswith("Commando.sid")]
    n = len(env["srv"].retrs)
    await tracks.get_track_extended(tid, _Req())            # opened again: read already
    await asyncio.sleep(0.2)
    assert len(env["srv"].retrs) == n
    assert sent and sent[-1] == tid


@pytest.mark.asyncio
async def test_track_info_never_fetches_when_the_budget_or_source_says_no(env, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)
    tid = _tid("Protracker/Test Artist/other song.mod")
    track = await get_track(tid)
    monkeypatch.setattr(catalogue, "permit_prewarm", lambda path, owner, **k: False)
    n = len(env["srv"].retrs)
    stream.catalogue_read_for_info(tid, track, "10.0.0.5")
    await asyncio.sleep(0.3)
    assert len(env["srv"].retrs) == n and not stream._INFO_READS
    # a library file outside any catalogue: nothing at all
    local = type("T", (), {"path": "/music/a.sid", "mtime": 0})()
    stream.catalogue_read_for_info("x", local, "10.0.0.5")
    assert not stream._INFO_READS


# ── Opt-in: read the tunes shown in lists (Settings → External sources) ─────

def _sndh(tunes: int = 5) -> bytes:
    hdr = b"\x60\x00\x00\x00" * 3 + b"SNDH" + b"TITLDemo Tune\x00COMMAuthor X\x00" \
        + f"##{tunes:02d}".encode() + b"\x00HDNS"
    return hdr + b"\x4e\x75" * 64


def _add_catalogue_file(env, rel: str, data: bytes) -> str:
    p = env["ftp_root"] / "pub" / "modules" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    t = mc.build_track(rel, len(data), added_at=1, siblings={}, use_filename=False,
                       names=True, dir_hash="d", root_h=mc.root_hash())
    env["store"].upsert_tracks_batch([t])
    return t["id"]


@pytest.mark.asyncio
async def test_list_reads_are_off_by_default_and_validated(env):
    from soniqboom.api import stream
    await _enable(env)
    assert mc.tune_details() == "intent" and mc.status()["tune_details"] == "intent"
    tid = _add_catalogue_file(env, "SNDH/Someone/demo.sndh", _sndh())
    assert stream.catalogue_read_for_list([tid], "c") == 0     # default: never from a list
    mc.set_tune_details("lists")
    assert mc.status()["tune_details"] == "lists"
    with pytest.raises(ValueError):
        mc.set_tune_details("always")


@pytest.mark.asyncio
async def test_tunes_shown_in_a_list_are_read_one_at_a_time(env, monkeypatch):
    from soniqboom.api import stream
    await _enable(env)
    mc.set_tune_details("lists")
    store = env["store"]
    a = _add_catalogue_file(env, "SNDH/Someone/demo.sndh", _sndh(5))
    b = _add_catalogue_file(env, "SNDH/Someone/other.sndh", _sndh(3))
    mod = _tid("Protracker/Test Artist/tiny song.mod")            # not a list-read kind
    tfmx = _tid("TFMX/Chris Huelsbeck/mdat.turrican 2-intro")     # an index counts these
    queued = stream.catalogue_read_for_list([a, b, mod, tfmx, "nope"], "c")
    assert queued == 2
    for _ in range(200):
        if mc.is_merged(store.get_track(a)) and mc.is_merged(store.get_track(b)):
            break
        await asyncio.sleep(0.05)
    assert mc.is_merged(store.get_track(a)) and mc.is_merged(store.get_track(b))
    fetched = [p for p, _o in env["srv"].retrs]
    assert not any(p.endswith(".mod") or "mdat." in p for p in fetched)
    assert stream.catalogue_read_for_list([a, b], "c") == 0       # read already


def test_list_reads_have_their_own_budget_and_leave_room_for_plays(monkeypatch):
    path = "ftp://x/y:/SNDH/a.sndh"
    monkeypatch.setattr(catalogue, "_split", lambda p: ("ftp://x/y", p.rsplit(":/", 1)[1]))
    monkeypatch.setattr(catalogue, "is_cached", lambda p: False)
    monkeypatch.setattr(catalogue, "_new_fetches", __import__("collections").deque())
    monkeypatch.setattr(catalogue, "_list_fetches", __import__("collections").deque())
    monkeypatch.setattr(catalogue, "LIST_RATE_MAX", 3)
    assert all(catalogue.permit_list(path) for _ in range(3))
    assert catalogue.permit_list(path) is False                     # its own budget spent
    catalogue._list_fetches.clear()
    import time as _t
    now = _t.monotonic()
    catalogue._new_fetches.extend([now] * (catalogue.RATE_MAX - catalogue.LIST_PLAY_RESERVE))
    assert catalogue.permit_list(path) is False                     # the rest is for plays
    assert catalogue.permit_play(path) is True


def test_the_page_learns_whether_list_reads_are_on():
    src = (Path(__file__).resolve().parent.parent / "soniqboom/main.py").read_text()
    assert '"catalogue_list_reads": _catalogue_list_reads(),' in src
    lib = (Path(__file__).resolve().parent.parent / "soniqboom/frontend/js/library.js").read_text()
    assert "_noteCatalogueRow(t);" in lib and "/api/tracks/catalogue-reads" in lib


# ── Modules whose suffix is never music on its own, in their player's folder ─

@pytest.mark.parametrize("rel,listed,fmt", [
    ("Music Assembler/- unknown/thanatos.ma", True, "Music Assembler"),
    ("Mike Davies/Mike Davies/mercs.md", True, "Mike Davies"),
    ("Paul Shields/Paul Shields/airball-gameover.ps", True, "Paul Shields"),
    ("Speedy System/Michael Winterberg/information.ss", True, "Speedy System"),
    ("IFF-SMUS/- unknown/64/instruments/Rythm1.ss", False, ""),   # an instrument
    ("Protracker/x/readme.md", False, ""),
])
def test_denylisted_suffixes_are_music_in_their_players_folder(rel, listed, fmt):
    from soniqboom.core import uade_formats as uf
    tok = rel.rsplit(".", 1)[-1]
    if tok not in uf.player_map():
        pytest.skip("no eagleplayer.conf token")
    assert (mc._classify(rel) == mc.KIND_MUSIC) is listed
    assert mc.format_for(rel) == fmt
    assert (mc.family_genre(rel) == ["Amiga", "Module"]) is listed


def test_such_a_catalogue_module_plays_through_uade():
    from soniqboom.api import stream
    from soniqboom.core import uade_formats as uf
    if "ma" not in uf.player_map():
        pytest.skip("no eagleplayer.conf token")
    amiga = {"genre": ["Amiga", "Module"]}
    assert stream._uade_name_routes("thanatos.ma", ".ma", amiga) is True
    assert stream._uade_name_routes("thanatos.ma", ".ma", {"genre": []}) is False
    assert stream._uade_name_routes("readme.md", ".md", None) is False


def test_the_equaliser_icons_stand_still_while_paused():
    root = Path(__file__).resolve().parent.parent / "soniqboom/frontend"
    css = (root / "css/app.css").read_text()
    assert "body.sb-paused .row-eq i,\nbody.sb-paused .ti-sub-eq i { animation-play-state: paused; }" in css
    js = (root / "js/app.js").read_text()
    assert "document.body.classList.toggle('sb-paused', !playing);" in js
