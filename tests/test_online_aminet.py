# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The online Aminet collection (``core/online_aminet``) and packages
(``OnlineCatalogue.expand``).

Served by a local HTTP server holding Aminet's ``INDEX``, its category page
and a few uploads under ``mods/``: a one-module package, a module set, a
package with nothing playable, a loose MP3 and a sample pack (left out).
Covers: the index → one row per package (no archive read); the gate; a play
opening a package (its members in its place, the first keeping its id, the
readme's author); Track Info opening one; a package with nothing playable
leaving at once; a re-applied index keeping opened packages; a changed package
(new size) going back to one row; the parser.
"""
from __future__ import annotations

import asyncio
import http.server
import io
import threading
import zipfile
from pathlib import Path

import pytest

from soniqboom.core import catalogue, online_aminet, online_catalog, remote_cache
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _mod(title: bytes) -> bytes:
    return title.ljust(20, b"\0") + b"\0" * 1064 + b"M.K." + b"\0" * 1024


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


PACKAGES = {
    "techn/Dreams.zip": _zip({"mod.Dreams": _mod(b"Dreams of Speed"),
                              "Dreams.readme": b"x"}),
    "4mat/4mat_1.zip": _zip({"mod.Madness": _mod(b"4-Mat's Madness"),
                             "mod.Above_My_Head": _mod(b"Above My Head"),
                             "mod.Chrome_Desert": _mod(b"")}),
    "misc/Player.zip": _zip({"Player.exe": b"MZ" + b"\0" * 64, "readme.txt": b"hi"}),
    "sets/Nested.zip": _zip({"inner.zip": _zip({"deep.mod": _mod(b"Deep")})}),
}
README = {
    "techn/Dreams.readme": "Short:        Dreams - 4ch techno mod\nAuthor:       Jane Tracker "
                           "(jane@example.com)\nUploader:     jane example com\nType: mods/techn\n",
    "4mat/4mat_1.readme": "Short:        4MAT Modules\nAuthor:       4MAT\nType: mods/4mat\n",
}
LOOSE = {"techn/Live.mp3": b"ID3" + b"\0" * 200,
         "smpl/Drums.zip": _zip({"kick.iff": b"FORM"})}


def _index(packages=PACKAGES) -> str:
    rows = ["|", "| Aminet index", "|"]
    for rel, data in {**packages, **LOOSE}.items():
        cat, name = rel.split("/", 1)
        desc = {"techn/Dreams.zip": "Dreams - 4ch techno mod",
                "4mat/4mat_1.zip": "4MAT Modules - Details inside!"}.get(rel, "stuff")
        rows.append(f"{name:<30} mods/{cat:<5} {max(1, len(data) // 1024)}K 999 {desc}")
    rows.append(f"{'Other.lha':<30} util/misc   10K 999 not music")
    return "\n".join(rows) + "\n"


TREE = ("<html><body><ul><li><a href='/tree?path=mods/4mat'>mods/4mat</a> - Mods by 4mat "
        "(71 packages)</li><li>mods/techn - Techno music (1737 packages)</li>"
        "<li>mods/misc - Miscellaneous songs (1561 packages)</li></ul></body></html>")


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


def _write_site(web: Path, packages=PACKAGES) -> None:
    (web / "INDEX").write_text(_index(packages), "latin-1")
    (web / "tree").write_text(TREE, "utf-8")
    for rel, data in {**packages, **LOOSE}.items():
        p = web / "mods" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
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
    _write_site(web)
    _Handler.log = []
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
    yield {"store": store, "web": web, "cat": cat, "site": site}
    srv.shutdown()
    catalogue.unregister_root(cat.root)
    from soniqboom.core.filesource import remove_source
    remove_source(cat.root)


async def _enable(env) -> dict:
    st = await env["cat"].enable()
    assert st["state"] == "ready", st
    return st


def _package_gets():
    return [p for m, p in _Handler.log if m == "GET" and p.startswith("/mods/")]


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
async def test_the_index_lists_one_row_per_package_without_reading_one(env):
    st = await _enable(env)
    cat, store = env["cat"], env["store"]
    assert st["tunes"] == 5 and st["index_rows"] == 5            # samples / util left out
    assert _package_gets() == []
    t = store.get_track(cat.track_id("techn/Dreams.zip"))
    assert t["path"] == f"{cat.root}:/techn/Dreams.zip"
    assert t["title"] == "Dreams" and t["format"] == "ZIP package"
    assert t["comment"] == "Dreams - 4ch techno mod" and t["genre"] == ["Techno"]
    four = store.get_track(cat.track_id("4mat/4mat_1.zip"))
    assert four["artist"] == "4mat"                               # "Mods by 4mat"
    mp3 = store.get_track(cat.track_id("techn/Live.mp3"))
    assert mp3["format"] == "MP3"
    assert store.get_track(cat.track_id("smpl/Drums.zip")) is None


@pytest.mark.asyncio
async def test_reads_are_gated_and_sizes_come_from_the_archive(env):
    from soniqboom.core.filesource import get_source
    await _enable(env)
    src = get_source(env["cat"].root)
    assert src.stat("/techn/Dreams.zip").size == 0                # no request outside a play
    with pytest.raises(catalogue.CatalogueFetchRefused):
        src.read_file("/techn/Dreams.zip")
    with pytest.raises(catalogue.CatalogueFetchRefused):
        src.read_file("/techn/Dreams.readme")
    assert _package_gets() == []


@pytest.mark.asyncio
async def test_a_play_opens_the_package_and_its_member_plays(env):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("techn/Dreams.zip")
    path, ext = await _play(tid)
    assert ext == ".mod" and Path(path).read_bytes() == _mod(b"Dreams of Speed")
    t = store.get_track(tid)                                      # the package's id plays on
    assert t["path"] == f"{cat.root}:/techn/Dreams.zip::mod.Dreams.mod"
    assert t["artist"] == "Jane Tracker"                          # readme Author, no address
    assert t["comment"] == "Dreams - 4ch techno mod" and "Techno" in t["genre"]
    assert sorted(_package_gets()) == ["/mods/techn/Dreams.readme", "/mods/techn/Dreams.zip"]
    t = await _merged(store, tid)
    assert t["title"] == "Dreams of Speed" and "Techno" in t["genre"]
    assert t["artist"] == "Jane Tracker"
    # Played again: nothing fetched a second time.
    _Handler.log = []
    await _play(tid)
    assert _package_gets() == []


@pytest.mark.asyncio
async def test_a_set_opens_into_its_modules_first_keeping_the_id(env):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    store.create_playlist("pl1", "mine", track_ids=[cat.track_id("4mat/4mat_1.zip")])
    tid = cat.track_id("4mat/4mat_1.zip")
    await _play(tid)
    rows = sorted((t["path"].rsplit("::", 1)[-1], t["id"], t.get("album"))
                  for t in store._tracks.values() if "4mat_1.zip" in t["path"])
    assert [r[0] for r in rows] == ["mod.Above_My_Head.mod", "mod.Chrome_Desert.mod",
                                    "mod.Madness.mod"]
    assert all(r[2] == "4mat_1" for r in rows)
    first = store.get_track(tid)
    assert first is not None and "::" in first["path"]            # the playlist entry plays
    assert sum(1 for r in rows if r[1] == tid) == 1
    titles = {store.get_track(r[1])["title"] for r in rows}
    assert titles == {"Madness", "Above_My_Head", "Chrome_Desert"}
    assert env["cat"].status()["tunes"] == 7


@pytest.mark.asyncio
async def test_a_package_with_nothing_playable_leaves_at_once(env):
    from fastapi import HTTPException
    from soniqboom.api import stream
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("misc/Player.zip")
    with pytest.raises(HTTPException) as ei:
        await _play(tid)
    # (410: it left the library; the sentence says what it holds — not "corrupt")
    assert ei.value.status_code == 410 and "has no tunes SoniqBoom plays" in ei.value.detail
    assert "a PC program" in ei.value.detail
    assert store.get_track(tid) is None                          # gone now, not at the next apply
    _Handler.log = []
    gone = stream._track_missing(tid)                             # a later request says why
    assert gone.status_code == 410 and gone.detail == ei.value.detail
    assert _package_gets() == []                                  # no second download
    st = await cat.reapply()                                      # … and the apply keeps it out
    assert store.get_track(tid) is None and st["tunes"] == 4


@pytest.mark.asyncio
async def test_an_opened_package_survives_a_refresh_and_a_changed_one_folds_back(env):
    import os
    import time as _time
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("4mat/4mat_1.zip")
    await _play(tid)
    await _merged(store, tid)
    st = await cat.reapply()
    assert st["tunes"] == 7 and "::" in store.get_track(tid)["path"]
    assert (store.get_track(tid).get("mtime") or 0) > 0           # the play's read stays
    # A new upload of the package (another size): one row again, opened anew.
    bigger = dict(PACKAGES)
    bigger["4mat/4mat_1.zip"] = _zip({"mod.Madness": _mod(b"4-Mat's Madness") * 8,
                                      "pad.bin": os.urandom(4096)})
    _write_site(env["web"], bigger)
    later = _time.time() + 10
    os.utime(env["web"] / "INDEX", (later, later))
    st = await cat.refresh(manual=True)
    assert st["state"] == "ready", st
    t = store.get_track(tid)
    assert t["path"] == f"{cat.root}:/4mat/4mat_1.zip" and st["tunes"] == 5
    path, _ext = await _play(tid)
    assert store.get_track(tid)["path"].endswith("::mod.Madness.mod")
    # The new upload's bytes play — not the cached copy of the old one.
    assert Path(path).read_bytes() == _mod(b"4-Mat's Madness") * 8


@pytest.mark.asyncio
async def test_track_info_opens_a_package(env):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("techn/Dreams.zip")
    stream.catalogue_read_for_info(tid, await get_track(tid), "tester")
    t = await _merged(store, tid)
    assert t["path"].endswith("::mod.Dreams.mod") and t["title"] == "Dreams of Speed"


def test_parsers():
    from soniqboom.core import metadata
    rows = online_aminet.parse_index(_index(), metadata.SUPPORTED_EXTENSIONS)
    rels = [r for r, _e in rows]
    assert "smpl/Drums.zip" not in rels and "techn/Live.mp3" in rels
    assert all(not r.startswith("util") for r in rels)
    assert dict(rows)["4mat/4mat_1.zip"]["desc"] == "4MAT Modules - Details inside!"
    cats = online_aminet.parse_categories(TREE)
    assert cats["4mat"] == "Mods by 4mat" and cats["techn"] == "Techno music"
    assert online_aminet.category_artist("Mods by Blasa (Mauro R.)") == "Blasa"
    assert online_aminet.category_artist("Techno music") == ""
    assert online_aminet.parse_readme("Author: jd@example.com\n") == {}
    assert online_aminet.parse_readme(README["techn/Dreams.readme"])["author"] == "Jane Tracker"
    assert online_catalog.get("aminet") is online_aminet.AMINET_ONLINE
    assert online_catalog._iso_date("Fri, 26 Jun 2026 13:39:13 GMT") == "2026-06-26"
    assert online_catalog._iso_date("2026-10-14") == "2026-10-14"


@pytest.mark.asyncio
async def test_an_archive_inside_a_package_opens_like_the_scanner_opens_it(env):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("sets/Nested.zip")
    path, ext = await _play(tid)
    assert ext == ".mod" and Path(path).read_bytes()[:4] == b"Deep"
    assert store.get_track(tid)["path"].endswith("Nested.zip::inner.zip::deep.mod")


@pytest.mark.asyncio
async def test_the_other_tunes_of_an_opened_package_in_archive_order(env):
    await _enable(env)
    cat = env["cat"]
    tid = cat.track_id("4mat/4mat_1.zip")
    assert online_catalog.package_member_ids(tid) == []          # not opened yet
    await _play(tid)
    ids = online_catalog.package_member_ids(tid)
    names = [env["store"].get_track(i)["path"].rsplit("::", 1)[-1] for i in ids]
    assert names == ["mod.Above_My_Head.mod", "mod.Chrome_Desert.mod"]   # zip order, first left out
    from soniqboom.api import tracks
    assert any(getattr(r, "path", "").endswith("/{track_id}/package-members")
               for r in tracks.router.routes)


@pytest.mark.asyncio
async def test_a_play_while_the_collection_switches_off_leaves_no_rows(env, monkeypatch):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    await cat.disable()
    rows = [("4mat/4mat_1.zip::mod.Madness.mod", 0, 0,
             {"member": "mod.Madness.mod", "first": True, "members": 1})]
    await cat._write_package_rows("4mat/4mat_1.zip", rows)
    assert not [t for t in store._tracks.values() if "4mat_1.zip" in (t.get("path") or "")]


def test_readme_authors_and_descriptions_are_cleaned():
    ca = online_aminet.clean_author
    assert ca("jogeir@liljedahl.com (Jogeir Liljedahl)") == ("Jogeir Liljedahl", "")
    assert ca("Jazzcat (jazzcat@example.org), Olof") == ("Jazzcat, Olof", "")
    assert ca("Boray (anders at boray dot se)") == ("Boray", "")
    assert ca("Delorean http://www.delorean.de") == ("Delorean", "")
    assert ca("Various") == ("", "") and ca("me") == ("", "")
    assert ca("STONE (REFORM team)") == ("STONE", "REFORM team")
    assert ca("Jester/Sanity") == ("Jester", "Sanity")
    assert online_aminet.clean_desc("Pop mod by 4mat               ****+") == "Pop mod by 4mat"
    assert online_aminet.desc_artist("Techno mod by Skope - 4ch") == ("Skope", "")
    assert online_aminet.desc_artist("made by me") == ("", "")
    assert online_aminet.category_artist("Neurodancer's modules") == "Neurodancer"
    assert online_aminet.category_party("Mods from The Party 1996") == "The Party 1996"
