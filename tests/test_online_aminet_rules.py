# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Aminet rules beyond listing and opening (``core/online_aminet``):

* MPEG audio ``.mpg`` uploads are listed, play transcoded until their first
  play reads the layer, then Layer III plays as MP3 (MP2 stays transcoded);
* a re-upload under the same name and rounded size is noticed by its upload
  day (INDEX day minus age) — the opened package folds back, its cached copy
  goes, the next play opens the new upload; an upload aging (or past the age
  cap) is no change;
* a collection's readme Author (a compiler) is no composer — the description's
  composer is ("14 Ron Klaren modules!");
* a prefix-named module (``mod.Opus``) opens as the module it is;
* the format lists leave unopened packages out;
* an index read by older rules is downloaded again on its next refresh.
"""
from __future__ import annotations

import asyncio
import http.server
import io
import os
import threading
import time
import zipfile
from pathlib import Path

import pytest

from soniqboom.core import catalogue, online_aminet, remote_cache
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _mod(title: bytes) -> bytes:
    return title.ljust(20, b"\0") + b"\0" * 1060 + b"M.K." + b"\0" * 1024


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _mpeg(layer: int, frames: int = 80) -> bytes:
    """MPEG-1 frames, 44.1 kHz, 128 kbps, layer 1/2/3 (silent)."""
    b1 = {3: 0xFB, 2: 0xFD, 1: 0xFF}[layer]
    b2 = {3: 0x90, 2: 0x80, 1: 0x40}[layer]
    size = 136 if layer == 1 else 417
    return (bytes([0xFF, b1, b2, 0x00]) + b"\0" * (size - 4)) * frames


def _mpeg_titled(title: str) -> bytes:
    """Layer III frames behind an ID3v2 tag naming ``title``."""
    import tempfile
    from mutagen.id3 import ID3, TIT2
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "x.mp3"
        f.write_bytes(_mpeg(3))
        tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        tags.save(f)
        return f.read_bytes()


DAY = 20375                                  # 14-Oct-2025 (days since 1970)


def _date(day: int) -> str:
    return time.strftime("%d-%b-%Y", time.gmtime(day * 86400))


SITE = {
    "sets/14RK_Mods.zip": (_zip({"mod.Astra_1": _mod(b"Astra 1"), "mod.Astra_2": _mod(b"Astra 2")}),
                           "14 Ron Klaren modules! (Use DT/EP)"),
    "sets/Gryzor.zip": (_zip({"mod.Oldie": _mod(b"Oldie")}), "Set of modules composed by Gryzor"),
    "techn/Opus.zip": (_zip({"mod.Opus": _mod(b"Opus")}), "Techno mod by Skope - 4ch"),
    "techn/Re.zip": (_zip({"mod.Re": _mod(b"First upload")}), "Re-upload test"),
    "mpg/Song.mpg": (_mpeg_titled("Song.mpg"), "MP3 by Someone"),
    "mpg/Old.mpg": (_mpeg(2), "MP2 by Somebody"),
}
README = {
    "sets/14RK_Mods.readme": "Short: 14 Ron Klaren modules!\nAuthor: Mikael Lund\n",
    "sets/Gryzor.readme": "Short: Set of modules composed by Gryzor\nAuthor: Gryzor\n",
    "techn/Re.readme": "Short: Re-upload test\nAuthor: Skope\n",
}
TREE = ("<li>mods/sets - Module collections (340 packages)</li>"
        "<li>mods/techn - Techno music (1737 packages)</li>"
        "<li>mods/mpg - Music in MPEG audio format (505 packages)</li>")


class _Handler(http.server.SimpleHTTPRequestHandler):
    log: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).log.append(("GET", self.path, self.headers.get("If-None-Match"),
                               self.headers.get("If-Modified-Since")))
        return super().do_GET()

    def do_HEAD(self):
        type(self).log.append(("HEAD", self.path, None, None))
        return super().do_HEAD()


def _write(web: Path, *, day: int = DAY, ages: dict | None = None, files: dict | None = None):
    files = files or SITE
    ages = ages or {}
    rows = ["|", f"| Aminet index, created on {_date(day)}", "|"]
    for rel, (data, desc) in files.items():
        cat, name = rel.split("/", 1)
        rows.append(f"{name:<30} mods/{cat:<5} {max(1, len(data) // 1024)}K "
                    f"{ages.get(rel, 999)} {desc}")
        p = web / "mods" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (web / "INDEX").write_text("\n".join(rows) + "\n", "latin-1")
    later = time.time() + 5 + len(_Handler.log)
    os.utime(web / "INDEX", (later, later))
    (web / "tree").write_text(TREE, "utf-8")
    for rel, text in README.items():
        (web / "mods" / rel).write_text(text, "latin-1")


@pytest.fixture
def env(tmp_data_dir, tmp_path, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.config import settings
    store = TrackStore()
    monkeypatch.setattr(store_mod, "_store", store)
    cache = remote_cache.RemoteCache(tmp_data_dir / "remote-cache", 512)
    monkeypatch.setattr(remote_cache, "_cache", cache)
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    web = tmp_path / "web"
    web.mkdir()
    _Handler.log = []
    _write(web, ages={"techn/Re.zip": 5})
    handler = lambda *a, **kw: _Handler(*a, directory=str(web), **kw)   # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{srv.server_address[1]}"
    cat = online_aminet.AMINET_ONLINE
    monkeypatch.setattr(online_aminet, "SITE", site)
    monkeypatch.setattr(online_aminet, "INDEX_URL", f"{site}/INDEX")
    monkeypatch.setattr(online_aminet, "TREE_URL", f"{site}/tree?path=mods")
    monkeypatch.setattr(cat, "root", f"{site}/mods")
    monkeypatch.setattr(cat, "min_rows", 1)
    monkeypatch.setattr(cat, "_status", dict(cat._status, state="off", error=None))
    for attr, val in (("_job_lock", None), ("_tasks", set()), ("_job_tasks", set()),
                      ("_refresh_task", None), ("_local", threading.local()),
                      ("_cats", None), ("_pkg_locks", {}), ("_expanded_at", {}),
                      ("_merge_tasks", {}), ("_merging", set())):
        monkeypatch.setattr(cat, attr, val)
    yield {"store": store, "web": web, "cat": cat}
    srv.shutdown()
    catalogue.unregister_root(cat.root)
    from soniqboom.core.filesource import remove_source
    remove_source(cat.root)


async def _play(tid):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    path, ext, _n, pin = await stream._resolve_play_source(tid, await get_track(tid))
    if pin:
        stream._zip_unpin(pin)
    return path, ext


async def _merged(store, tid):
    for _ in range(100):
        if (store.get_track(tid).get("mtime") or 0) > 0:
            return store.get_track(tid)
        await asyncio.sleep(0.05)
    raise AssertionError("not merged")


@pytest.mark.asyncio
async def test_mpeg_audio_uploads_play_and_layer_iii_plays_as_mp3(env):
    from soniqboom.api import stream
    cat, store = env["cat"], env["store"]
    st = await cat.enable()
    assert st["state"] == "ready", st
    tid = cat.track_id("mpg/Song.mpg")
    t = store.get_track(tid)
    assert t["format"] == "MPEG audio"
    assert stream._render_ident(t["path"], t) == (".mpg", False)     # transcoded until read
    path, ext = await _play(tid)
    assert ext == ".mpg" and Path(path).read_bytes()[:3] == b"ID3"
    t = await _merged(store, tid)
    assert t["format"] == "MP3" and t["duration"] > 1.5
    assert t["title"] == "Song"                # (its tag only repeats the file name)
    assert stream._render_ident(t["path"], t) == (".mp3", False)     # now served as is
    old = cat.track_id("mpg/Old.mpg")
    await _play(old)
    t = await _merged(store, old)
    assert t["format"] == "MP2" and stream._render_ident(t["path"], t)[0] == ".mpg"


@pytest.mark.asyncio
async def test_a_collections_readme_author_is_no_composer(env):
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("sets/14RK_Mods.zip")
    await _play(tid)
    rows = [t for t in store._tracks.values() if "14RK_Mods.zip::" in t["path"]]
    assert len(rows) == 2 and {t["artist"] for t in rows} == {"Ron Klaren"}
    g = cat.track_id("sets/Gryzor.zip")
    await _play(g)
    assert store.get_track(g)["artist"] == "Gryzor"        # the readme's, named in the description


@pytest.mark.asyncio
async def test_a_prefix_named_module_opens_as_the_module(env):
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("techn/Opus.zip")
    path, ext = await _play(tid)
    assert ext == ".mod" and Path(path).read_bytes()[:4] == b"Opus"
    assert store.get_track(tid)["path"].endswith("Opus.zip::mod.Opus.mod")
    assert store.get_track(tid)["artist"] == "Skope"


@pytest.mark.asyncio
async def test_a_re_upload_of_the_same_size_is_noticed_by_its_upload_day(env):
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("techn/Re.zip")
    path, _ext = await _play(tid)
    assert Path(path).read_bytes()[:12] == b"First upload"
    assert "::" in store.get_track(tid)["path"]
    # A week later, the same file aged a week: no change.
    st = await (_rewrite(env, day=DAY + 7, ages={"techn/Re.zip": 12}))
    assert st["state"] == "ready" and "::" in store.get_track(tid)["path"]
    # Re-uploaded (age 0) with the same rounded size: folds back, opens anew.
    files = dict(SITE)
    files["techn/Re.zip"] = (_zip({"mod.Re": _mod(b"Second uploa")}), "Re-upload test")
    assert len(files["techn/Re.zip"][0]) == len(SITE["techn/Re.zip"][0])
    st = await _rewrite(env, day=DAY + 7, ages={"techn/Re.zip": 0}, files=files)
    assert st["state"] == "ready", st
    assert store.get_track(tid)["path"] == f"{cat.root}:/techn/Re.zip"
    path, _ext = await _play(tid)
    assert Path(path).read_bytes()[:12] == b"Second uploa"


async def _rewrite(env, **kw):
    _write(env["web"], **kw)
    return await env["cat"].refresh(manual=True)


def test_upload_day_change_rule():
    rc = online_aminet.AMINET_ONLINE.row_changed
    old = {"approx": 2048, "up": DAY - 5}
    assert not rc(0, old, 0, {"approx": 2048, "up": DAY - 5})
    assert not rc(0, old, 0, {"approx": 2048, "up": DAY - 4})      # (a day of slack)
    assert rc(0, old, 0, {"approx": 2048, "up": DAY})               # re-uploaded
    assert rc(0, old, 0, {"approx": 3072, "up": DAY - 5})           # another size
    assert not rc(0, {"approx": 2048, "up": DAY - 998}, 0, {"approx": 2048, "up": 0})   # aged past the cap
    assert rc(0, {"approx": 2048, "up": 0}, 0, {"approx": 2048, "up": DAY})             # 999+ → recent
    assert not rc(0, {"approx": 2048}, 0, {"approx": 2048, "up": DAY})                 # an older index


def test_the_format_lists_leave_unopened_packages_out():
    store = TrackStore()
    store.upsert_tracks_batch([
        {"id": "a", "path": "/x/a.mod", "title": "a", "format": "ProTracker"},
        {"id": "b", "path": "https://h/m:/b.zip", "title": "b", "format": "ZIP package"},
        {"id": "c", "path": "https://h/m:/c.lha", "title": "c", "format": "LHA package"},
    ])
    fmts = {f["format"] for f in store.aggregate_formats()}
    assert fmts == {"ProTracker"}


@pytest.mark.asyncio
async def test_an_index_read_by_older_rules_is_downloaded_again(env):
    cat = env["cat"]
    await cat.enable()
    await asyncio.get_running_loop().run_in_executor(None, cat._touch_info, {"rules": "1"})
    _Handler.log = []
    st = await cat.refresh(manual=True)
    assert st["state"] == "ready"
    gets = [x for x in _Handler.log if x[0] == "GET" and x[1] == "/INDEX"]
    assert gets and gets[0][2] is None and gets[0][3] is None       # unconditional
    assert cat.index_info().get("rules") == str(cat.index_rules)



@pytest.mark.asyncio
async def test_a_package_listed_by_older_naming_rules_reopens_from_its_cached_copy(env):
    import sqlite3
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("techn/Opus.zip")
    await _play(tid)
    con = sqlite3.connect(cat.packages_path())
    con.execute("UPDATE packages SET marker = substr(marker, 1, instr(marker, '#') - 1)")
    con.commit()
    con.close()
    st = await cat.reapply()                        # (listed by other rules: one entry again)
    assert st["state"] == "ready" and store.get_track(tid)["path"].endswith("techn/Opus.zip")
    _Handler.log = []
    path, ext = await _play(tid)
    assert ext == ".mod" and store.get_track(tid)["path"].endswith("::mod.Opus.mod")
    assert not [x for x in _Handler.log if x[0] == "GET" and x[1].endswith("Opus.zip")]


@pytest.mark.asyncio
async def test_track_info_on_a_big_mpeg_upload_reads_its_layer_by_ranges(env, monkeypatch):
    """Too big to fetch whole: its head says the layer (this test server
    answers no range, so the tail read is refused and the head is all)."""
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    cat, store = env["cat"], env["store"]
    await cat.enable()
    monkeypatch.setattr(stream, "_CATALOGUE_WHOLE_MAX", 10_000)
    monkeypatch.setattr(stream, "_TAG_HEAD", 8 * 1024)
    monkeypatch.setattr(stream, "_TAG_TAIL", 8 * 1024)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", set())
    for rel, fmt in (("mpg/Old.mpg", "MP2"), ("mpg/Song.mpg", "MP3")):
        tid = cat.track_id(rel)
        title = store.get_track(tid)["title"]
        stream.catalogue_read_for_info(tid, await get_track(tid), "tester")
        for _ in range(100):
            if tid in stream._RANGED_INFO_DONE:
                break
            await asyncio.sleep(0.05)
        t = store.get_track(tid)
        assert t["format"] == fmt and not (t.get("mtime") or 0), (rel, t.get("format"))
        assert t["title"] == title                 # (Song's tag only repeats its file name)
    t = store.get_track(cat.track_id("mpg/Old.mpg"))
    assert stream._render_ident(t["path"], t)[0] == ".mpg"           # MP2: still transcoded
    t = store.get_track(cat.track_id("mpg/Song.mpg"))
    assert stream._render_ident(t["path"], t) == (".mp3", False)


@pytest.mark.asyncio
async def test_a_member_reopened_under_its_new_name_keeps_its_user_data(env):
    """A package opened by older member naming rules (``mod.Opus`` read by
    its suffix) folds back on the next apply; opened again, the member is
    ``mod.Opus.mod`` — its rating, plays and playlist entry follow it."""
    import json
    import sqlite3
    from soniqboom.core.store import PARKED_PLAYLIST_IDS
    cat, store = env["cat"], env["store"]
    await cat.enable()
    files = dict(SITE)
    files["techn/Two.zip"] = (_zip({"song.mod": _mod(b"Song"), "mod.Opus": _mod(b"Opus two")}),
                              "Two by Skope")
    st = await _rewrite(env, files=files)
    assert st["state"] == "ready", st
    tid = cat.track_id("techn/Two.zip")
    await _play(tid)
    new = cat.track_id("techn/Two.zip::mod.Opus.mod")
    old = cat.track_id("techn/Two.zip::mod.Opus")
    assert new in store._tracks and store._tracks[tid]["path"].endswith("::song.mod")
    # As the older rules opened it: the member under its name reading.
    con = sqlite3.connect(cat.packages_path())
    marker, members = con.execute("SELECT marker, members FROM packages WHERE pkg = ?",
                                  ("techn/Two.zip",)).fetchone()
    members = [["mod.Opus" if m[0] == "mod.Opus.mod" else m[0], *m[1:]]
               for m in json.loads(members)]
    con.execute("UPDATE packages SET marker = ?, members = ? WHERE pkg = ?",
                (marker.split("#n", 1)[0], json.dumps(members), "techn/Two.zip"))
    con.commit()
    con.close()
    row = dict(store._tracks[new], id=old, path=cat.track_path("techn/Two.zip::mod.Opus"))
    store.delete_track_ids([new])
    store.upsert_tracks_batch([row])
    store.set_rating(old, 4)
    store.record_play(old, uid="")
    store.set_rating(tid, 2)
    store.create_playlist("pl", "Mix", [tid, old])
    st = await cat.reapply()                            # one entry again (parked)
    assert st["state"] == "ready" and old not in store._tracks
    assert old in (store.get_config(PARKED_PLAYLIST_IDS) or [])
    await _play(tid)                                    # opened by the current rules
    assert new in store._tracks and old not in store._tracks
    assert store.get_rating(new) == 4 and store.get_play_stats(new, uid="")["count"] == 1
    assert store.get_rating(old) == 0 and store.get_rating(tid) == 2
    assert store._playlists["pl"]["track_ids"] == [tid, new]
    assert old not in (store.get_config(PARKED_PLAYLIST_IDS) or [])


@pytest.mark.asyncio
async def test_what_was_made_from_a_changed_file_goes_and_nothing_else(tmp_path, monkeypatch,
                                                                         tmp_data_dir):
    """A file that changed at its source: its cached copy and ZIP subsets
    fetched before the change, its in-memory blocks, its renders, transcodes,
    failure / silence notes, VU sidecars and waveform go — a copy fetched
    since, and other files' renders, stay."""
    from soniqboom.config import settings
    from soniqboom.core import conversion_cache as cc, remote_zip
    from soniqboom.core.remote_cache import RemoteCache, subset_name
    root = "https://aminet.invalid/mods"
    cache = RemoteCache(tmp_path / "rc", 64)
    for rel in ("/a.zip", subset_name("/a.zip", "x.mod", 10, 0), "/b.zip"):
        f = tmp_path / "f.bin"
        f.write_bytes(b"PK..")
        cache.put_file(root, rel, f)
    for key in list(cache._index):
        cache._index[key]["fetched"] = 1000.0
    cache._index[cache._cache_key(root, "/b.zip")]["fetched"] = 3000.0    # since the change
    assert cache.drop_if_older(root, "/a.zip", 2000.0) == 2     # the copy + its subset
    assert cache.get_cached(root, "/a.zip") is None
    assert cache.find_subset(root, "/a.zip", "x.mod") is None
    assert cache.drop_if_older(root, "/b.zip", 2000.0) == 0     # fetched after: kept
    assert cache.get_cached(root, "/b.zip") is not None

    remote_zip.clear_blocks()
    with remote_zip._blocks_lock:
        remote_zip._blocks[(root, "/a.zip", 0)] = b"x" * 10
        remote_zip._blocks[(root, "/a.zip", 1)] = b"x" * 10
        remote_zip._blocks[(root, "/b.zip", 0)] = b"y" * 7
        remote_zip._blocks_bytes = 27
    assert remote_zip.forget_blocks(root, "/a.zip") == 2
    assert list(remote_zip._blocks) == [(root, "/b.zip", 0)] and remote_zip._blocks_bytes == 7
    remote_zip.clear_blocks()

    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(cc, "_meta", {})
    monkeypatch.setattr(cc, "_lru", type(cc._lru)())
    a, b = "aaaa-1111", "bbbb-2222"
    made = {}
    for tid in (a, b):
        d = tmp_path / "conv" / "tracker" / tid[:2]
        d.mkdir(parents=True, exist_ok=True)
        for key in (f"{tid}__sub0", f"{tid}__sub0__flac__vabc"):
            p = d / f"{key}.wav"
            p.write_bytes(b"RIFF")
            cc._meta[key] = {"path": str(p), "size_bytes": 4, "format_type": "tracker"}
            made[key] = p
        (d / f"{tid}.vu").write_bytes(b"v")
        cc._recent_failures[f"{tid}__sub0"] = (0.0, 1, "x")
        cc._silent_keys[f"{tid}__sub0"] = 0.0
    assert await cc.purge_tracks({a}) == 2
    assert set(cc._meta) == {f"{b}__sub0", f"{b}__sub0__flac__vabc"}
    assert not made[f"{a}__sub0"].exists() and made[f"{b}__sub0"].exists()
    assert not (tmp_path / "conv" / "tracker" / a[:2] / f"{a}.vu").exists()
    assert (tmp_path / "conv" / "tracker" / b[:2] / f"{b}.vu").exists()
    assert f"{a}__sub0" not in cc._recent_failures and f"{b}__sub0" in cc._recent_failures
    assert f"{a}__sub0" not in cc._silent_keys and f"{b}__sub0" in cc._silent_keys
    cc._recent_failures.pop(f"{b}__sub0", None)
    cc._silent_keys.pop(f"{b}__sub0", None)

    store = TrackStore()
    store.store_waveform(a, [0.5])
    store.store_waveform(b, [0.5])
    assert store.forget_waveforms([a]) == 1 and store.get_waveform(b) == [0.5]


