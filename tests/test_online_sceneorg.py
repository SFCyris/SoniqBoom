# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The online scene.org collection (``core/online_sceneorg``).

Served by a local HTTP server that lists folders the way archive.scene.org
does (its listing page markup) and answers ranged reads.  Covers: the
listing → rows (artist / group / compo / disk folders naming the tunes, only
archives and playable files, no package read); a refresh listing again only
folders that can have changed; a folder that can't be listed (kept for the
next refresh); a play opening a ZIP — a big one listed by ranged reads of its
directory, not fetched whole; the listing parser.
"""
from __future__ import annotations

import asyncio
import http.server
import io
import os
import threading
import zipfile
from pathlib import Path

import pytest

from soniqboom.core import catalogue, online_sceneorg, remote_cache
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _mod(title: bytes, pad: int = 0) -> bytes:
    return title.ljust(20, b"\0") + b"\0" * 1064 + b"M.K." + b"\0" * 1024 + os.urandom(pad)


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


FILES = {
    "artists/4-mat/4-mats_madness.zip": _zip({"4-mats_madness.mod": _mod(b"Madness")}),
    "artists/4-mat/readme.txt": b"hi",
    "artists/a-man/old/tune.zip": _zip({"tune.xm": b"Extended Module: tune" + b"\0" * 300}),
    "groups/the_jazz_cats/jc-pack.zip": _zip({"one.mod": _mod(b"One"), "two.mod": _mod(b"Two")}),
    "compos/20_min_chiptune_compo/entry.zip": _zip({"entry.mod": _mod(b"Entry")}),
    "disks/alchemy/alchemy.zip": _zip({f"track{i}.mod": _mod(b"T%d" % i, 2048)
                                       for i in range(6)}),
    "disks/alchemy/alchemy.diz": b"a disk",
    "compilations/demovibes.rar": b"Rar!",
    "compilations/loose.mp3": b"ID3" + b"\0" * 64,
}
DATES = {}


def _listing(rel: str, web: Path) -> str:
    d = web / "pub" / "music" / rel if rel else web / "pub" / "music"
    rows = ['<tr class="d"><td class="n"><a href="../">..</a>/</td><td class="m">&nbsp;</td>'
            '<td class="s">- &nbsp;</td><td class="t">Directory</td></tr>']
    for p in sorted(d.iterdir()):
        sub = f"{rel}/{p.name}" if rel else p.name
        date = DATES.get(sub, "2002-Apr-14 17:45:14")
        if p.is_dir():
            rows.append(f'<tr class="d"><td class="n"><a href="{p.name}/">{p.name}</a>/</td>'
                        f'<td class="m">{date}</td><td class="s">- &nbsp;</td>'
                        '<td class="t">Directory</td></tr>')
        else:
            kb = max(0.1, p.stat().st_size / 1024)
            rows.append(f'<tr><td class="n"><a href="{p.name}">{p.name}</a></td>'
                        f'<td class="m">{date}</td><td class="s">{kb:.1f}K</td>'
                        '<td class="t">application/zip</td></tr>')
    return ("<html><body><h2>Index of /pub/music/</h2><div class=\"list\"><table>"
            "<thead><tr><th class=\"n\">Name</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div></body></html>")


class _Handler(http.server.BaseHTTPRequestHandler):
    log: list = []
    web: Path = Path(".")
    broken: set = set()
    cut: set = set()               # listings sent the way archive.scene.org cuts long ones
    no_ranges: set = set()         # files sent whole whatever the Range asks
    star_total: set = set()        # ranged answers that leave the total out ("/*")

    def log_message(self, *a):
        pass

    def _serve(self, body: bool):
        path = self.path.split("?", 1)[0]
        type(self).log.append((self.command, path, self.headers.get("Range")))
        if not path.startswith("/pub/music"):
            self.send_error(404)
            return
        rel = path[len("/pub/music"):].strip("/")
        from urllib.parse import unquote
        rel = unquote(rel)
        if rel in type(self).broken:
            self.send_error(500)
            return
        p = type(self).web / "pub" / "music" / rel
        if p.is_dir():
            data = _listing(rel, type(self).web).encode()
            length = len(data)
            if rel in type(self).cut:
                data = data[:data.rindex(b"</tr>") + 5]       # everything after the last row lost
                self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if body:
                self.wfile.write(data)
            return
        if not p.is_file():
            self.send_error(404)
            return
        data = p.read_bytes()
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=") and rel not in type(self).no_ranges:
            a, _, b = rng[6:].partition("-")
            start = int(a)
            end = int(b) if b else len(data) - 1
            chunk = data[start:end + 1]
            self.send_response(206)
            total = "*" if rel in type(self).star_total else len(data)
            self.send_header("Content-Range", f"bytes {start}-{start + len(chunk) - 1}/{total}")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if body:
                self.wfile.write(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)


def _write(web: Path, files=FILES) -> None:
    for rel, data in files.items():
        p = web / "pub" / "music" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


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
    _write(web)
    DATES.clear()
    _Handler.log, _Handler.web, _Handler.broken, _Handler.cut = [], web, set(), set()
    _Handler.no_ranges = set()
    _Handler.star_total = set()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/pub/music"
    cat = online_sceneorg.SCENEORG_ONLINE
    monkeypatch.setattr(online_sceneorg, "BASE", base)
    monkeypatch.setattr(online_sceneorg, "CRAWL_GAP_S", 0.0)
    monkeypatch.setattr(cat, "crawl_path", lambda: tmp_path / "crawl.json")
    monkeypatch.setattr(cat, "root", base)
    monkeypatch.setattr(cat, "min_rows", 1)
    monkeypatch.setattr(cat, "_status", dict(cat._status, state="off", error=None))
    for attr, val in (("_job_lock", None), ("_tasks", set()), ("_job_tasks", set()),
                      ("_refresh_task", None), ("_local", threading.local()),
                      ("_pkg_locks", {}), ("_expanded_at", {}), ("_merge_tasks", {}),
                      ("_merging", set()), ("_run_stop", None)):
        monkeypatch.setattr(cat, attr, val)
    yield {"store": store, "web": web, "cat": cat, "base": base}
    srv.shutdown()
    catalogue.unregister_root(base)
    from soniqboom.core.filesource import remove_source
    remove_source(base)


def _listings():
    return [p for m, p, _r in _Handler.log if m == "GET" and (p.endswith("/") or "." not in p)]


def _file_gets():
    return [(p, r) for m, p, r in _Handler.log if m == "GET" and not p.endswith("/") and "." in p]


async def _play(tid):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    path, ext, _n, pin = await stream._resolve_play_source(tid, await get_track(tid))
    if pin:
        stream._zip_unpin(pin)
    return path, ext


@pytest.mark.asyncio
async def test_the_listings_give_one_row_per_package_named_by_their_folders(env):
    st = await env["cat"].enable()
    assert st["state"] == "ready", st
    cat, store = env["cat"], env["store"]
    assert st["tunes"] == 6 and _file_gets() == []                # no package read
    t = store.get_track(cat.track_id("artists/4-mat/4-mats_madness.zip"))
    assert t["title"] == "4-mats madness" and t["artist"] == "4-mat"
    assert t["format"] == "ZIP package" and t["file_size"] > 0
    assert store.get_track(cat.track_id("artists/a-man/old/tune.zip"))["artist"] == "a-man"
    assert store.get_track(cat.track_id("groups/the_jazz_cats/jc-pack.zip"))["scene_group"] \
        == "the jazz cats"
    assert store.get_track(cat.track_id("compos/20_min_chiptune_compo/entry.zip"))["album"] \
        == "20 min chiptune compo"
    assert store.get_track(cat.track_id("disks/alchemy/alchemy.zip"))["album"] == "alchemy"
    assert store.get_track(cat.track_id("compilations/loose.mp3"))["format"] == "MP3"
    assert store.get_track(cat.track_id("compilations/demovibes.rar")) is None
    assert store.get_track(cat.track_id("artists/4-mat/readme.txt")) is None


@pytest.mark.asyncio
async def test_a_refresh_lists_only_what_can_have_changed(env):
    cat = env["cat"]
    await cat.enable()
    first = len(_listings())
    assert first == 12                    # root, 5 sections, 6 folders below them
    _Handler.log = []
    st = await cat.refresh(manual=True)
    assert st["state"] == "ready"
    again = _listings()
    # Leaf folders whose dates are the same are not listed again.
    assert "/pub/music/artists/4-mat/" not in again and "/pub/music/disks/alchemy/" not in again
    assert "/pub/music/artists/a-man/" in again                   # it has a subfolder
    # A leaf folder with a new date is listed again and its new file listed.
    _write(env["web"], {"artists/4-mat/new_one.zip": _zip({"n.mod": _mod(b"N")})})
    DATES["artists/4-mat"] = "2026-Oct-01 10:00:00"
    _Handler.log = []
    st = await cat.refresh(manual=True)
    assert "/pub/music/artists/4-mat/" in _listings()
    assert env["store"].get_track(cat.track_id("artists/4-mat/new_one.zip")) is not None
    assert st["tunes"] == 7


@pytest.mark.asyncio
async def test_a_folder_that_cannot_be_listed_is_tried_again(env):
    cat = env["cat"]
    # Never listed and failing twice (the retry pass too): nothing is applied
    # without its files — the next attempt resumes from what was listed.
    _Handler.broken = {"groups/the_jazz_cats"}
    st = await cat.enable()
    assert st["tunes"] == 0 and "could not be listed" in (st["error"] or ""), st
    _Handler.log = []
    st = await cat.refresh(manual=True)
    # Failed the last time too: taken without it (a folder that can't be
    # listed at all must not block the collection); unchanged leaves kept.
    assert st["state"] == "ready" and st["tunes"] == 5, st
    assert "/pub/music/artists/4-mat/" not in _listings()
    _Handler.broken = set()
    st = await cat.refresh(manual=True)
    assert st["tunes"] == 6
    # Files a later listing finds in a folder listed late are not "new".
    t = env["store"].get_track(cat.track_id("groups/the_jazz_cats/jc-pack.zip"))
    assert t["added_at"] == 1


@pytest.mark.asyncio
async def test_a_play_opens_a_zip_and_a_big_one_is_read_by_ranges(env, monkeypatch):
    from soniqboom.core import remote_zip
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("artists/4-mat/4-mats_madness.zip")
    path, ext = await _play(tid)
    assert ext == ".mod" and Path(path).read_bytes()[:7] == b"Madness"
    assert store.get_track(tid)["path"].endswith("4-mats_madness.zip::4-mats_madness.mod")
    # A disk too big to fetch whole: its members come from its directory.
    monkeypatch.setattr(remote_zip, "_SUBSET_MIN_ARCHIVE", 4096)
    _Handler.log = []
    tid = cat.track_id("disks/alchemy/alchemy.zip")
    path, ext = await _play(tid)
    assert ext == ".mod" and Path(path).read_bytes()[:2] == b"T0"
    reads = [r for p, r in _file_gets() if p.endswith("alchemy.zip")]
    assert reads and all(r for r in reads)                       # ranged only, never whole
    # The played member's bytes are cached (a subset): it counts as cached,
    # its siblings don't.
    assert catalogue.is_cached(store.get_track(tid)["path"])
    assert not catalogue.is_cached(f"{cat.root}:/disks/alchemy/alchemy.zip::track3.mod")
    members = sorted(t["path"].rsplit("::", 1)[-1] for t in store._tracks.values()
                     if "alchemy.zip::" in t["path"])
    assert members == [f"track{i}.mod" for i in range(6)]
    assert all(store.get_track(cat.track_id(f"disks/alchemy/alchemy.zip::track{i}.mod")
                               )["album"] == "alchemy" for i in range(1, 6))
    for _ in range(100):
        if (store.get_track(tid).get("mtime") or 0) > 0:
            break
        await asyncio.sleep(0.05)
    assert store.get_track(tid)["album"] == "alchemy"


@pytest.mark.asyncio
async def test_a_listing_cut_after_its_last_row_is_taken(env):
    # archive.scene.org sends long listings without their tail over HTTP/1.1.
    _Handler.cut = {"disks/alchemy", "artists"}
    st = await env["cat"].enable()
    assert st["state"] == "ready" and st["tunes"] == 6, st


def test_listing_parser():
    page = ('<tr class="d"><td class="n"><a href="../">..</a>/</td><td class="m">&nbsp;</td>'
            '<td class="s">- &nbsp;</td><td class="t">Directory</td></tr>'
            '<tr class="d"><td class="n"><a href="x_y/">x_y</a>/</td><td class="m">2018-Oct-12 '
            '19:03:35</td><td class="s">- &nbsp;</td><td class="t">Directory</td></tr>'
            '<tr><td class="n"><a href="melcom%27s_(1).zip">melcom\'s_(1).zip</a></td>'
            '<td class="m">2025-Oct-07 09:11:12</td><td class="s">80.9M</td>'
            '<td class="t">application/zip</td></tr>')
    dirs, files = online_sceneorg.parse_listing(page)
    assert dirs == [("x_y", "2018-Oct-12 19:03:35")]
    assert files == [("melcom's_(1).zip", int(80.9 * 1024 * 1024), "2025-Oct-07 09:11:12")]


@pytest.mark.asyncio
async def test_a_failed_size_check_never_downloads_a_big_zip_whole(env, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.core import remote_zip
    cat = env["cat"]
    await cat.enable()
    monkeypatch.setattr(remote_zip, "_SUBSET_MIN_ARCHIVE", 4096)
    orig = _Handler.do_HEAD
    monkeypatch.setattr(_Handler, "do_HEAD", lambda self: self.send_error(503))
    _Handler.log = []
    with pytest.raises(HTTPException) as ei:
        await _play(cat.track_id("disks/alchemy/alchemy.zip"))
    assert ei.value.status_code in (502, 503)
    assert not [p for p, r in _file_gets() if p.endswith("alchemy.zip") and not r]
    monkeypatch.setattr(_Handler, "do_HEAD", orig)


def _tagged_mp3(path: Path, frames: int) -> None:
    from mutagen.id3 import ID3, TIT2, TPE1
    path.write_bytes((bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\0" * 413) * frames)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Long Mix"))
    tags.add(TPE1(encoding=3, text="DJ Test"))
    tags.save(path)


@pytest.mark.asyncio
async def test_track_info_reads_a_big_files_tags_by_ranges_only(env, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    big = env["web"] / "pub" / "music" / "artists" / "dj" / "mix.mp3"
    big.parent.mkdir(parents=True)
    _tagged_mp3(big, 3000)                                       # ~1.25 MB, 78 s
    monkeypatch.setattr(stream, "_CATALOGUE_WHOLE_MAX", 200_000)
    monkeypatch.setattr(stream, "_TAG_HEAD", 64 * 1024)
    monkeypatch.setattr(stream, "_TAG_TAIL", 64 * 1024)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", set())
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("artists/dj/mix.mp3")
    assert store.get_track(tid)["file_size"] > 200_000
    _Handler.log = []
    stream.catalogue_read_for_info(tid, await get_track(tid), "tester")
    for _ in range(100):
        if store.get_track(tid).get("title") == "Long Mix":
            break
        await asyncio.sleep(0.05)
    t = store.get_track(tid)
    assert t["title"] == "Long Mix" and t["artist"] == "DJ Test"
    assert 75 < t["duration"] < 82 and not (t.get("mtime") or 0)   # (read, not "played")
    gets = [(p, r) for p, r in _file_gets() if p.endswith("mix.mp3")]
    assert gets and all(r for p, r in gets)                         # ranged only
    fetched = sum(int(r.split("-")[1]) - int(r.split("=")[1].split("-")[0]) + 1
                  for _p, r in gets if r.split("-")[1])
    assert fetched <= 2 * 64 * 1024 + 4096                          # head + tail, not the file
    # Read once: after a restart (its length known) Track Info reads nothing more.
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", set())
    _Handler.log = []
    stream.catalogue_read_for_info(tid, await get_track(tid), "tester")
    await asyncio.sleep(0.3)
    assert not [p for p, _r in _file_gets() if p.endswith("mix.mp3")]


@pytest.mark.asyncio
async def test_a_big_catalogue_file_streams_while_it_downloads(env, monkeypatch):
    from starlette.requests import Request
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    big = env["web"] / "pub" / "music" / "artists" / "dj" / "set.mp3"
    big.parent.mkdir(parents=True)
    _tagged_mp3(big, 3000)
    monkeypatch.setattr(stream, "_CATALOGUE_PROGRESSIVE_MIN", 200_000)
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("artists/dj/set.mp3")
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": b""})
    resp = await stream._maybe_remote_progressive(req, await get_track(tid), target_format=None,
                                                  max_bitrate_kbps=0, force_transcode=False)
    assert resp is not None and resp.media_type == "audio/mpeg"
    body = b""
    async for chunk in resp.body_iterator:
        body += chunk
    assert body == big.read_bytes()
    for _ in range(100):                                  # the first-play read, once whole
        if (store.get_track(tid).get("mtime") or 0) > 0:
            break
        await asyncio.sleep(0.1)
    t = store.get_track(tid)
    assert t["mtime"] > 0 and t["title"] == "Long Mix"
    # A small one plays the regular way (fetched whole).
    small = cat.track_id("compilations/loose.mp3")
    assert await stream._maybe_remote_progressive(req, await get_track(small), target_format=None,
                                                  max_bitrate_kbps=0,
                                                  force_transcode=False) is None


@pytest.mark.asyncio
async def test_a_long_download_may_resume_past_its_fetch_window(env):
    from soniqboom.core.filesource import get_source
    await env["cat"].enable()
    src = get_source(env["cat"].root)
    with pytest.raises(catalogue.CatalogueFetchRefused):
        src._gate("/artists/dj/x.mp3", "download", resume=True)    # nothing downloading
    src._downloading = lambda rel: True
    assert src._gate("/artists/dj/x.mp3", "download", resume=True) == "artists/dj/x.mp3"
    with pytest.raises(catalogue.CatalogueFetchRefused):
        src._gate("/artists/dj/x.mp3", "download")                 # a fresh start: never


def test_what_is_too_big_to_fetch_whole(env, monkeypatch):
    from soniqboom.api import stream
    from types import SimpleNamespace as NS
    root = env["cat"].root
    catalogue.register_root(root, key="sceneorg", label="scene.org")
    big = 200 * 1024 * 1024
    assert stream._too_big_to_fetch_whole(f"{root}:/a/b.lha", NS(file_size=big))
    assert stream._too_big_to_fetch_whole(f"{root}:/a/b.wav", NS(file_size=big))
    assert not stream._too_big_to_fetch_whole(f"{root}:/a/b.zip", NS(file_size=big))   # by ranges
    assert not stream._too_big_to_fetch_whole(f"{root}:/a/b.zip::c.mod", NS(file_size=big))
    assert not stream._too_big_to_fetch_whole(f"{root}:/a/b.wav", NS(file_size=1024))
    assert not stream._too_big_to_fetch_whole("/local/b.wav", NS(file_size=big))


@pytest.mark.asyncio
async def test_a_ranged_read_is_logged_once_and_a_whole_file_answer_is_refused(env):
    from soniqboom.core import online_catalog
    from soniqboom.core.filesource import get_source
    cat, store = env["cat"], env["store"]
    await cat.enable()
    src = get_source(cat.root)
    rel = "artists/4-mat/4-mats_madness.zip"
    data = (env["web"] / "pub" / "music" / rel).read_bytes()
    assert catalogue.permit_play(store.get_track(cat.track_id(rel))["path"])
    before = catalogue.stats()
    assert src.read_at("/" + rel, 30, 100) == data[30:130]
    after = catalogue.stats()
    assert after["reads"] == before["reads"] + 1                   # one read, one log line
    assert after["bytes"] == before["bytes"] + 100                 # its bytes counted once
    assert after["recent_reads"][-1]["op"] == "range read"
    _Handler.no_ranges = {rel}                                      # a server ignoring Range
    with pytest.raises(online_catalog.RangeNotHonoured):
        src.read_range("/" + rel, 30, 100)
    assert not src.is_offline()                                     # no network trouble
    assert src.read_range("/" + rel, 0, 100) == data[:100]          # the head is the head
    assert src.read_at("/" + rel, 30, 100) == data[30:130]          # (lenient: reads the prefix)


def _big_tag_mp3(path: Path, cover: int) -> None:
    from mutagen.id3 import APIC, ID3, TIT2
    path.write_bytes((bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\0" * 413) * 400)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Big Cover"))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=os.urandom(cover)))
    tags.save(path, padding=lambda info: 0)


async def _info_read(tid):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    stream.catalogue_read_for_info(tid, await get_track(tid), "tester")
    for _ in range(100):
        if tid in stream._RANGED_INFO_DONE:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("no ranged read")


@pytest.mark.asyncio
async def test_track_info_reads_an_id3_tag_bigger_than_the_head_whole(env, monkeypatch):
    from soniqboom.api import stream
    big = env["web"] / "pub" / "music" / "artists" / "dj" / "cover.mp3"
    big.parent.mkdir(parents=True)
    _big_tag_mp3(big, 150_000)                                  # a 150 KB cover, 10 s of audio
    head = big.read_bytes()[:10]
    tag = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14
                | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
    assert tag > 64 * 1024
    monkeypatch.setattr(stream, "_CATALOGUE_WHOLE_MAX", 100_000)
    monkeypatch.setattr(stream, "_TAG_HEAD", 64 * 1024)
    monkeypatch.setattr(stream, "_TAG_TAIL", 16 * 1024)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", set())
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("artists/dj/cover.mp3")
    _Handler.log = []
    await _info_read(tid)
    t = store.get_track(tid)
    assert t["title"] == "Big Cover" and 9.5 < t["duration"] < 11 and not (t.get("mtime") or 0)
    ranges = [r for p, r in _file_gets() if p.endswith("cover.mp3")]
    size = big.stat().st_size
    # The head, the whole tag with the first frames behind it, the tail.
    assert ranges == ["bytes=0-65535", f"bytes=0-{tag + 4096 - 1}",
                      f"bytes={size - 16 * 1024}-{size - 1}"], ranges


@pytest.mark.asyncio
async def test_track_info_on_a_server_ignoring_ranges_reads_the_head_only(env, monkeypatch):
    from soniqboom.api import stream
    from soniqboom.core.filesource import get_source
    big = env["web"] / "pub" / "music" / "artists" / "dj" / "mix.mp3"
    big.parent.mkdir(parents=True)
    _tagged_mp3(big, 3000)
    monkeypatch.setattr(stream, "_CATALOGUE_WHOLE_MAX", 200_000)
    monkeypatch.setattr(stream, "_TAG_HEAD", 64 * 1024)
    monkeypatch.setattr(stream, "_TAG_TAIL", 64 * 1024)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", set())
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("artists/dj/mix.mp3")
    _Handler.no_ranges = {"artists/dj/mix.mp3"}
    before = catalogue.stats()["bytes"]
    await _info_read(tid)
    t = store.get_track(tid)
    assert t["title"] == "Long Mix" and t["artist"] == "DJ Test"     # its head's ID3
    assert catalogue.stats()["bytes"] - before == 64 * 1024          # the head, nothing more
    assert not get_source(cat.root).is_offline()


def test_a_package_member_gets_no_ranged_tag_read(env, monkeypatch):
    from soniqboom.api import stream
    from types import SimpleNamespace as NS
    root = env["cat"].root
    catalogue.register_root(root, key="sceneorg", label="scene.org")
    calls = []
    monkeypatch.setattr(stream, "_ranged_tag_read_for_info", lambda *a: calls.append(a))
    monkeypatch.setattr(stream, "_catalogue_unmerged", lambda t: True)
    monkeypatch.setattr(stream, "_too_big_to_fetch_whole", lambda p, t: True)
    stream.catalogue_read_for_info("m1", NS(path=f"{root}:/a/big.lha::song.mp3",
                                            file_size=1 << 30), "tester")
    stream.catalogue_read_for_info("m2", NS(path=f"{root}:/a/big.mp3", file_size=1 << 30),
                                   "tester")
    assert [c[0] for c in calls] == ["m2"]
    assert stream._ranged_tags("m1", f"{root}:/a/big.lha::song.mp3") is None



def test_a_member_of_a_big_lha_counts_its_packages_size(env, monkeypatch):
    """An LHA is read whole to play any member: the package's index size
    decides, not the (small) member's."""
    from soniqboom.api import stream
    from soniqboom.core import online_catalog
    from types import SimpleNamespace as NS
    root = env["cat"].root
    catalogue.register_root(root, key="sceneorg", label="scene.org")
    sizes = {"a/big.lha": 200 * 1024 * 1024, "a/small.lha": 300 * 1024}

    class _Cat:
        rel_of = env["cat"].rel_of

        def entry(self, rel):
            return (0, {"approx": sizes[rel]}) if rel in sizes else None
    monkeypatch.setattr(online_catalog, "owner_of", lambda p: _Cat())
    assert stream._too_big_to_fetch_whole(f"{root}:/a/big.lha::c.mod", NS(file_size=4096))
    assert not stream._too_big_to_fetch_whole(f"{root}:/a/small.lha::c.mod", NS(file_size=4096))


