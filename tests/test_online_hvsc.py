# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The online HVSC collection (``core/online_catalog`` + ``core/online_hvsc``).

Served by a local HTTP server holding a tiny ``C64Music`` tree: HVSC's
``DOCUMENTS/Songlengths.md5`` (the index) and ``STIL.txt``, and the SIDs.
Covers: the index → library rows (lengths, tune counts, checksums, STIL,
credit, genre) without any SID read; listings / stats from the index; the
fetch gate (no read outside a play); a play's fetch + first-play merge; a
conditional refresh (304 = nothing downloaded again); a changed index (added /
removed tunes, playlist entries parked); the documents serving the user's own
SIDs (lengths by md5, STIL by md5 for a SID outside HVSC); switch-off; and
Modland leaving its HVSC folder out.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import struct
import time
import threading
from pathlib import Path

import pytest

from soniqboom.core import catalogue, online_catalog, online_hvsc, remote_cache
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _psid(title: bytes, songs: int = 1, start: int = 1) -> bytes:
    h = bytearray(0x7C)
    h[0:4] = b"PSID"
    struct.pack_into(">HHHHHHH", h, 4, 2, 0x7C, 0, 0x1000, 0x1003, songs, start)
    h[0x16:0x16 + len(title)] = title
    h[0x36:0x36 + 11] = b"Rob Hubbard"
    h[0x56:0x56 + 4] = b"1985"
    return bytes(h) + b"\x60" * 64


SIDS = {
    "MUSICIANS/H/Hubbard_Rob/Commando.sid": _psid(b"Commando", songs=3),
    "MUSICIANS/H/Hubbard_Rob/Monty_on_the_Run.sid": _psid(b"Monty on the Run"),
    "GAMES/A-F/Arkanoid.sid": _psid(b"Arkanoid", songs=2),
}
LENS = {"MUSICIANS/H/Hubbard_Rob/Commando.sid": "4:39 0:03 1:02",
        "MUSICIANS/H/Hubbard_Rob/Monty_on_the_Run.sid": "5:51",
        "GAMES/A-F/Arkanoid.sid": "0:52 0:04"}
STIL = """### STIL header ###

/MUSICIANS/H/Hubbard_Rob/Commando.sid
  COMMENT: The famous one.

/GAMES/A-F/Arkanoid.sid
  COMMENT: Breakout.
"""


def _md5(rel: str) -> str:
    return hashlib.md5(SIDS[rel]).hexdigest()


def _songlengths(rels) -> str:
    out = ["[Database]"]
    for rel in rels:
        out += [f"; /{rel}", f"{_md5(rel)}={LENS[rel]}"]
    return "\n".join(out) + "\n"


class _Handler(http.server.SimpleHTTPRequestHandler):
    log: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).log.append(("GET", self.path))
        return super().do_GET()

    def do_HEAD(self):
        type(self).log.append(("HEAD", self.path))
        return super().do_HEAD()


def _write_tree(root: Path, rels) -> None:
    docs = root / "C64Music" / "DOCUMENTS"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "Songlengths.md5").write_text(_songlengths(rels), "latin-1")
    (docs / "STIL.txt").write_text(STIL, "latin-1")
    for rel in rels:
        p = root / "C64Music" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(SIDS[rel])