def test_names_cut_short_by_the_index_and_one_spelling_per_composer(monkeypatch):
    am = online_aminet
    rows = [("a", {"desc": "Chip mod by HKvalhe"}), ("b", {"desc": "Ambient mod by HKvalhe"}),
            ("c", {"desc": "Chip mod by Hkvalhe"}),
            ("d", {"desc": "A very long melodic techno module by HKval"}),     # (cut)
            ("e", {"desc": "Old mod by Jogeir Liljedahl"}),
            ("f", {"desc": "Pop mod by Airon"}), ("g", {"desc": "Pop mod by Airwalk"})]
    names = am.known_composers(rows)
    assert "HKvalhe" in names and "Hkvalhe" not in names and "HKval" not in names
    assert am.complete_name("HKval", names) == "HKvalhe"
    assert am.complete_name("Jogeir Lil", names) == "Jogeir Liljedahl"
    assert am.complete_name("AiR", names) == ""                 # Airon or Airwalk: no guess
    assert am.complete_name("Nobody", names) == ""
    cat = am.AMINET_ONLINE
    cat.prepare_build()
    monkeypatch.setattr(cat, "_cats", {"airon": "Mods by Airon", "chip": "Chiptunes",
                                       "blakk": "Mods by Blakkhar"})
    monkeypatch.setattr(cat, "_names", sorted(names))
    monkeypatch.setattr(cat, "_spell", None)

    def artist(rel, desc):
        return cat.build_track(rel, 0, {"desc": desc}, added_at=0, dir_hash="",
                               root_h="").get("artist")
    long_ = "Some long description of an old song by "
    assert artist("chip/x.mod", long_[:-3] + "by HKval") == "HKvalhe"       # completed
    assert artist("airon/y.mod", long_ + "AiR") == "Airon"                 # its category's
    assert artist("chip/z.mod", long_ + "Zx") is None                      # too short to guess
    assert artist("chip/w.mod", "Chip mod by blakkhar") == "Blakkhar"      # one spelling


