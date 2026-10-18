# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Packages of an online collection (Aminet here; scene.org shares the code)
that hold nothing SoniqBoom plays, and the formats / archives that used to
make a playable package look empty.

Served by a local HTTP server (Aminet's INDEX + uploads).  Covers: a package
with nothing playable leaves the library at once — its play answers 410 with
what it holds (an Amiga program, a PC program …), its playlist entries are
parked, every open page is told (``catalogue_tracks_left``), a later request
for its id says the same, the next apply keeps it out; one that can't be
opened is marked corrupt (422) and stays; Face The Music modules (also with
no extension), LHA methods lhafile lacks and a disk image inside a ZIP play;
a package found empty comes back — and plays — when the formats this host
plays change; rows an older version marked "holds nothing" are unmarked."""
from __future__ import annotations

import http.server
import io
import struct
import threading
import zipfile
from pathlib import Path

import pytest

import _lha_writer as W
from soniqboom.core import archive, catalogue, metadata, online_aminet, online_catalog
from soniqboom.core import remote_cache
from soniqboom.core import store as store_mod
from soniqboom.core.store import TrackStore


def _mod(title: bytes) -> bytes:
    return title.ljust(20, b"\0") + b"\0" * 1064 + b"M.K." + b"\0" * 1024


def _ftm(title: bytes = b"Antom", artist: bytes = b"SSilk") -> bytes:
    """A Face The Music module (FTMN v3, one embedded sample, notes on
    channel 1 — libopenmpt >= 0.8 plays it)."""
    wave = bytes(([100] * 8 + [156] * 8) * 256)
    h = (b"FTMN" + bytes([3, 1]) + struct.pack(">HH", 4, 0x2000) + bytes([0, 0xFF, 63, 1, 4, 24])
         + title.ljust(32, b"\0") + artist.ljust(32, b"\0") + bytes([0, 0]))
    ev = bytes([0xA0, 0x4D, 0xF0, 0x03]) * 16
    chans = struct.pack(">hI", 0, len(ev)) + ev + b"".join(struct.pack(">hI", 0, 0)
                                                           for _ in range(7))
    return (h + b"Inst".ljust(30, b"\0") + b"\0\0" + chans
            + struct.pack(">HH", 0, len(wave) // 2) + wave)


def _adf(name: bytes, body: bytes) -> bytes:
    """A DD OFS ADF holding one file (root block 880, header 882, data 900…)."""
    img = bytearray(901120)
    img[0:4] = b"DOS\x00"
    bs = 512

    def put32(blk, off, v):
        struct.pack_into(">I", img, blk * bs + off, v & 0xFFFFFFFF)
    root, hdr = 880, 882
    put32(root, 24, hdr)
    put32(hdr, bs - 4, (-3) & 0xFFFFFFFF)
    put32(hdr, 0x144, len(body))
    img[hdr * bs + bs - 80] = len(name)
    img[hdr * bs + bs - 79: hdr * bs + bs - 79 + len(name)] = name
    nblk = (len(body) + 487) // 488
    put32(hdr, 0x08, nblk)
    for i in range(nblk):
        db = 900 + i
        put32(hdr, bs - 204 - i * 4, db)
        chunk = body[i * 488:(i + 1) * 488]
        put32(db, 0x0C, len(chunk))
        img[db * bs + 24: db * bs + 24 + len(chunk)] = chunk
    return bytes(img)


def _pm2_member() -> bytes:
    """A level-0 member packed with PMarc's -pm2- (its data stands in)."""
    raw = bytearray(W.member("MOD.b", b"xy", "-lh0-"))
    raw[2:7] = b"-pm2-"
    raw[1] = sum(raw[2:raw[0] + 2]) & 0xFF
    return bytes(raw)


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


HUNK = b"\x00\x00\x03\xf3" + bytes(range(256)) * 8
PACKAGES = {
    "8voic/Antom.lha": W.archive([("Antom.FTM", _ftm(), "-lh5-")]),
    "8voic/AntomPlain.lha": W.archive([("Antom", _ftm(b"Plain"), "-lh5-")]),
    "8voic/Old.lha": W.archive([("MOD.Old", _mod(b"Old one"), "-lh1-")], level=1),
    "8voic/Lz.lha": W.archive([("mod.Larc", _mod(b"LArc"), "-lz5-")], level=3, end=b""),
    "misc/Player.lha": W.archive([("Player", HUNK, "-lh5-"),
                                  ("Player.info", b"\xe3\x10" + b"\0" * 60, "-lh0-")]),
    "misc/PcDisk.zip": _zip({"DISK.EXE": b"MZ" + b"\0" * 400, "readme.txt": b"hi"}),
    "misc/Disk.zip": _zip({"music.adf": _adf(b"mod.tune", _mod(b"On a disk"))}),
    "misc/Damaged.zip": b"PK\x03\x04" + b"\x14" * 80,
    "misc/Pm.lha": W.member("MOD.a", _mod(b"a"), "-lh5-") + _pm2_member() + b"\0",
    "techn/Dreams.zip": _zip({"mod.Dreams": _mod(b"Dreams of Speed")}),
}
TREE = ("<html><body><ul><li><a href='/tree?path=mods/8voic'>mods/8voic</a> - 8 voice "
        "modules (300 packages)</li><li>mods/techn - Techno music (1737 packages)</li>"
        "<li>mods/misc - Miscellaneous songs (1561 packages)</li></ul></body></html>")
needs_ftm = pytest.mark.skipif(".ftm" not in getattr(metadata, "OPENMPT_EXTRA_EXTS", {".ftm"}),
                               reason="the installed libopenmpt doesn't read Face The Music")


class _Handler(http.server.SimpleHTTPRequestHandler):
    log: list = []

    def log_message(self, *a):
        pass

    def translate_path(self, path):
        if path.split("?", 1)[0].startswith("/tree"):
            return super().translate_path("/tree")
        return super().translate_path(path)

    def do_GET(self):
        type(self).log.append(("GET", self.path))
        return super().do_GET()


def _write_site(web: Path) -> None:
    rows = ["|", "| Aminet index", "|"]
    for rel, data in PACKAGES.items():
        cat, name = rel.split("/", 1)
        rows.append(f"{name:<30} mods/{cat:<5} {max(1, len(data) // 1024)}K 999 a package")
        p = web / "mods" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (web / "INDEX").write_text("\n".join(rows) + "\n", "latin-1")
    (web / "tree").write_text(TREE, "utf-8")


@pytest.fixture
def env(tmp_data_dir, tmp_path, monkeypatch):
    from soniqboom.api import library, stream
    from soniqboom.config import settings
    # (Aminet's client names its real host; the server here answers for it —
    # offline mode off as for test_online_aminet: conftest's strict guard
    # still refuses any real internet lookup.)
    monkeypatch.setenv("SONIQBOOM_OFFLINE", "0")
    store = TrackStore()
    monkeypatch.setattr(store_mod, "_store", store)
    cache = remote_cache.RemoteCache(tmp_data_dir / "remote-cache", 512)
    monkeypatch.setattr(remote_cache, "_cache", cache)
    monkeypatch.setattr(settings, "conversion_cache_dir", str(tmp_path / "conv"))
    monkeypatch.setattr(archive, "_LHA_BIN", None)          # (no lhasa: in-process only)
    zx = tmp_path / "zx"
    zx.mkdir()
    monkeypatch.setattr(stream, "_zip_extract_dir", lambda: zx)
    left: list = []

    async def _left(root, folder, ids, reasons=None):
        left.append((folder, list(ids), dict(reasons or {})))
    monkeypatch.setattr(library, "broadcast_catalogue_tracks_left", _left, raising=False)
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
                      ("_merge_tasks", {}), ("_merging", set()), ("_empty_ids", None)):
        monkeypatch.setattr(cat, attr, val, raising=False)
    yield {"store": store, "web": web, "cat": cat, "site": site, "left": left}
    srv.shutdown()
    catalogue.unregister_root(cat.root)
    from soniqboom.core.filesource import remove_source
    remove_source(cat.root)