@pytest.mark.asyncio
async def test_a_big_tune_inside_a_zip_is_opened_but_not_fetched_for_track_info(env, monkeypatch):
    """A ZIP is read by ranges, so it is never too big to open — but the big
    tune inside it downloads for its play only, not for Track Info."""
    from soniqboom.api import stream
    from soniqboom.core import remote_zip
    from soniqboom.core.data import get_track
    p = env["web"] / "pub" / "music" / "artists" / "dj" / "mixzip.zip"
    p.parent.mkdir(parents=True)
    p.write_bytes(_zip({"long_mix.mod": _mod(b"Mix", 300_000)}))
    monkeypatch.setattr(stream, "_CATALOGUE_WHOLE_MAX", 200_000)
    monkeypatch.setattr(remote_zip, "_SUBSET_MIN_ARCHIVE", 100_000)
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("artists/dj/mixzip.zip")
    _Handler.log = []
    stream.catalogue_read_for_info(tid, await get_track(tid), "tester")
    for _ in range(200):
        if tid not in stream._INFO_READS:
            break
        await asyncio.sleep(0.05)
    t = store.get_track(tid)
    assert t["path"].endswith("::long_mix.mod") and not (t.get("mtime") or 0)
    ranges = [r for q, r in _file_gets() if q.endswith("mixzip.zip")]
    assert ranges and all(ranges)                                   # ranged reads only
    fetched = sum(int(r.split("-")[1]) - int(r.split("=")[1].split("-")[0]) + 1 for r in ranges)
    assert fetched < 100_000, fetched                               # its directory, not the tune