def test_an_upload_new_to_the_index_dates_from_its_upload_day():
    sf = online_aminet.AMINET_ONLINE.seen_for
    now = 2_000_000_000
    assert sf({"up": 0}, now) == 1                         # 999+ days old: old
    assert sf({"up": 20000}, now) == 20000 * 86400
    assert sf({"up": 30000}, now) == now                   # (never in the future)
    assert sf({}, now) == now and sf(None, now) == now     # (an index without upload days)


def test_mpeg_audio_is_announced_the_way_it_is_served():
    """Subsonic listings, the stream's own routing and Cast agree: Layer III
    (format "MP3") goes out as the MP3 it is, any other layer transcoded."""
    from types import SimpleNamespace as NS
    from soniqboom.api import cast_stream, stream, subsonic
    p = "https://aminet.invalid/mods:/mpg/Song.mpg"
    assert stream.delivered_format(p, "MP3") is None                  # (the file itself)
    assert stream.delivered_format(p, "MP2") is not None
    assert stream.delivered_format(p, "MPEG audio") is not None
    assert subsonic._delivery({"path": p, "format": "MP3"})[:3] == ("mp3", "audio/mpeg", None)
    assert subsonic._delivery({"path": p, "format": "MP2"})[2] == "wav"
    assert cast_stream._ext_for(p, NS(format="MP3")) == ".mp3"
    assert cast_stream._ext_for(p, NS(format="MP2")) == ".mpg"
    assert cast_stream._ext_for(p) == ".mpg"