async def _enable(env) -> dict:
    st = await env["cat"].enable()
    assert st["state"] == "ready", st
    return st


async def _play(tid):
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    path, ext, _n, pin = await stream._resolve_play_source(tid, await get_track(tid))
    if pin:
        stream._zip_unpin(pin)
    return path, ext


def _gets(rel):
    return [p for m, p in _Handler.log if m == "GET" and p == f"/mods/{rel}"]


@pytest.mark.asyncio
async def test_an_empty_package_leaves_at_once_and_says_what_it_holds(env):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.core.store import PARKED_PLAYLIST_IDS
    st = await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("misc/Player.lha")
    store.create_playlist("pl1", "mine", track_ids=[tid, cat.track_id("techn/Dreams.zip")])
    tunes = st["tunes"]
    with pytest.raises(HTTPException) as ei:
        await _play(tid)
    why = ei.value.detail
    assert ei.value.status_code == 410
    assert "Player.lha has no tunes SoniqBoom plays" in why and "an Amiga program" in why
    assert "corrupt" not in why.lower()
    # Gone at once — not at the next apply — its playlist entry parked.
    assert store.get_track(tid) is None
    assert tid in (store.get_config(PARKED_PLAYLIST_IDS) or [])
    assert cat.status()["tunes"] == tunes - 1
    # Every open page heard it, with the reason for this id.
    assert env["left"] and env["left"][-1][1] == [tid] and env["left"][-1][2][tid] == why
    # A later request for the id (a queue entry, the player's probe) says the same.
    gone = stream._track_missing(tid)
    assert gone.status_code == 410 and gone.detail == why
    assert stream._track_missing("00000000-0000-0000-0000-000000000000").status_code == 404
    # The next apply keeps it out.
    await cat.reapply()
    assert store.get_track(tid) is None