@pytest.fixture
def env(tmp_data_dir, tmp_path, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.config import settings
    store = TrackStore()
    monkeypatch.setattr(store_mod, "_store", store)
    cache = remote_cache.RemoteCache(tmp_data_dir / "remote-cache", 512)
    monkeypatch.setattr(remote_cache, "_cache", cache)
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(settings, "hvsc_docs_path", "")
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    web = tmp_path / "web"
    _write_tree(web, list(SIDS))
    _Handler.log = []
    handler = lambda *a, **kw: _Handler(*a, directory=str(web), **kw)   # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/C64Music"
    cat = online_hvsc.HVSC_ONLINE
    monkeypatch.setattr(online_hvsc, "BASE", base)
    monkeypatch.setattr(cat, "root", base)
    monkeypatch.setattr(cat, "min_rows", 1)
    monkeypatch.setattr(cat, "_status", dict(cat._status, state="off", error=None))
    monkeypatch.setattr(cat, "_job_lock", None)
    monkeypatch.setattr(cat, "_tasks", set())
    monkeypatch.setattr(cat, "_job_tasks", set())
    monkeypatch.setattr(cat, "_refresh_task", None)
    monkeypatch.setattr(cat, "_local", threading.local())
    monkeypatch.setattr(cat, "_stil", None)
    monkeypatch.setattr(cat, "_learned", None)
    monkeypatch.setattr(online_hvsc, "_reconcile_task", None)
    from soniqboom.core import subsonic_state
    subsonic_state.reset_state(tmp_path / "subsonic_state.json")
    from soniqboom.core.hvsc import get_hvsc
    get_hvsc().configure(None)
    yield {"store": store, "web": web, "cat": cat, "base": base}
    srv.shutdown()
    catalogue.unregister_root(base)
    from soniqboom.core.filesource import remove_source
    remove_source(base)
    get_hvsc().configure(None)


async def _enable(env) -> dict:
    cat = env["cat"]
    st = await cat.enable()
    assert st["state"] == "ready", st
    return st


async def _reconcile_settled() -> None:
    """Let a reconcile the catalogue scheduled itself (``schedule_reconcile``
    — spawned via ``call_soon_threadsafe``) finish, so an explicit run in a
    test doesn't race it."""
    for _ in range(3):
        await asyncio.sleep(0.05)
        t = online_hvsc._reconcile_task
        if t is not None and not t.done():
            await t


def _sid_gets():
    return [p for m, p in _Handler.log if m == "GET" and p.endswith(".sid")]


@pytest.mark.asyncio
async def test_the_index_lists_every_tune_without_reading_one(env):
    st = await _enable(env)
    cat, store = env["cat"], env["store"]
    assert st["tunes"] == 3 and st["index_rows"] == 3
    assert _sid_gets() == []                                  # no SID read
    t = store.get_track(cat.track_id("MUSICIANS/H/Hubbard_Rob/Commando.sid"))
    assert t["path"] == f"{env['base']}:/MUSICIANS/H/Hubbard_Rob/Commando.sid"
    assert t["format"] == "SID" and t["genre"][:2] == ["Chiptune", "C64"]
    assert t["title"] == "Commando" and t["artist"] == "Hubbard Rob"
    assert t["hvsc_lengths"] == [279.0, 3.0, 62.0] and t["subsongs"] == 3
    assert t["duration"] == 279.0
    assert t["sid_md5"] == _md5("MUSICIANS/H/Hubbard_Rob/Commando.sid")
    assert "famous" in t["stil"]
    single = store.get_track(cat.track_id("MUSICIANS/H/Hubbard_Rob/Monty_on_the_Run.sid"))
    assert "subsongs" not in single and single["hvsc_lengths"] == [351.0]
    assert catalogue.is_catalogue_root(env["base"])


@pytest.mark.asyncio
async def test_listings_and_stats_come_from_the_index_and_reads_are_gated(env):
    from soniqboom.core.filesource import get_source
    await _enable(env)
    src = get_source(env["base"])
    names = [e.name for e in src.list_dir("/MUSICIANS/H/Hubbard_Rob")]
    assert names == ["Commando.sid", "Monty_on_the_Run.sid"]
    assert src.is_dir("/GAMES") and not src.is_dir("/NOPE")
    assert src.is_dir("/")                                   # the share watcher's probe
    assert src.stat("/GAMES/A-F/Arkanoid.sid").size == 0          # no size, no request
    with pytest.raises(catalogue.CatalogueFetchRefused):
        src.read_file("/GAMES/A-F/Arkanoid.sid")
    assert _Handler.log == [("GET", "/C64Music/DOCUMENTS/Songlengths.md5"),
                            ("GET", "/C64Music/DOCUMENTS/STIL.txt")]


@pytest.mark.asyncio
async def test_a_play_fetches_the_sid_and_merges_it(env):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)
    cat, store = env["cat"], env["store"]
    rel = "GAMES/A-F/Arkanoid.sid"
    tid = cat.track_id(rel)
    path, _ext, _n, pin = await stream._resolve_play_source(tid, await get_track(tid))
    if pin:
        stream._zip_unpin(pin)
    assert Path(path).read_bytes() == SIDS[rel]
    assert _sid_gets() == [f"/C64Music/{rel}"]
    for _ in range(100):
        if (store.get_track(tid).get("mtime") or 0) > 0:
            break
        await asyncio.sleep(0.05)
    t = store.get_track(tid)
    assert t["mtime"] > 0 and t["id"] == tid and t["path"].startswith(env["base"])
    assert t["hvsc_lengths"] == [52.0, 4.0] and "Breakout" in t["stil"]
    await stream._resolve_play_source(tid, await get_track(tid))       # cached: no 2nd GET
    assert _sid_gets() == [f"/C64Music/{rel}"]