@pytest.mark.asyncio
async def test_one_merge_watcher_per_streamed_file(monkeypatch, tmp_path):
    """Many range requests of one file streamed while it downloads: one
    watcher reads it once it is whole; a download that ends between two
    looks is looked at once more."""
    from soniqboom.api import stream
    from soniqboom.core import online_catalog, remote_cache
    merged = []
    monkeypatch.setattr(online_catalog, "merge_after_play", lambda tid, p: merged.append((tid, p)))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(stream.asyncio, "sleep", lambda s: real_sleep(0.01))
    state = {"looks": 0}
    whole = tmp_path / "x.mp3"

    class Cache:
        def get_cached(self, root, rel):
            state["looks"] += 1
            # Not there at the first look; the download ends right after it.
            return whole if state["looks"] >= 2 else None

        def downloading(self, root, rel):
            return False
    monkeypatch.setattr(remote_cache, "get_cache", lambda: Cache())
    monkeypatch.setattr(stream, "_MERGE_WATCHERS", {})
    for _ in range(5):
        stream._merge_when_downloaded("t1", "root", "/x.mp3")
    assert len(stream._MERGE_WATCHERS) == 1
    await asyncio.wait_for(stream._MERGE_WATCHERS["t1"], 5)
    assert merged == [("t1", whole)]
    await real_sleep(0)
    assert stream._MERGE_WATCHERS == {}                  # (gone once done)