@pytest.mark.asyncio
async def test_a_file_too_big_to_download_whole_plays_straight_from_the_archive(env, monkeypatch):
    """Over ``_CATALOGUE_WHOLE_MAX``: each request's range comes straight from
    the archive — nothing lands in the remote cache, a stopped play stops its
    download — and its tags are read by ranges, once."""
    from starlette.requests import Request
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    big = env["web"] / "pub" / "music" / "artists" / "dj" / "huge.mp3"
    big.parent.mkdir(parents=True)
    _tagged_mp3(big, 3000)                                       # ~1.25 MB
    data = big.read_bytes()
    monkeypatch.setattr(stream, "_CATALOGUE_PROGRESSIVE_MIN", 200_000)
    monkeypatch.setattr(stream, "_CATALOGUE_WHOLE_MAX", 400_000)
    monkeypatch.setattr(stream, "_TAG_HEAD", 64 * 1024)
    monkeypatch.setattr(stream, "_TAG_TAIL", 64 * 1024)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", set())
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("artists/dj/huge.mp3")

    async def play(rng=None):
        h = [(b"range", rng.encode())] if rng else []
        req = Request({"type": "http", "method": "GET", "path": "/", "headers": h,
                       "query_string": b""})
        return await stream._maybe_remote_progressive(
            req, await get_track(tid), target_format=None, max_bitrate_kbps=0,
            force_transcode=False)
    resp = await play("bytes=1000-1999")
    assert resp.status_code == 206 and resp.headers["content-range"] == f"bytes 1000-1999/{len(data)}"
    assert b"".join([c async for c in resp.body_iterator]) == data[1000:2000]
    resp = await play()
    assert resp.status_code == 200 and resp.media_type == "audio/mpeg"
    assert b"".join([c async for c in resp.body_iterator]) == data
    assert remote_cache.get_cache().get_cached(cat.root, "/artists/dj/huge.mp3") is None
    for _ in range(100):
        if store.get_track(tid).get("title") == "Long Mix":
            break
        await asyncio.sleep(0.05)
    t = store.get_track(tid)
    assert t["title"] == "Long Mix" and 75 < t["duration"] < 82 and not (t.get("mtime") or 0)
    # A play stopped after its first chunk: its download stops with it.
    before = catalogue.stats()["bytes"]
    resp = await play()
    it = resp.body_iterator
    first = await it.__anext__()
    await it.aclose()
    await asyncio.sleep(0.2)
    assert first and catalogue.stats()["bytes"] - before < len(data) // 2