@pytest.mark.asyncio
async def test_refresh_downloads_nothing_new_when_the_index_is_unchanged(env):
    await _enable(env)
    cat = env["cat"]
    _Handler.log = []
    st = await cat.refresh(manual=True)
    assert st["state"] == "ready" and st["last_result"]["index"] == {"unchanged": True}
    assert [m for m, _p in _Handler.log] == ["GET"]               # Songlengths only, a 304


@pytest.mark.asyncio
async def test_a_changed_index_adds_and_removes_and_parks_playlists(env):
    import os
    import time as _time
    await _enable(env)
    cat, store = env["cat"], env["store"]
    gone = "GAMES/A-F/Arkanoid.sid"
    store.create_playlist("pl1", "mine", track_ids=[cat.track_id(gone)])
    _write_tree(env["web"], ["MUSICIANS/H/Hubbard_Rob/Commando.sid",
                             "MUSICIANS/H/Hubbard_Rob/Monty_on_the_Run.sid"])
    sl = env["web"] / "C64Music" / "DOCUMENTS" / "Songlengths.md5"
    later = _time.time() + 10
    os.utime(sl, (later, later))
    monkey_min = cat.min_rows
    st = await cat.refresh(manual=True)
    # A third of the tunes gone: refused once (kept as it was) …
    assert st["shrink_refused"] and st["tunes"] == 3 and "Refresh now" in st["error"]
    st = await cat.refresh(manual=True)                       # … taken on the next one
    assert cat.min_rows == monkey_min
    assert st["last_result"]["removed"] == 1 and st["tunes"] == 2
    assert store.get_track(cat.track_id(gone)) is None
    from soniqboom.core.store import PARKED_PLAYLIST_IDS
    assert cat.track_id(gone) in (store.get_config(PARKED_PLAYLIST_IDS) or [])


@pytest.mark.asyncio
async def test_its_documents_serve_the_users_own_sids(env, tmp_path):
    from soniqboom.core.hvsc import get_hvsc
    await _enable(env)
    hv = get_hvsc()
    assert hv.is_configured() and hv.docs_path == str(env["cat"].dir() / "DOCUMENTS")
    md5 = _md5("MUSICIANS/H/Hubbard_Rob/Commando.sid")
    assert hv.lookup_durations_by_md5(md5) == [279.0, 3.0, 62.0]
    # A SID kept outside any HVSC tree finds its STIL entry by checksum.
    own = tmp_path / "my music" / "commando copy.sid"
    own.parent.mkdir()
    own.write_bytes(SIDS["MUSICIANS/H/Hubbard_Rob/Commando.sid"])
    assert "famous" in hv.lookup_stil(own, md5=md5)["text"]
    # The option off: the lookups go back to "no HVSC".
    env["cat"].set_option(online_hvsc.OPT_LOCAL_DOCS, False)
    assert not hv.is_configured()
    env["cat"].set_option(online_hvsc.OPT_LOCAL_DOCS, True)
    assert hv.is_configured()


@pytest.mark.asyncio
async def test_a_local_hvsc_folder_wins_over_the_downloaded_documents(env, monkeypatch, tmp_path):
    from soniqboom.config import settings
    from soniqboom.core.hvsc import get_hvsc
    local = tmp_path / "HVSC" / "DOCUMENTS"
    local.mkdir(parents=True)
    monkeypatch.setattr(settings, "hvsc_docs_path", str(local))
    get_hvsc().configure(str(local))
    await _enable(env)
    assert get_hvsc().docs_path == str(local)


@pytest.mark.asyncio
async def test_switch_off_removes_the_tunes_and_the_root(env):
    from soniqboom.core.hvsc import get_hvsc
    await _enable(env)
    cat = env["cat"]
    st = await cat.disable()
    assert st["state"] == "off" and cat.tune_count() == 0
    assert not catalogue.is_catalogue_root(env["base"])
    assert env["base"] not in env["store"]._scan_dirs
    assert not get_hvsc().is_configured()