def test_description_names_in_their_many_spellings():
    da = online_aminet.desc_artist
    cases = {
        "Techno mod by Skope - 4ch": ("Skope", ""),
        "7 great tunes by Cyborg/Crazy": ("Cyborg", "Crazy"),
        '"Blue Water Pond" / A.Akerheden': ("A.Akerheden", ""),
        "Cool Mod By Arash Tavakoli aka HyperionT": ("Arash Tavakoli (HyperionT)", ""),
        "S3M-Mod by -Zane aka HMW-": ("Zane (HMW)", ""),
        "S3M-Mod by Michael J. Bertrand": ("Michael J. Bertrand", ""),
        "S3M-Mod by C.J. Miller": ("C.J. Miller", ""),
        "Chiptune by Kyd, Jesper 0:30 C1++": ("Jesper Kyd", ""),     # "Last, First" + length
        "Mpeg file by Salty, House": ("Salty", ""),                    # no length: no swap
        "A slow song by me, Owlz": ("Owlz", ""),
        "A track By CSM/1996": ("CSM", ""),                            # a year is no group
        "Protracker module by Boray 95/11/18____": ("Boray", ""),
        "DBM 4ch mod by Tripper^Stx^Moods": ("Tripper", "Stx"),
        "Slow Chip Mod by Nork/Network^Massive^Osm": ("Nork", "Network"),
        "PT Module by (c) JazzCat 1995": ("JazzCat", ""),
        "House by [mASh!]": ("mASh", ""),
        "Metal module by ## Etrimon ##": ("Etrimon", ""),
        "Funk mod by Don_Cato": ("Don Cato", ""),
        "Fine MED module by /\\/\\ind \\/\\/alker": ("", ""),        # ASCII art: none
        "Song by Mr. Mortal": ("Mr. Mortal", ""),
        "Mod by 0:15": ("", ""),
        "Protracker module by Boray 93": ("Boray", ""),
        "A track by CSM.1996": ("CSM", ""),
        "Tune by racoon '95": ("racoon", ""),
        "Mod by Saint Shoe/may 1994/": ("Saint Shoe", ""),     # a date is no group
        "Chiptune by ElbiE^t": ("ElbiE", ""),                   # (a cut group: none)
        'Mod by Room 237 "PhoneTap': ("Room 237", ""),
        "Song by Hollywood and": ("Hollywood", ""),
        "Mod by Unknown Artist from Exodus": ("", ""),
        "Mod by Studio 54": ("Studio 54", ""),
    }
    for desc, want in cases.items():
        assert da(desc) == want, (desc, da(desc))
    assert da("14 Ron Klaren modules!", collection=True) == ("Ron Klaren", "")
    assert da("Paula's Lost DSS Tunes", collection=True) == ("", "")     # the sound chip
    assert da("3 FIXED mods from Desert Dream", collection=True) == ("", "")