@pytest.mark.asyncio
async def test_a_big_file_plays_without_a_size_check_and_a_changed_one_is_asked_again(
        env, monkeypatch):
    """The straight-from-the-archive play takes the file's size from the
    archive's answer (an archive refusing HEAD still plays); a file that
    changed forgets the size asked of it before."""
    from fastapi import HTTPException
    from starlette.requests import Request
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    from soniqboom.core.filesource import get_source
    big = env["web"] / "pub" / "music" / "artists" / "dj" / "huge.mp3"
    big.parent.mkdir(parents=True)
    _tagged_mp3(big, 3000)
    data = big.read_bytes()
    monkeypatch.setattr(stream, "_CATALOGUE_WHOLE_MAX", 400_000)
    monkeypatch.setattr(stream, "_CATALOGUE_PROGRESSIVE_MIN", 200_000)
    cat = env["cat"]
    await cat.enable()
    tid = cat.track_id("artists/dj/huge.mp3")
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", {tid})        # (no tag read here)
    monkeypatch.setattr(_Handler, "do_HEAD", lambda self: self.send_error(405))

    async def play(rng):
        req = Request({"type": "http", "method": "GET", "path": "/",
                       "headers": [(b"range", rng.encode())], "query_string": b""})
        return await stream._maybe_remote_progressive(
            req, await get_track(tid), target_format=None, max_bitrate_kbps=0,
            force_transcode=False)
    resp = await play("bytes=100-")
    assert resp.status_code == 206 and resp.headers["content-range"] == \
        f"bytes 100-{len(data) - 1}/{len(data)}"
    assert b"".join([c async for c in resp.body_iterator]) == data[100:]
    resp = await play(f"bytes={len(data) + 10}-")
    assert resp.status_code == 416
    big.unlink()
    with pytest.raises(HTTPException) as ei:
        await play("bytes=0-99")
    assert ei.value.status_code == 404                              # (not a 500)
    src = get_source(cat.root)
    src._sizes["artists/dj/huge.mp3"] = 123
    cat._drop_cached("artists/dj/huge.mp3", before=1.0)
    assert "artists/dj/huge.mp3" not in src._sizes