@pytest.mark.asyncio
async def test_a_pc_program_package_says_so(env):
    from fastapi import HTTPException
    await _enable(env)
    cat = env["cat"]
    with pytest.raises(HTTPException) as ei:
        await _play(cat.track_id("misc/PcDisk.zip"))
    assert ei.value.status_code == 410 and "a PC program, not tune files" in ei.value.detail


@pytest.mark.asyncio
async def test_a_damaged_package_is_marked_corrupt_and_stays(env):
    from fastapi import HTTPException
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("misc/Damaged.zip")
    with pytest.raises(HTTPException) as ei:
        await _play(tid)
    assert ei.value.status_code == 422 and "damaged" in ei.value.detail
    t = store.get_track(tid)
    assert t is not None and t["defect"] == "corrupt"
    # (the reader's own words — which can name local paths — stay in the log)
    assert t["defect_detail"] == "The archive is damaged — it can't be opened."
    from soniqboom.core import skip_rules
    assert skip_rules.entry_skipped(tid, 0)                      # passed by when dealt
    # Its cached copy went: the next pick fetches it again (a cut download
    # would come whole).
    n = len(_gets("misc/Damaged.zip"))
    with pytest.raises(HTTPException):
        await _play(tid)
    assert len(_gets("misc/Damaged.zip")) == n + 1


@needs_ftm
@pytest.mark.asyncio
async def test_a_face_the_music_package_plays(env):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("8voic/Antom.lha")
    path, ext = await _play(tid)
    assert ext == ".ftm" and Path(path).read_bytes() == _ftm()
    assert store.get_track(tid)["path"].endswith("8voic/Antom.lha::Antom.FTM")
    plain = cat.track_id("8voic/AntomPlain.lha")
    path, ext = await _play(plain)                               # (stored as "Antom")
    assert ext == ".ftm" and Path(path).read_bytes() == _ftm(b"Plain")
    assert store.get_track(plain)["path"].endswith("::Antom.ftm")