def test_who_a_readme_author_is_depends_on_the_upload(monkeypatch):
    cat = online_aminet.AMINET_ONLINE
    cat.prepare_build()
    monkeypatch.setattr(cat, "_cats", {"tp96": "Mods from The Party 1996",
                                       "sets": "Module collections", "voy": "Mods by Voyager",
                                       "rated": "Rated modules"})
    monkeypatch.setattr(cat, "_names", [])
    monkeypatch.setattr(cat, "_spell", None)

    def build(rel, ex):
        return cat.build_track(rel, 0, ex, added_at=0, dir_hash="", root_h="")
    # A party's upload is its composer's entry (the uploader is another line).
    t = build("tp96/42.lha::42.mod", {"desc": "42, a TP96 multichannel mod.", "member": "42.mod",
                                      "members": 1, "pkg": {"author": "Jets", "group": "Beam"}})
    assert (t["artist"], t["scene_group"], t["scene_party"]) == ("Jets", "Beam", "The Party 1996")
    # A set of several tunes: its Author compiled it — the description's composer counts …
    t = build("sets/14RK_Mods.lha::mod.Astra_1",
              {"desc": "14 Ron Klaren modules!", "member": "mod.Astra_1", "members": 14,
               "pkg": {"author": "Mikael Lund"}})
    assert t["artist"] == "Ron Klaren"
    # … unless the description names the Author — also where Aminet's cut ends it short.
    t = build("sets/Dark.lha::a.mod", {"desc": "A long collection of old modules by Darkne",
                                       "member": "a.mod", "members": 3,
                                       "pkg": {"author": "Darkness"}})
    assert t["artist"] == "Darkness"
    assert not online_aminet._named_in("Ash", "Flash and more")          # whole words only
    # One spelling per composer in their own category; rating codes leave the comment.
    assert build("voy/x.lha", {"desc": "PT 4-track, ambient module by V0yager"})["artist"] \
        == "Voyager"
    t = build("rated/y.lha", {"desc": "Chiptune by Kyd, Jesper 0:30 C1++"})
    assert t["artist"] == "Jesper Kyd" and t["comment"] == "Chiptune by Kyd, Jesper"
    assert "comment" not in build("rated/z.lha", {"desc": "Module -:-- -"})
    # A group Aminet's cut ends short: completed from the groups named in full;
    # one named in full that ends a description stands.
    monkeypatch.setattr(cat, "_groups", ["Honoo+LED", "RNO"])
    t = build("chip/a.lha", {"desc": "Concept in dance, a mod by Black Dragon/Honoo+L"})
    assert (t["artist"], t["scene_group"]) == ("Black Dragon", "Honoo+LED")
    t = build("chip/b.lha", {"desc": "A distracted trance-song by Kure4Kancer/RNO"})
    assert t["scene_group"] == "RNO"