@pytest.mark.asyncio
async def test_a_download_too_big_for_the_cache_is_refused_before_it_starts(env, monkeypatch):
    """A play that can't stream as is (a conversion, a Subsonic or Cast
    request) of a file bigger than half the remote cache is refused — never
    downloaded into it."""
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    big = env["web"] / "pub" / "music" / "artists" / "dj" / "huge.mp3"
    big.parent.mkdir(parents=True)
    _tagged_mp3(big, 3000)
    monkeypatch.setattr(stream, "_remote_cache_half", lambda: 300_000)
    cat = env["cat"]
    await cat.enable()
    tid = cat.track_id("artists/dj/huge.mp3")
    _Handler.log = []
    with pytest.raises(HTTPException) as ei:
        await stream._resolve_play_source(tid, await get_track(tid))
    assert ei.value.status_code == 413
    assert not [p for p, _r in _file_gets() if p.endswith("huge.mp3")]
    assert remote_cache.get_cache().get_cached(cat.root, "/artists/dj/huge.mp3") is None


@pytest.mark.asyncio
async def test_a_big_zip_on_a_server_ignoring_ranges_is_not_read_through(env, monkeypatch):
    """Its directory sits at its end: a server answering the whole file to
    that range is refused rather than read up to it."""
    from fastapi import HTTPException
    from soniqboom.core import remote_zip
    monkeypatch.setattr(remote_zip, "_SUBSET_MIN_ARCHIVE", 4096)
    rel = "disks/big/bigdisk.zip"
    p = env["web"] / "pub" / "music" / rel
    p.parent.mkdir(parents=True)
    p.write_bytes(_zip({f"t{i}.mod": _mod(b"T%d" % i, 600_000) for i in range(4)}))
    cat = env["cat"]
    await cat.enable()
    _Handler.no_ranges = {rel}
    before = catalogue.stats()["bytes"]
    with pytest.raises(HTTPException):
        await _play(cat.track_id(rel))
    assert catalogue.stats()["bytes"] - before < p.stat().st_size // 4   # (never read through)