def test_modland_leaves_its_hvsc_folder_out_while_the_option_is_on(tmp_data_dir, monkeypatch):
    from soniqboom.core import modland_catalog as mc
    store = TrackStore()
    monkeypatch.setattr(store_mod, "_store", store)
    assert "HVSC" not in mc._left_out_tops(mc.SCOPE_ALL)
    store.set_config("online_hvsc_enabled", True)
    # Switched on but without its tunes (6581.nl unreachable): Modland's stay.
    monkeypatch.setattr(online_hvsc.HVSC_ONLINE, "tune_count", lambda: 0)
    assert "HVSC" not in mc._left_out_tops(mc.SCOPE_ALL)
    monkeypatch.setattr(online_hvsc.HVSC_ONLINE, "tune_count", lambda: 61157)
    assert "HVSC" in mc._left_out_tops(mc.SCOPE_ALL)
    assert "HVSC" in mc._left_out_tops(mc.SCOPE_STANDARD)
    store.set_config("online_hvsc_replace_modland", False)
    assert "HVSC" not in mc._left_out_tops(mc.SCOPE_ALL)


def test_songlengths_parsing_keeps_paths_md5s_and_lengths():
    rows = list(online_hvsc.parse_songlengths(_songlengths(list(SIDS))))
    assert [r[0] for r in rows] == list(SIDS)
    assert rows[0][1] == _md5("MUSICIANS/H/Hubbard_Rob/Commando.sid")
    assert rows[0][2] == [279.0, 3.0, 62.0]


def test_the_registry_finds_the_owner_of_a_path():
    cat = online_catalog.owner_of(f"{online_hvsc.HVSC_ONLINE.root}:/GAMES/x.sid")
    assert cat is online_hvsc.HVSC_ONLINE
    assert online_catalog.owner_of("ftp://ftp.modland.com/pub/modules:/HVSC/x.sid") is None


@pytest.mark.asyncio
async def test_switching_hvsc_off_applies_modland_again(env, monkeypatch):
    # after_disable runs in a worker thread: the Modland re-apply is handed to
    # the event loop (and waits while Modland is busy).
    from soniqboom.core import modland_catalog as mc
    started, busy = [], [True]

    async def fake_reapply():
        started.append(True)
        return {}
    monkeypatch.setattr(mc, "enabled", lambda: True)
    monkeypatch.setattr(mc, "busy", lambda: busy[0])
    monkeypatch.setattr(mc, "reapply", fake_reapply)
    monkeypatch.setattr(online_hvsc, "_reapply_task", None)
    cat = env["cat"]
    monkeypatch.setattr(cat, "_loop", asyncio.get_running_loop())
    await asyncio.get_running_loop().run_in_executor(None, cat.after_disable)
    await asyncio.sleep(0.3)
    assert started == []                                    # Modland busy: it waits
    busy[0] = False
    for _ in range(40):
        if started:
            break
        await asyncio.sleep(0.1)
    assert started == [True]


def test_a_merge_without_documents_keeps_the_index_lengths():
    cat = online_hvsc.HVSC_ONLINE
    old = {"hvsc_lengths": [279.0, 3.0, 62.0], "duration": 279.0, "subsongs": 3}
    d = {"duration": 180.0, "start_subsong": 1}                 # the read's default length
    d = cat.merged_fields(d, old, "MUSICIANS/H/Hubbard_Rob/Commando.sid")
    assert d["duration"] == 279.0 and d["hvsc_lengths"] == [279.0, 3.0, 62.0] and d["subsongs"] == 3
    d = cat.merged_fields({"duration": 3.0, "start_subsong": 2, "hvsc_lengths": [279.0, 3.0, 62.0]},
                          old, "x.sid")
    assert d["duration"] == 3.0                                  # read with documents: kept


def test_songlengths_fractions_are_decimal_seconds():
    from soniqboom.core.hvsc import _parse_duration
    assert _parse_duration("0:03.5") == 3.5
    assert _parse_duration("1:02.25") == 62.25
    assert _parse_duration("4:39.123") == 279.123
    assert _parse_duration("0:52") == 52.0