@pytest.mark.asyncio
async def test_other_build_rules_rebuild_every_row_and_a_played_one_keeps_its_read(
        env, monkeypatch):
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("techn/Opus.zip")
    await _play(tid)
    played = await _merged(store, tid)
    assert "artist" in played["index_fields"] and "title" not in played["index_fields"]
    unplayed = cat.track_id("mpg/Old.mpg")
    # Rows as older rules built them.
    other = cat.track_id("sets/Gryzor.zip")
    store.update_track_fields_batch([
        (unplayed, {"artist": "Wrong", "scene_path": "mpg/Old.mpg", "scene_group": "1996",
                    "index_vals": {"scene_group": "1996"}}),
        # (a group another pass set over the build's: not the build's to withdraw)
        (other, {"scene_group": "Demozoo Crew", "index_vals": {"scene_group": "Old"}}),
        (tid, {"artist": "Old Rules"})])
    assert (await cat.reapply())["state"] == "ready"
    assert store.get_track(tid)["artist"] == "Old Rules"            # same rules: kept
    monkeypatch.setattr(cat, "build_rules", 7)
    assert (await cat.reapply())["state"] == "ready"
    t = store.get_track(tid)
    assert t["artist"] == "Skope"                                    # re-derived
    assert t["mtime"] == played["mtime"] and t["title"] == played["title"]   # its read stays
    u = store.get_track(unplayed)
    assert u["artist"] == "Somebody" and not u.get("scene_path"), u.get("scene_path")
    assert not u.get("scene_group") and not u.get("index_vals")      # the old build's: gone
    assert store.get_track(other)["scene_group"] == "Demozoo Crew"
    assert cat.index_info().get("built_rules") == "7"
    # What an apply recorded survives a new index download.
    applied = cat.index_info().get("applied_at")
    st = await _rewrite(env, day=DAY + 1, ages={"techn/Re.zip": 6})
    assert st["state"] == "ready"
    info = cat.index_info()
    assert info.get("built_rules") == "7" and float(info["applied_at"]) >= float(applied)
    assert store.get_track(tid)["mtime"] == played["mtime"]         # not read anew