@pytest.mark.asyncio
@pytest.mark.parametrize("rel,member,title", [
    ("8voic/Old.lha", "MOD.Old.mod", b"Old one"),          # -lh1-, header level 1
    ("8voic/Lz.lha", "mod.Larc.mod", b"LArc"),             # -lz5-, level 3, no end marker
])
async def test_lha_packages_lhafile_turns_down_play(env, rel, member, title):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id(rel)
    path, ext = await _play(tid)
    assert ext == ".mod" and Path(path).read_bytes() == _mod(title)
    assert store.get_track(tid)["path"].endswith(f"{rel}::{member}")


@pytest.mark.asyncio
async def test_a_disk_image_inside_a_package_plays(env):
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("misc/Disk.zip")
    path, ext = await _play(tid)
    assert ext == ".mod" and Path(path).read_bytes() == _mod(b"On a disk")
    assert "misc/Disk.zip::music.adf::" in store.get_track(tid)["path"]


@needs_ftm
@pytest.mark.asyncio
async def test_a_package_found_empty_comes_back_when_the_formats_change(env, monkeypatch):
    """Opened on a host that doesn't play Face The Music: empty, gone.  The
    host learns the format (another formats fingerprint): the next apply lists
    the package again and settling opens it from its cached copy — the tune is
    listed and plays, the package isn't fetched again."""
    from fastapi import HTTPException
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("8voic/Antom.lha")
    with monkeypatch.context() as m:
        m.setattr(archive, "_EXT_SET", archive._EXT_SET - {".ftm"})
        m.setattr(archive, "_AMIGA_PREFIX_EXT",
                  {k: v for k, v in archive._AMIGA_PREFIX_EXT.items() if v != ".ftm"})
        m.setattr(archive, "formats_fingerprint", lambda: "no-ftm")
        archive._MAP_CACHE.clear()
        with pytest.raises(HTTPException) as ei:
            await _play(tid)
        assert ei.value.status_code == 410
        assert "a Face The Music module, which this server's libopenmpt doesn't play" in ei.value.detail
        assert store.get_track(tid) is None
    archive._MAP_CACHE.clear()
    fetched = len(_gets("8voic/Antom.lha"))
    await cat.reapply()                                       # listed again: one entry
    t = store.get_track(tid)
    assert t is not None and t["path"].endswith("8voic/Antom.lha") and not t.get("defect")
    assert await cat.settle_empty_packages() == 1             # opened again from the cache
    assert store.get_track(tid)["path"].endswith("::Antom.FTM")
    assert len(_gets("8voic/Antom.lha")) == fetched
    path, ext = await _play(tid)
    assert ext == ".ftm"


@pytest.mark.asyncio
async def test_rows_an_older_version_marked_empty_are_unmarked(env):
    """An older version marked an empty package's row corrupt ("holds nothing
    SoniqBoom plays") and kept it listed; its opening was stored by older
    naming rules — so the current rules list it again, without the mark (its
    next opening says what it holds)."""
    import sqlite3
    await _enable(env)
    cat, store = env["cat"], env["store"]
    rel = "misc/Player.lha"
    tid = cat.track_id(rel)
    e = cat.entry(rel)
    cat._save_opened(rel, f"{cat.package_marker(*e)}#n3", [], {})
    store.update_track_fields_batch([(tid, {"defect": "corrupt",
                                            "defect_detail": online_catalog._LEGACY_EMPTY_DETAIL})])
    await cat.reapply()
    t = store.get_track(tid)
    assert t is not None and t.get("defect") is None and t.get("defect_detail") is None
    con = sqlite3.connect(cat.packages_path())
    assert con.execute("SELECT holds FROM packages WHERE pkg = ?", (rel,)).fetchone() == (None,)
    con.close()