@pytest.mark.asyncio
async def test_a_play_teaches_the_composers_own_name_to_the_folder(env):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)
    cat, store = env["cat"], env["store"]
    monty = cat.track_id("MUSICIANS/H/Hubbard_Rob/Monty_on_the_Run.sid")
    assert store.get_track(monty)["artist"] == "Hubbard Rob"
    tid = cat.track_id("MUSICIANS/H/Hubbard_Rob/Commando.sid")
    _p, _e, _n, pin = await stream._resolve_play_source(tid, await get_track(tid))
    if pin:
        stream._zip_unpin(pin)
    for _ in range(100):
        if (store.get_track(tid).get("mtime") or 0) > 0:
            break
        await asyncio.sleep(0.05)
    assert store.get_track(tid)["artist"] == "Rob Hubbard"           # the file's own author
    assert store.get_track(monty)["artist"] == "Rob Hubbard"         # its folder follows
    # … and every later build of the folder's tunes (a refresh, a restart).
    cat._learned = None
    t = cat.build_track("MUSICIANS/H/Hubbard_Rob/Monty_on_the_Run.sid", 0, None,
                        added_at=1, dir_hash="d", root_h="r")
    assert t["artist"] == "Rob Hubbard"
    arkanoid = store.get_track(cat.track_id("GAMES/A-F/Arkanoid.sid"))
    assert arkanoid.get("artist") != "Rob Hubbard"                    # (another folder)


def test_who_counts_as_the_folders_composer():
    sc = online_hvsc.same_composer
    assert sc("Hubbard Rob", "Rob Hubbard")
    assert sc("Wyndex", "Stephen L. Judd (Wyndex)")
    assert sc("Judd Stephen", "Stephen L. Judd")
    assert sc("Tel Jeroen", "Jeroen Tel")
    assert not sc("Hubbard Rob", "Rob Hubbard & Ben Daglish")       # a collaboration
    assert not sc("Hubbard Rob", "Ben Daglish")
    assert not sc("Hubbard Rob", "Rob Hubbard/Ben Daglish")
    assert not sc("Galway Martin", "Martin Galway, Rob Hubbard")
    # Spelled with letters HVSC's folder names write out.
    assert sc("Huelsbeck Chris", "Chris Hülsbeck")
    assert sc("Oesterby Jens", "Jens Østerby")
    assert sc("Judd Stephen", "Stephen L. Judd (Wyndex)")         # a name folder, its handle
    # Not the folder's composer on its own: a remark, a second name, more words.
    assert not sc("Daglish Ben", "Ben Daglish (remix)")
    assert not sc("Hubbard Rob", "Rob Hubbard (Ben Daglish)")
    assert not sc("Hubbard Rob", "Rob Hubbard (1987)")
    assert not sc("Hubbard Rob", "DJ Rob Hubbard")
    assert not sc("Hubbard Rob", "Rob Hubbard 87")
    assert online_hvsc.composer_dir("MUSICIANS/H/Hubbard_Rob/x.sid") == "MUSICIANS/H/Hubbard_Rob"
    assert online_hvsc.composer_dir("GAMES/A-F/Arkanoid.sid") is None


@pytest.mark.asyncio
async def test_user_data_follows_the_hvsc_copy_in_the_library(env):
    from soniqboom.core import modland_catalog as mc
    await _enable(env)
    await _reconcile_settled()
    cat, store = env["cat"], env["store"]
    rel = "MUSICIANS/H/Hubbard_Rob/Commando.sid"
    mid, oid = mc.track_id("HVSC/" + rel), cat.track_id(rel)
    # Rated and played as Modland's copy (now left out of the library).
    store.set_rating(mid, 5)
    store.record_play(mid, uid="")
    store.create_playlist("pl", "mine", track_ids=[mid])
    store.store_waveform(oid, [0.5, 0.25])          # (computed for the copy: no user data)
    other = "MUSICIANS/H/Hubbard_Rob/Monty_on_the_Run.sid"
    store.set_rating(mc.track_id("HVSC/" + other), 2)
    store.set_rating(cat.track_id(other), 4)                       # both copies rated
    store.create_playlist("pl2", "both", track_ids=[mc.track_id("HVSC/" + other)])
    res = await online_hvsc.reconcile_user_data()
    assert res["moved"] == 1 and res["conflicts"] == 1
    assert store.get_rating(oid) == 5 and store.get_play_stats(oid, uid="").get("count") == 1
    assert not store.get_rating(mid)
    pl = next(p for p in store.list_playlists() if p.get("id") == "pl")
    ids = [e.get("id") if isinstance(e, dict) else e for e in pl["track_ids"]]
    assert ids == [oid]
    assert store.get_rating(cat.track_id(other)) == 4                # nothing overwritten
    assert store.get_rating(mc.track_id("HVSC/" + other)) == 2
    pl2 = next(p for p in store.list_playlists() if p.get("id") == "pl2")
    assert [e.get("id") if isinstance(e, dict) else e
            for e in pl2["track_ids"]] == [cat.track_id(other)]     # its entry follows still
    # Again: nothing more to move.
    assert (await online_hvsc.reconcile_user_data())["moved"] == 0
    # The other way round: the online copy leaves, Modland's is there (the
    # switch-off schedules the move itself; an explicit run finds it done).
    await cat.disable()
    store.upsert_tracks_batch([{"id": mid, "path": f"{mc.ROOT}:/HVSC/{rel}", "title": "Commando"}])
    await online_hvsc.reconcile_user_data()
    if online_hvsc._reconcile_task is not None:
        await online_hvsc._reconcile_task
    assert store.get_rating(mid) == 5 and not store.get_rating(oid)
    assert store.get_play_stats(mid, uid="").get("count") == 1