def test_rows_an_opening_wrote_meanwhile_are_left_to_it():
    cat = online_sceneorg.SCENEORG_ONLINE
    saved = cat._expanded_at
    try:
        cat._expanded_at = {"a": 105.0, "b": 90.0}
        rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        assert [t["id"] for t in cat._not_opened_since(rows, 100.0)] == ["b", "c"]
    finally:
        cat._expanded_at = saved


def test_the_whole_download_limit_follows_the_cache_size(tmp_path):
    from soniqboom.core.remote_cache import RemoteCache
    assert RemoteCache(tmp_path / "a", 100).whole_download_limit == 50 * 1024 * 1024
    assert RemoteCache(tmp_path / "b", 10240).whole_download_limit == 5 * 1024 ** 3
    assert RemoteCache(tmp_path / "c", 64).whole_download_limit == 48 * 1024 * 1024


@pytest.mark.asyncio
async def test_a_download_bigger_than_half_the_real_cache_is_refused(env):
    """(No limit mocked: the remote cache's own, 512 MB here → 256 MB.)"""
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    cat, store = env["cat"], env["store"]
    await cat.enable()
    tid = cat.track_id("compilations/loose.mp3")
    store.update_track_fields_batch([(tid, {"file_size": 300 * 1024 * 1024})])
    _Handler.log = []
    with pytest.raises(HTTPException) as ei:
        await stream._resolve_play_source(tid, await get_track(tid))
    assert ei.value.status_code == 413 and not _file_gets()
    store.update_track_fields_batch([(tid, {"file_size": 200 * 1024 * 1024})])
    await stream._resolve_play_source(tid, await get_track(tid))     # (under it: fetched)