@pytest.mark.asyncio
async def test_track_info_of_an_empty_package_tells_the_pages(env):
    import asyncio
    from soniqboom.api import stream
    from soniqboom.core.data import get_track
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("misc/PcDisk.zip")
    stream.catalogue_read_for_info(tid, await get_track(tid), "tester")
    for _ in range(100):
        if store.get_track(tid) is None and env["left"]:
            break
        await asyncio.sleep(0.05)
    assert store.get_track(tid) is None
    assert env["left"][-1][1] == [tid] and "a PC program" in env["left"][-1][2][tid]


@pytest.mark.asyncio
async def test_a_package_packed_with_a_method_soniqboom_lacks_says_so(env):
    """Not damaged and not empty: refused by the reader (no lhasa here) —
    the reason names the method."""
    from fastapi import HTTPException
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("misc/Pm.lha")
    with pytest.raises(HTTPException) as ei:
        await _play(tid)
    assert ei.value.status_code == 410
    assert "packed with a method SoniqBoom doesn't unpack (-pm2-)" in ei.value.detail
    assert store.get_track(tid) is None


@pytest.mark.asyncio
async def test_a_formats_change_relists_only_packages_that_could_now_hold_a_tune(env, monkeypatch):
    """Player.lha holds an Amiga program: another formats fingerprint (a new
    format, another uade) doesn't put it back in the lists."""
    from fastapi import HTTPException
    await _enable(env)
    cat, store = env["cat"], env["store"]
    tid = cat.track_id("misc/Player.lha")
    with pytest.raises(HTTPException):
        await _play(tid)
    assert store.get_track(tid) is None
    monkeypatch.setattr(archive, "formats_fingerprint", lambda: "another")
    await cat.reapply()
    assert store.get_track(tid) is None
    assert await cat.settle_empty_packages() == 0


@pytest.mark.asyncio
async def test_track_info_of_a_gone_package_answers_410_with_the_reason(env):
    from fastapi import HTTPException
    from soniqboom.api import tracks
    await _enable(env)
    cat = env["cat"]
    tid = cat.track_id("misc/Player.lha")
    with pytest.raises(HTTPException):
        await _play(tid)
    with pytest.raises(HTTPException) as ei:
        await tracks.get_track_extended(tid, None)
    assert ei.value.status_code == 410 and "an Amiga program" in ei.value.detail
    assert "music disk" not in ei.value.detail                    # (misc/: no disks folder)


@pytest.mark.asyncio
async def test_members_lhasa_listed_keep_their_user_data_under_the_built_in_names(env):
    """An Aminet package an older version opened with lhasa (``DIR/x``
    member names) and the built-in reader re-opens (``DIR\\x``): the member's
    playlist entry follows it to its new row."""
    await _enable(env)
    cat, store = env["cat"], env["store"]
    rel = "8voic/Old.lha"
    before = [["DIR/MOD.a.mod", 10, "DIR/MOD.a"], ["DIR/MOD.b.mod", 10, "DIR/MOD.b"]]
    now = [["DIR\\MOD.a.mod", 10, "DIR\\MOD.a"], ["DIR\\MOD.b.mod", 10, "DIR\\MOD.b"]]
    old_id = cat.track_id(f"{rel}::DIR/MOD.b.mod")
    new_id = cat.track_id(f"{rel}::DIR\\MOD.b.mod")
    for tid, member in ((old_id, "DIR/MOD.b.mod"), (new_id, "DIR\\MOD.b.mod")):
        store.upsert_tracks_batch([{"id": tid, "path": cat.track_path(f"{rel}::{member}"),
                                    "title": "b", "format": "ProTracker"}])
    store.create_playlist("pl2", "mine", track_ids=[old_id])
    assert await cat._carry_renamed_members(rel, before, now) == 1
    assert store.get_track(old_id) is None
    pl = next(p for p in store.list_playlists() if p["id"] == "pl2")
    ids = [e.get("id") if isinstance(e, dict) else e for e in pl["track_ids"]]
    assert ids == [new_id]