@pytest.mark.asyncio
async def test_plays_follow_the_hvsc_copy_per_user(env):
    """Plays alone are user data that moves; where one user played both
    copies, that user's plays (and history) stay on each copy while another
    user's move."""
    from soniqboom.core import modland_catalog as mc
    await _enable(env)
    await _reconcile_settled()
    cat, store = env["cat"], env["store"]
    both = "MUSICIANS/H/Hubbard_Rob/Commando.sid"
    only = "MUSICIANS/H/Hubbard_Rob/Monty_on_the_Run.sid"
    mid, oid = mc.track_id("HVSC/" + both), cat.track_id(both)
    m_only, o_only = mc.track_id("HVSC/" + only), cat.track_id(only)
    store.record_play(m_only, uid="u1")                              # plays only, no rating
    store.record_play(m_only, uid="u1")
    store.record_play(mid, uid="u1")
    store.record_play(mid, uid="u2")
    for _ in range(7):
        store.record_play(oid, uid="u2")                             # u2 played both copies
    store.push_history({"track_id": mid, "ts": 1}, uid="u1")
    store.push_history({"track_id": mid, "ts": 2}, uid="u2")
    res = await online_hvsc.reconcile_user_data()
    assert res == {"moved": 1, "conflicts": 1}
    assert store.get_play_stats(o_only, uid="u1")["count"] == 2
    assert store.get_play_stats(m_only, uid="u1")["count"] == 0
    assert store.get_play_stats(oid, uid="u1")["count"] == 1        # u1: moved
    assert store.get_history(5, uid="u1")[0]["track_id"] == oid
    assert store.get_play_stats(oid, uid="u2")["count"] == 7        # u2: each copy its own
    assert store.get_play_stats(mid, uid="u2")["count"] == 1
    assert store.get_history(5, uid="u2")[0]["track_id"] == mid
    # Nothing left to move: a second run does nothing (and writes nothing).
    await _reconcile_settled()
    writes: list = []
    real = store._aof_append
    store._aof_append = lambda op, **kw: writes.append(op)
    try:
        assert await online_hvsc.reconcile_user_data() == {"moved": 0, "conflicts": 0}
    finally:
        store._aof_append = real
    assert writes == []


@pytest.mark.asyncio
async def test_credits_are_learned_from_tunes_played_before(env):
    """A tune played before its folder's credit was learned (or with the
    credits file lost) still teaches it — at the next apply — and the folder's
    unplayed tunes follow; nothing is labelled a Modland path from its path."""
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("MUSICIANS/H/Hubbard_Rob/Commando.sid")
    monty = cat.track_id("MUSICIANS/H/Hubbard_Rob/Monty_on_the_Run.sid")
    assert "scene_path" not in store.get_track(tid)
    # Played before any learning: its own author, the folder's other tune not.
    store.update_track_fields_batch([(tid, {"artist": "Rob Hubbard", "mtime": time.time()})])
    cat.credits_path().unlink(missing_ok=True)
    cat._learned = None
    assert store.get_track(monty)["artist"] == "Hubbard Rob"
    assert await cat.learn_from_library() == 1
    assert store.get_track(monty)["artist"] == "Rob Hubbard"
    assert cat._credits() == {"MUSICIANS/H/Hubbard_Rob": "Rob Hubbard"}
    assert await cat.learn_from_library() == 0           # (learned once)