@pytest.mark.asyncio
async def test_a_zip_too_big_to_fetch_whole_is_never_the_fallback(env, monkeypatch):
    """A ranged read refused (or a member most of the archive): the whole
    archive is the fallback only below the collection's limit."""
    from soniqboom.core import remote_zip
    from soniqboom.core.filesource import get_source
    from soniqboom.core.remote_cache import RemoteCache
    monkeypatch.setattr(remote_zip, "_SUBSET_MIN_ARCHIVE", 4096)
    monkeypatch.setattr(RemoteCache, "whole_download_limit", property(lambda self: 100_000))
    rel = "disks/big/bigdisk.zip"
    p = env["web"] / "pub" / "music" / rel
    p.parent.mkdir(parents=True)
    p.write_bytes(_zip({f"t{i}.mod": _mod(b"T%d" % i, 200_000) for i in range(3)}))
    cat = env["cat"]
    await cat.enable()
    _Handler.no_ranges = {rel}
    assert catalogue.permit_play(f"{cat.root}:/{rel}::t0.mod")
    with pytest.raises(remote_zip.TooLargeToFetch):
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: remote_zip.archive_for_member(cat.root, "/" + rel, "t0.mod",
                                                        get_source(cat.root)))
    assert remote_cache.get_cache().get_cached(cat.root, "/" + rel) is None