@pytest.mark.asyncio
async def test_played_members_keep_their_read_when_a_package_is_listed_anew(env):
    """Listed by other naming rules, a package folds back to one entry; opened
    again, its played members come back with what their plays read."""
    import sqlite3
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("sets/14RK_Mods.zip")
    await _play(tid)
    first = await _merged(store, tid)
    second = cat.track_id("sets/14RK_Mods.zip::mod.Astra_2.mod")
    await _play(second)
    second_row = await _merged(store, second)
    con = sqlite3.connect(cat.packages_path())
    con.execute("UPDATE packages SET marker = substr(marker, 1, instr(marker, '#') - 1)")
    con.commit()
    con.close()
    assert (await cat.reapply())["state"] == "ready"
    assert store.get_track(tid)["path"].endswith("sets/14RK_Mods.zip")   # one entry again
    assert second not in store._tracks
    await _play(tid)                                     # opened again (from its cached copy)
    assert store.get_track(tid)["mtime"] == first["mtime"]
    assert store.get_track(second)["mtime"] == second_row["mtime"]
    assert store.get_track(second)["title"] == second_row["title"]


@pytest.mark.asyncio
async def test_a_loose_upload_reads_its_readme_on_its_first_play(env, monkeypatch):
    cat, store = env["cat"], env["store"]
    (env["web"] / "mods" / "mpg" / "Old.readme").write_text(
        "Short: An old MP2 song\nAuthor: Real Author/Crew\nUploader: someone\n", "latin-1")
    await cat.enable()
    tid = cat.track_id("mpg/Old.mpg")
    assert store.get_track(tid)["artist"] == "Somebody"            # (the description's)
    await _play(tid)
    t = await _merged(store, tid)
    assert (t["artist"], t.get("scene_group"), t["comment"]) == ("Real Author", "Crew",
                                                                 "An old MP2 song")
    assert not {"artist", "scene_group", "comment"} & set(t["index_fields"])
    monkeypatch.setattr(cat, "build_rules", 9)                     # a rebuild keeps them
    assert (await cat.reapply())["state"] == "ready"
    assert store.get_track(tid)["artist"] == "Real Author"


@pytest.mark.asyncio
async def test_a_tune_played_after_its_re_upload_stays_read_across_index_downloads(env):
    """Its change stamp predates the apply that took it in: a later index
    download (a new index file) must not take it for a fresh change again —
    rebuilt unread, its cache dropped."""
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("techn/Re.zip")
    await _play(tid)
    files = dict(SITE)
    files["techn/Re.zip"] = (_zip({"mod.Re": _mod(b"Second uploa")}), "Re-upload test")
    st = await _rewrite(env, day=DAY + 7, ages={"techn/Re.zip": 0}, files=files)
    assert st["state"] == "ready"
    await _play(tid)                                     # the new upload, read
    t = await _merged(store, tid)
    st = await _rewrite(env, day=DAY + 8, ages={"techn/Re.zip": 1}, files=files)
    assert st["state"] == "ready"
    assert store.get_track(tid)["mtime"] == t["mtime"]   # still read
    assert cat.index_info().get("built_rules") == str(cat.build_rules)


@pytest.mark.asyncio
async def test_rows_built_by_older_rules_are_rebuilt_at_startup(env, monkeypatch):
    from soniqboom.core import online_catalog
    cat, store = env["cat"], env["store"]
    await cat.enable()
    unplayed = cat.track_id("mpg/Old.mpg")
    store.update_track_fields_batch([(unplayed, {"artist": "Wrong"})])
    await asyncio.get_running_loop().run_in_executor(None, cat._touch_info, {"built_rules": "1"})
    real_sleep = asyncio.sleep
    monkeypatch.setattr(online_catalog.asyncio, "sleep",
                        lambda s: real_sleep(0 if s >= 20 else s))
    await cat.startup()                                # (a restart with this code)
    for _ in range(100):
        if store.get_track(unplayed)["artist"] == "Somebody":
            break
        await real_sleep(0.05)
    assert store.get_track(unplayed)["artist"] == "Somebody"
    assert cat.index_info()["built_rules"] == str(cat.build_rules)


@pytest.mark.asyncio
async def test_a_played_tune_loses_a_group_the_build_no_longer_sets(env, monkeypatch):
    cat, store = env["cat"], env["store"]
    await cat.enable()
    files = dict(SITE)
    files["techn/Grp.zip"] = (_zip({"mod.Grp": _mod(b"Grp")}), "Techno mod by Skope/Crew")
    assert (await _rewrite(env, files=files))["state"] == "ready"
    tid = cat.track_id("techn/Grp.zip")
    await _play(tid)
    played = await _merged(store, tid)
    assert played["scene_group"] == "Crew" and played["index_vals"] == {"scene_group": "Crew"}
    real = cat.build_track

    def build(*a, **kw):
        t = real(*a, **kw)
        t.pop("scene_group", None)                       # other rules: no group here
        return t
    monkeypatch.setattr(cat, "build_track", build)
    monkeypatch.setattr(cat, "build_rules", 11)
    assert (await cat.reapply())["state"] == "ready"
    t = store.get_track(tid)
    assert not t.get("scene_group") and t["mtime"] == played["mtime"]