def test_a_group_members_folder_names_the_member_and_a_group_teaches_none(monkeypatch):
    from types import SimpleNamespace as NS
    cd, fc = online_hvsc.composer_dir, online_hvsc.folder_credit
    assert cd("MUSICIANS/0-9/20CC/Paul_Falco/Tune.sid") == "MUSICIANS/0-9/20CC/Paul_Falco"
    assert fc("MUSICIANS/0-9/20CC/Paul_Falco/Tune.sid") == "Paul Falco"
    assert cd("MUSICIANS/0-9/20CC/Paul_Falco/Worktunes/x.sid") == "MUSICIANS/0-9/20CC/Paul_Falco"
    assert cd("MUSICIANS/C/Crowley_Owen/Worktunes/x.sid") == "MUSICIANS/C/Crowley_Owen"
    assert cd("MUSICIANS/H/Hubbard_Rob/Commando.sid") == "MUSICIANS/H/Hubbard_Rob"
    cat = online_hvsc.HVSC_ONLINE
    monkeypatch.setattr(cat, "list_dir", lambda d: [NS(name="Paul_Falco", is_dir=True),
                                                    NS(name="Worktunes", is_dir=True),
                                                    NS(name="Group.sid", is_dir=False)])
    assert cat.is_group_folder("MUSICIANS/0-9/20CC")
    monkeypatch.setattr(cat, "list_dir", lambda d: [NS(name="Worktunes", is_dir=True)])
    assert not cat.is_group_folder("MUSICIANS/C/Crowley_Owen")
    # A tune of the group itself, its file naming one member in "Name (Group)":
    # the group folder learns no member's name.
    monkeypatch.setattr(cat, "is_group_folder", lambda d: True)
    learned = []
    monkeypatch.setattr(cat, "_learn_credit", lambda d, a: learned.append((d, a)))
    monkeypatch.setattr(online_hvsc, "folder_credit", lambda rel: "20CC")
    cat.after_merge("t", {"artist": "Paul Falco (20CC)"},
                    {"path": cat.track_path("MUSICIANS/0-9/20CC/Group.sid")})
    assert learned == []


@pytest.mark.asyncio
async def test_learning_credits_from_many_played_folders_is_one_pass(env, monkeypatch, tmp_path):
    import time as _t
    cat, store = env["cat"], env["store"]
    await _enable(env)
    monkeypatch.setattr(cat, "is_group_folder", lambda d: False)
    monkeypatch.setattr(cat, "_learned", {})
    monkeypatch.setattr(cat, "credits_path", lambda: tmp_path / "credits.json")
    rows = []
    for f in range(800):
        for k in range(76):
            rel = f"MUSICIANS/X/Name{f:03d}_Comp/T{k}.sid"
            rows.append({"id": cat.track_id(rel), "path": cat.track_path(rel), "title": "t",
                         "artist": (f"Comp Name{f:03d}" if k == 0 else f"Name{f:03d} Comp"),
                         "mtime": 1.0 if k == 0 else 0.0, "scan_root_hash": cat.root_hash()})
    store.upsert_tracks_batch(rows)
    t0 = _t.perf_counter()
    assert await cat.learn_from_library() == 800
    took = _t.perf_counter() - t0
    assert took < 30, took                           # (one pass: ~2 s; ~38 s the old way)
    assert store.get_track(cat.track_id("MUSICIANS/X/Name007_Comp/T5.sid"))["artist"] \
        == "Comp Name007"


@pytest.mark.asyncio
async def test_a_group_members_tune_is_credited_to_the_member(env):
    await _enable(env)
    cat = env["cat"]
    cat._learned = {}
    t = cat.build_track("MUSICIANS/0-9/20CC/Paul_Falco/Tune.sid", 0, None,
                        added_at=1, dir_hash="d", root_h="r")
    assert t["artist"] == "Paul Falco"
    t = cat.build_track("MUSICIANS/C/Crowley_Owen/Worktunes/x.sid", 0, None,
                        added_at=1, dir_hash="d", root_h="r")
    assert t["artist"] != "Worktunes"