@pytest.mark.asyncio
async def test_a_range_answered_without_its_total_gets_the_size_asked(env, monkeypatch):
    from starlette.requests import Request
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    big = env["web"] / "pub" / "music" / "artists" / "dj" / "huge.mp3"
    big.parent.mkdir(parents=True)
    _tagged_mp3(big, 3000)
    data = big.read_bytes()
    monkeypatch.setattr(stream, "_CATALOGUE_WHOLE_MAX", 400_000)
    monkeypatch.setattr(stream, "_CATALOGUE_PROGRESSIVE_MIN", 200_000)
    cat = env["cat"]
    await cat.enable()
    tid = cat.track_id("artists/dj/huge.mp3")
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", {tid})
    _Handler.star_total = {"artists/dj/huge.mp3"}
    req = Request({"type": "http", "method": "GET", "path": "/",
                   "headers": [(b"range", b"bytes=1000-")], "query_string": b""})
    resp = await stream._maybe_remote_progressive(req, await get_track(tid), target_format=None,
                                                  max_bitrate_kbps=0, force_transcode=False)
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 1000-{len(data) - 1}/{len(data)}"
    assert b"".join([c async for c in resp.body_iterator]) == data[1000:]


@pytest.mark.asyncio
async def test_a_listener_gone_mid_read_never_closes_under_the_read(monkeypatch):
    import threading
    from types import SimpleNamespace as NS
    from starlette.requests import Request
    from soniqboom.api import stream
    gate, log = threading.Event(), []

    class Up:
        size = 10 * 256 * 1024
        reads = 0
        def read(self, n):
            Up.reads += 1
            if Up.reads == 2:
                log.append("read-start")
                gate.wait(5)
                log.append("read-end")
            return b"x" * n
        def close(self):
            log.append("close")

    class Src:
        def open_stream(self, path, *, offset=0, lane="stream", length=None):
            return Up()
    track = NS(id="t1", path="https://example.invalid/pub:/a/b.mp3", mtime=0, duration=0)
    monkeypatch.setattr(stream, "_RANGED_INFO_DONE", {"t1"})
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": b""})
    resp = await stream._catalogue_passthrough(req, track, Src(), "/a/b.mp3", "audio/mpeg")
    it = resp.body_iterator

    async def consume():
        async for _c in it:
            pass
    task = asyncio.ensure_future(consume())
    for _ in range(100):
        if "read-start" in log:
            break
        await asyncio.sleep(0.01)
    task.cancel()                                     # the listener went
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.05)
    assert "close" not in log                         # not while the read runs
    gate.set()
    for _ in range(100):
        if "close" in log:
            break
        await asyncio.sleep(0.01)
    assert log == ["read-start", "read-end", "close"]

