# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Faster covers in lists (2.1.2): the link-speed check and what it sets
(core/link_speed, the FTP pool's interactive reserve), what each folder holds
(core/folder_art_index), folder covers of network shares fetched in the
background (core/folder_covers), stamps from stored values (api/art G), list
thumbnails made by the scan (scanner A), and the batched cover status (F)."""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from soniqboom.api import art as A
# (imported here, at collection: a module first imported inside a test that
# patches ``soniqboom.core.store.get_store`` — test_dlna_browse's fixture —
# would keep that test's store for good)
from soniqboom.core import scanner as _scanner_import  # noqa: F401
from soniqboom.core import folder_art_index as fai
from soniqboom.core import folder_covers as fc
from soniqboom.core import link_speed as ls


def _jpeg(px: int = 600, colour=(200, 30, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (px, px), colour).save(buf, "JPEG", quality=80)
    return buf.getvalue()


@pytest.fixture
def index(tmp_path, monkeypatch):
    """A fresh, private folder index (and art cache folder)."""
    monkeypatch.setattr(fai, "_path", lambda: tmp_path / "folders.json")
    monkeypatch.setattr(A.art_cache, "get_art_cache_dir", lambda: tmp_path / "art")
    fai._reset_for_tests()
    fc._reset_for_tests()
    A._verified.clear()
    yield tmp_path
    fai._reset_for_tests()
    fc._reset_for_tests()


# ── link_speed ───────────────────────────────────────────────────────────────

class _Probed:
    def __init__(self, rtt):
        self.rtt = rtt
        self.calls = 0
        self._pool = SimpleNamespace(reserve=None,
                                     set_interactive_reserve=lambda n: setattr(self._pool, "reserve", n))

    def probe_rtt(self, n):
        self.calls += 1
        if isinstance(self.rtt, Exception):
            raise self.rtt
        return self.rtt


def test_a_fast_link_keeps_the_old_widths_and_gets_the_reserve():
    src = _Probed(0.002)
    assert ls.measure(src) is True
    lim = ls.limits(src)
    assert lim == ls.FAST
    assert lim._replace(ftp_reserve=0) == ls._DEFAULT          # the old widths
    assert src._pool.reserve == 1                              # measured upside on a LAN too


def test_a_slow_link_gets_more_at_once_and_the_ftp_reserve():
    src = _Probed(0.064)
    assert ls.measure(src) is False
    lim = ls.limits(src)
    assert lim == ls.SLOW
    assert lim.warm_width > ls._DEFAULT.warm_width
    assert lim.backfill > ls._DEFAULT.backfill
    assert src._pool.reserve == 1


def test_the_threshold_sits_between_the_two_measured_links():
    assert ls.measure(_Probed(ls.FAST_RTT_S)) is True
    assert ls.measure(_Probed(ls.FAST_RTT_S + 0.001)) is False


def test_a_failed_probe_is_unknown_and_keeps_the_old_limits():
    src = _Probed(OSError("down"))
    assert ls.measure(src) is None
    assert ls.limits(src) == ls._DEFAULT
    assert src._pool.reserve == 0
    assert ls.limits(None) == ls._DEFAULT                     # a local folder


def test_an_unmeasured_link_is_probed_in_the_background_once():
    src = _Probed(0.064)
    assert ls.limits(src) == ls._DEFAULT                       # not known yet: never blocks
    for _ in range(100):
        if getattr(src, "_sb_link", None) is not None:
            break
        time.sleep(0.01)
    assert ls.limits(src) == ls.SLOW
    ls.limits(src)
    assert src.calls == 1                                      # fresh: not probed again


def test_a_stale_measurement_is_taken_again(monkeypatch):
    src = _Probed(0.002)
    ls.measure(src)
    src._sb_link = src._sb_link._replace(at=time.monotonic() - ls._TTL - 1)
    src.rtt = 0.064
    ls.limits(src)
    for _ in range(100):
        if src.calls == 2 and src._sb_link.fast is False:
            break
        time.sleep(0.01)
    assert ls.limits(src) == ls.SLOW


# ── the FTP pool's interactive reserve (E) ───────────────────────────────────

def _pool(max_size=4):
    from soniqboom.core.filesource import _FTPConnectionPool

    class _Conn:
        def voidcmd(self, _c):
            return "200"

        def close(self):
            pass
    return _FTPConnectionPool(lambda: _Conn(), max_size)


def _hold(pool, lane, n, stop):
    started = threading.Barrier(n + 1)

    def run():
        with pool.borrow(lane=lane):
            started.wait()
            stop.wait(10)
    ts = [threading.Thread(target=run, daemon=True) for _ in range(n)]
    for t in ts:
        t.start()
    started.wait(5)
    return ts


def test_without_the_reserve_a_scan_takes_every_connection():
    from soniqboom.core import filesource as FS
    pool, stop = _pool(4), threading.Event()
    ts = _hold(pool, "scan", 4, stop)
    assert pool._in_use_scan == 4
    stop.set()
    for t in ts:
        t.join(5)


def test_with_the_reserve_a_scan_leaves_one_for_a_cover():
    from soniqboom.core import filesource as FS
    pool, stop = _pool(4), threading.Event()
    pool.set_interactive_reserve(1)
    ts = _hold(pool, "scan", 3, stop)
    FS._BORROW_WAIT.s = 0.3
    try:
        with pytest.raises(TimeoutError):
            with pool.borrow(lane="scan"):
                pass                                            # the 4th scan waits
        t0 = time.monotonic()
        with pool.borrow(lane="browse"):
            pass                                                # a cover does not
        assert time.monotonic() - t0 < 0.2
    finally:
        FS._BORROW_WAIT.s = None
        stop.set()
        for t in ts:
            t.join(5)


def test_the_reserve_never_leaves_a_scan_fewer_than_two():
    pool = _pool(2)
    pool.set_interactive_reserve(1)
    with pool._cond:
        pool._in_use_scan = 1
        assert pool._scan_at_reserve() is False


def test_lifting_the_reserve_wakes_a_waiting_scan():
    from soniqboom.core import filesource as FS
    pool, stop = _pool(3), threading.Event()
    pool.set_interactive_reserve(1)
    ts = _hold(pool, "scan", 2, stop)
    got = threading.Event()

    def scan():
        FS._BORROW_WAIT.s = 5
        with pool.borrow(lane="scan"):
            got.set()
    t = threading.Thread(target=scan, daemon=True)
    t.start()
    time.sleep(0.1)
    assert not got.is_set()
    pool.set_interactive_reserve(0)
    assert got.wait(2)
    stop.set()
    for x in ts + [t]:
        x.join(5)


# ── folder_art_index (B) ─────────────────────────────────────────────────────

def test_keys_follow_the_folder_not_the_file(index):
    a = fai.key_for_path("/Music/Album/01.flac")
    assert a == fai.key_for_path("/Music/Album/02.flac") == fai.key_for_local_dir("/Music/Album")
    assert fai.key_for_path("/Music/Album/pack.zip::x.mod") == a        # an archive's folder
    share = "ftp://nas.invalid/Music"
    r = fai.key_for_path(f"{share}:/A/01.mp3")
    assert r == fai.key_for_remote_dir(share, "/A") == fai.key_for_remote_dir(share, "A")
    assert r != fai.key_for_path("ftp://other.invalid/Music:/A/01.mp3")  # two shares, one /A
    assert fai.key_for_path(f"{share}:/top.mp3") == fai.key_for_remote_dir(share, "/")


def test_a_recorded_folder_is_known_and_kept_on_disk(index):
    k = fai.key_for_local_dir("/Music/A")
    assert fai.lookup(k) is None
    fai.record(k, "Cover.JPG", mtime=12.0, dir_hash="h1")
    fai.record(fai.key_for_local_dir("/Music/B"), None)
    fai.flush()
    fai._reset_for_tests()
    e = fai.lookup(k)
    assert (e.name, e.mtime, e.dir_hash) == ("Cover.JPG", 12.0, "h1")
    assert fai.lookup(fai.key_for_local_dir("/Music/B")).name is None


def test_a_remote_none_expires_a_local_one_lasts_longer(index, monkeypatch):
    r = fai.key_for_remote_dir("ftp://h.invalid/M", "/A")
    loc = fai.key_for_local_dir("/M/A")
    fai.record(r, None)
    fai.record(loc, None)
    later = time.time() + fai._NONE_TTL_REMOTE + 1
    monkeypatch.setattr(fai.time, "time", lambda: later)
    assert fai.lookup(r) is None
    assert fai.lookup(loc) is not None


def test_forgetting_all_drops_writers_that_read_before_it(index):
    k = fai.key_for_local_dir("/M/A")
    gen = fai.generation()
    fai.forget_all()
    fai.record(k, None, gen=gen)                    # read under the old names
    assert fai.lookup(k) is None
    fai.record(k, None, gen=fai.generation())
    assert fai.lookup(k) is not None


def test_a_folder_that_gains_an_image_is_announced_once(index):
    k = fai.key_for_local_dir("/M/A")
    fai.record(k, None, dir_hash="h1")
    fai.record(k, "folder.jpg", dir_hash="h1")
    assert fai.take_flips() == ["h1"]
    assert fai.take_flips() == []
    fai.record(fai.key_for_local_dir("/M/B"), "folder.jpg", dir_hash="h2")
    assert fai.take_flips() == []                   # never told "none": nothing to take back


def test_pick_follows_the_names_order_and_keeps_the_real_case(index):
    assert fai.pick(["COVER.jpg", "Folder.JPG"], ["folder.jpg", "cover.jpg"]) == "Folder.JPG"
    assert fai.pick(["a.mp3"], ["folder.jpg"]) is None


# ── local folders through the art endpoint (B, G) ───────────────────────────

def test_a_local_folder_is_listed_once_then_answered_from_the_index(index, tmp_path, monkeypatch):
    d = tmp_path / "Album"
    d.mkdir()
    (d / "Folder.jpg").write_bytes(_jpeg(40))
    calls = []
    real = os.listdir
    monkeypatch.setattr(A.os, "listdir", lambda p: (calls.append(str(p)), real(p))[1])
    pri = ["folder.jpg"]
    data, mime = A._folder_art_local_indexed(d, pri, "h1")
    assert data == (d / "Folder.jpg").read_bytes() and mime == "image/jpeg"
    assert A._folder_art_local_indexed(d, pri, "h1")[0] == data
    assert calls == [str(d)]                       # the second read went by the name

    e = tmp_path / "NoArt"
    e.mkdir()
    assert A._folder_art_local_indexed(e, pri) == (None, None)
    assert A._folder_art_local_indexed(e, pri) == (None, None)
    assert calls == [str(d), str(e)]               # "none" is not listed again


def test_a_recorded_image_that_is_gone_is_listed_again(index, tmp_path):
    d = tmp_path / "Album"
    d.mkdir()
    fai.record(fai.key_for_local_dir(str(d)), "cover.jpg")
    (d / "folder.jpg").write_bytes(b"\xff\xd8x")
    data, _ = A._folder_art_local_indexed(d, ["cover.jpg", "folder.jpg"])
    assert data == b"\xff\xd8x"
    assert fai.lookup(fai.key_for_local_dir(str(d))).name == "folder.jpg"


def test_an_unreachable_local_folder_records_nothing(index, tmp_path):
    gone = tmp_path / "offline"
    assert A._folder_art_local_indexed(gone, ["folder.jpg"]) == (None, None)
    assert fai.lookup(fai.key_for_local_dir(str(gone))) is None


async def test_the_stamp_of_a_recorded_local_folder_needs_no_live_check(index, tmp_path, monkeypatch):
    f = tmp_path / "A" / "t.mp3"
    f.parent.mkdir()
    f.write_bytes(b"x")
    track = SimpleNamespace(id="t1", path=str(f), mtime=100.0, dir_hash="h1")
    assert A._known_source_mtime(track) is A._UNKNOWN          # folder not recorded: live
    fai.record(fai.key_for_local_dir(str(f.parent)), "folder.jpg", mtime=250.0)
    verified = []
    monkeypatch.setattr(A, "_verify_later", lambda t: verified.append(t.id))
    live = []

    async def _live(p):
        live.append(p)
        return 1.0
    monkeypatch.setattr(A, "_source_mtime_for_async", _live)
    assert await A._source_stamp(track) == 250.0                # the folder image is newer
    assert live == [] and verified == ["t1"]
    share = SimpleNamespace(id="r1", path="ftp://h.invalid/M:/A/t.mp3", mtime=5.0)
    assert await A._source_stamp(share) == 1.0 and live         # a share: as before
    assert await A._source_stamp(SimpleNamespace(id="w", path="https://x.invalid/a.mp3"),
                                 urls=False) is None


async def test_the_background_check_finds_a_retagged_file(index, tmp_path, monkeypatch):
    d = tmp_path / "A"
    d.mkdir()
    f = d / "t.mp3"
    f.write_bytes(b"x")
    os.utime(f, (500.0, 500.0))
    track = SimpleNamespace(id="t1", path=str(f), mtime=100.0, dir_hash="h1")
    fai.record(fai.key_for_local_dir(str(d)), None)
    absent = {"t1"}
    store = SimpleNamespace(discard_art_absent=lambda tid: absent.discard(tid),
                            is_art_absent=lambda tid: tid in absent,
                            filter_tracks=lambda **k: [{"id": "t1", "path": str(f)}])
    monkeypatch.setattr(A, "get_store", lambda: store)
    monkeypatch.setattr(A, "_is_art_absent_persisted", lambda tid, live: live <= 100.0)
    cleared, resolved = [], []
    monkeypatch.setattr(A, "_clear_art_absent_persisted", cleared.append)
    monkeypatch.setattr(A, "_resolve_in_background", resolved.append)
    await A._verify(track)
    assert absent == set() and cleared == ["t1"] and resolved == ["t1"]
    (d / "cover.jpg").write_bytes(b"\xff\xd8")                  # dropped in later
    await asyncio.to_thread(A._verify_sync, str(f), "h1", fai.generation())
    assert fai.lookup(fai.key_for_local_dir(str(d))).name == "cover.jpg"


def test_the_background_check_runs_once_per_folder_per_window(index, monkeypatch):
    started = []

    async def _v(t):
        started.append(t.id)

    async def main():
        monkeypatch.setattr(A, "_verify", _v)
        for tid in ("a", "b"):
            A._verify_later(SimpleNamespace(id=tid, path="/M/A/x.mp3"))
        A._verify_later(SimpleNamespace(id="c", path="/M/B/x.mp3"))
        await asyncio.sleep(0)
    asyncio.run(main())
    assert started == ["a", "c"]


async def test_a_folder_that_gains_an_image_tells_its_tracks(index, monkeypatch):
    sent = []

    async def _broadcast(m):
        sent.append(m)
    import soniqboom.api.library as LIB
    monkeypatch.setattr(LIB, "_broadcast", _broadcast)
    store = SimpleNamespace(filter_tracks=lambda dir_hash, limit: [{"id": "t1"}, {"id": "t2"}]
                            if dir_hash == "h1" else [],
                            discard_art_absent=lambda tid: None)
    monkeypatch.setattr(A, "get_store", lambda: store)
    k = fai.key_for_local_dir("/M/A")
    fai.record(k, None, dir_hash="h1")
    fai.record(k, "folder.jpg", dir_hash="h1")
    assert await A.announce_folder_changes() == 2
    assert [m["track_id"] for m in sent] == ["t1", "t2"]
    assert await A.announce_folder_changes() == 0


# ── network-share folder covers in the background (C, D, H) ─────────────────

class _Src:
    """A share whose folders are given as {dir: {name: bytes}}."""

    def __init__(self, folders, *, fail=False, gate=None):
        self.folders = folders
        self.listed, self.read = [], []
        self.fail, self.gate = fail, gate

    def list_dir(self, d):
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise OSError("share down")
        self.listed.append(d)
        return [SimpleNamespace(name=n, path=f"{d.rstrip('/')}/{n}", is_dir=False)
                for n in self.folders.get(d, {})]

    def read_file(self, p, lane="stream"):
        assert lane == "browse"                     # covers wait behind playback
        self.read.append(p)
        d, n = p.rsplit("/", 1)
        return self.folders[d or "/"][n]


SHARE = "ftp://nas.invalid/Music"


def _tr(tid, rel, **kw):
    return SimpleNamespace(id=tid, path=f"{SHARE}:{rel}", dir_hash=kw.pop("dir_hash", "dh"),
                           cover_art=kw.pop("cover_art", None), **kw)


@pytest.fixture
def share(index, monkeypatch):
    """``share(folders)`` → the source; records what the jobs hand on."""
    got = SimpleNamespace(persisted=[], online=[], stored=[], src=None)
    import soniqboom.core.filesource as FS
    monkeypatch.setattr(FS, "get_source", lambda root: got.src if root == SHARE else None)
    monkeypatch.setattr(A, "_persist_and_notify",
                        lambda tid, data, **kw: got.persisted.append((tid, data)))
    monkeypatch.setattr(A, "_clear_art_absent_persisted", lambda tid: None)

    async def _online(tid):
        got.online.append(tid)
        return False
    monkeypatch.setattr(A, "ask_online_for", _online)

    async def _store(key, data, size):
        got.stored.append((key, size))
    monkeypatch.setattr(A.art_cache, "store_art", _store)
    monkeypatch.setattr(ls, "limits", lambda src: ls._DEFAULT)

    def make(folders, **kw):
        got.src = _Src(folders, **kw)
        return got.src
    got.make = make
    return got


async def _drain():
    for _ in range(200):
        if not fc._pending and not fc._running:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.01)


async def test_one_folder_is_listed_and_downloaded_once_for_all_its_rows(share):
    img = _jpeg(40)
    src = share.make({"/A": {"folder.jpg": img, "01.mp3": b""}})
    for i in range(6):
        assert fc.request(_tr(f"t{i}", f"/A/{i:02d}.mp3")) is True
    assert fc.pending(_tr("t0", "/A/00.mp3"))
    await _drain()
    assert src.listed == ["/A"] and src.read == ["/A/folder.jpg"]
    assert sorted(t for t, _ in share.persisted) == [f"t{i}" for i in range(6)]
    assert share.stored == [(f"folder:{fai.key_for_remote_dir(SHARE, '/A')}", "full")]
    assert fai.lookup(fai.key_for_remote_dir(SHARE, "/A")).name == "folder.jpg"
    assert not fc.pending(_tr("t0", "/A/00.mp3"))


async def test_a_folder_without_an_image_is_recorded_and_its_rows_go_online(share):
    src = share.make({"/B": {"01.mp3": b""}})
    fc.request(_tr("t1", "/B/01.mp3"))
    fc.request(_tr("t2", "/B/02.mp3"))
    await _drain()
    assert fai.lookup(fai.key_for_remote_dir(SHARE, "/B")).name is None
    assert sorted(share.online) == ["t1", "t2"] and share.persisted == []
    assert src.read == []


async def test_a_failed_folder_records_nothing_waits_and_lets_the_online_cover_stand_in(share):
    share.make({}, fail=True)
    t = _tr("t1", "/C/01.mp3")
    fc.request(t)
    await _drain()
    assert fai.lookup(fai.key_for_remote_dir(SHARE, "/C")) is None   # unknown, never "none"
    assert share.online == ["t1"]
    assert fc.unreachable(t)
    assert fc.request(t) is False                                     # not again at once
    fc._failed_at[fai.key_for_remote_dir(SHARE, "/C")] -= fc._FAIL_COOLDOWN_S + 1
    share.src.fail = False
    share.src.folders = {"/C": {"cover.jpg": b"\xff\xd8"}}
    assert fc.request(t) is True
    await _drain()
    assert not fc.unreachable(t)                                      # the share answered
    assert share.persisted == [("t1", b"\xff\xd8")]


async def test_the_newest_folder_asked_goes_first(share, monkeypatch):
    gate = threading.Event()
    src = share.make({f"/{n}": {"folder.jpg": b"\xff\xd8" + n.encode()} for n in "ABCD"}, gate=gate)
    monkeypatch.setattr(ls, "limits", lambda s: ls._DEFAULT._replace(folder_jobs=1))
    fc.request(_tr("a", "/A/1.mp3"))                 # starts at once (and waits on the gate)
    await asyncio.sleep(0.05)
    fc.request(_tr("b", "/B/1.mp3"))
    fc.request(_tr("c", "/C/1.mp3"))
    fc.request(_tr("d", "/D/1.mp3"))
    fc.request(_tr("b2", "/B/2.mp3"))                # B asked again: on screen now
    gate.set()
    await _drain()
    assert src.listed == ["/A", "/B", "/D", "/C"]


async def test_a_listing_is_reused_for_a_while(share):
    src = share.make({"/A": {"x.mp3": b""}})
    fc._listing(src, SHARE, "/A")
    fc._listing(src, SHARE, "/A")
    assert src.listed == ["/A"]


async def test_online_collections_and_local_tracks_get_no_folder_job(share, monkeypatch):
    from soniqboom.core import catalogue
    share.make({})
    monkeypatch.setattr(catalogue, "is_catalogue_path", lambda p: True)
    assert fc.request(_tr("m1", "/mods/a.mod")) is False
    monkeypatch.setattr(catalogue, "is_catalogue_path", lambda p: False)
    assert fc.request(SimpleNamespace(id="l1", path="/Music/a.mp3", dir_hash="x")) is False


# ── the art endpoint with network-share tracks ──────────────────────────────

class _Req:
    def __init__(self, inm=None):
        self.headers = {"if-none-match": inm} if inm else {}


@pytest.fixture
def endpoint(share, monkeypatch):
    """``A.cover_art`` over one share track ``r1`` (no embedded cover)."""
    tracks = {}

    async def _get(tid):
        return tracks.get(tid)
    monkeypatch.setattr(A, "get_track", _get)

    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(A.art_cache, "get_art", _none)
    monkeypatch.setattr(A, "_art_cached_mtime", lambda *_a: None)
    monkeypatch.setattr(A, "_is_art_absent_persisted", lambda *a: False)

    class _Store:
        def is_art_absent(self, tid):
            return False

        def discard_art_absent(self, tid):
            pass

        def get_config(self, key, default=None):
            return default
    monkeypatch.setattr(A, "get_store", lambda: _Store())

    async def _no_online(track):
        return A.online_cover.NOTHING
    monkeypatch.setattr(A.online_cover, "state", _no_online)
    share.tracks = tracks
    return share


async def test_a_share_track_answers_at_once_and_its_folder_comes_after(endpoint):
    src = endpoint.make({"/A": {"folder.jpg": b"\xff\xd8"}})
    endpoint.tracks["r1"] = _tr("r1", "/A/01.mp3")
    r = await A.cover_art("r1", _Req(), size="sm", fallback="none")
    assert r.status_code == 204
    assert r.headers["Cache-Control"] == "no-cache"              # not to be kept: a cover comes
    assert src.listed == []                                      # nothing listed in the request
    await _drain()
    assert endpoint.persisted == [("r1", b"\xff\xd8")]           # → art_ready


async def test_a_share_folder_known_to_hold_none_is_not_asked(endpoint):
    src = endpoint.make({"/A": {"folder.jpg": b"\xff\xd8"}})
    endpoint.tracks["r1"] = _tr("r1", "/A/01.mp3")
    fai.record(fai.key_for_remote_dir(SHARE, "/A"), None)
    r = await A.cover_art("r1", _Req(), size="sm", fallback="none")
    assert r.status_code == 204 and r.headers["Cache-Control"] == A._NO_ART_CC
    await _drain()
    assert src.listed == [] and not fc._pending


async def test_a_revalidated_share_row_starts_its_folder_job(endpoint, monkeypatch):
    src = endpoint.make({"/A": {"folder.jpg": b"\xff\xd8"}})
    endpoint.tracks["r1"] = _tr("r1", "/A/01.mp3")
    fai.record(fai.key_for_remote_dir(SHARE, "/A"), None)
    first = await A.cover_art("r1", _Req(), size="sm", fallback="none")
    fai.forget_all()                                             # (a setting changed)
    monkeypatch.setattr(src, "gate", threading.Event())
    again = await A.cover_art("r1", _Req(first.headers["ETag"]), size="sm", fallback="none")
    assert again.status_code == 304 and again.headers["Cache-Control"] == "no-cache"
    src.gate.set()
    await _drain()
    assert src.listed == ["/A"]


async def test_own_cover_missing_for_a_share_track(endpoint):
    t = _tr("r1", "/A/01.mp3")
    assert A._own_cover_missing("r1", t) is False                # folder not known yet
    fai.record(fai.key_for_remote_dir(SHARE, "/A"), "folder.jpg")
    assert A._own_cover_missing("r1", t) is False                # it holds an image
    fai.record(fai.key_for_remote_dir(SHARE, "/A"), None)
    assert A._own_cover_missing("r1", t) is True
    assert A._own_cover_missing("r2", _tr("r2", "/A/02.mp3", cover_art="/api/art/r2")) is False
    fc._failed_at[fai.key_for_remote_dir(SHARE, "/Z")] = time.monotonic()
    assert A._own_cover_missing("r3", _tr("r3", "/Z/01.mp3")) is True   # its folder unreachable


async def test_cast_and_subsonic_hear_that_a_cover_may_still_come(endpoint):
    gate = threading.Event()
    endpoint.make({"/A": {"folder.jpg": b"\xff\xd8"}}, gate=gate)
    endpoint.tracks["r1"] = _tr("r1", "/A/01.mp3")
    fc.request(endpoint.tracks["r1"])
    assert await A.online_cover_for("r1") == (None, True)
    gate.set()
    await _drain()


# ── the batched cover status (F) ─────────────────────────────────────────────

@pytest.fixture
def status(index, monkeypatch):
    rows = {}
    store = SimpleNamespace(get_track=lambda tid: rows.get(tid),
                            get_config=lambda k, d=None: d, is_art_absent=lambda tid: False)
    monkeypatch.setattr(A, "get_store", lambda: store)
    monkeypatch.setattr(A.art_cache, "art_exists", lambda tid, size: tid in status_cached)
    status_cached: set = set()

    async def _get(tid):
        d = rows.get(tid)
        return SimpleNamespace(**d) if d else None
    monkeypatch.setattr(A, "get_track", _get)
    online = {}

    async def _state(track):
        return online.get(track.id, A.online_cover.NOTHING)
    monkeypatch.setattr(A.online_cover, "state", _state)
    return SimpleNamespace(rows=rows, cached=status_cached, online=online)


class _JsonReq:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


async def test_status_names_only_tracks_known_to_have_no_cover(status, monkeypatch):
    monkeypatch.setattr(A.online_cover, "lookups_off", lambda: False)
    none_dir, img_dir = "/M/none", "/M/img"
    fai.record(fai.key_for_local_dir(none_dir), None)
    fai.record(fai.key_for_local_dir(img_dir), "folder.jpg")
    r = status.rows
    r["n1"] = {"id": "n1", "path": f"{none_dir}/1.mod", "dir_hash": "a"}
    r["emb"] = {"id": "emb", "path": f"{none_dir}/2.mp3", "cover_art": "/api/art/emb", "dir_hash": "a"}
    r["img"] = {"id": "img", "path": f"{img_dir}/1.mp3", "dir_hash": "b"}
    r["unk"] = {"id": "unk", "path": "/M/unknown/1.mod", "dir_hash": "c"}
    r["cached"] = {"id": "cached", "path": f"{none_dir}/3.mod", "dir_hash": "a"}
    status.cached.add("cached")
    r["online"] = {"id": "online", "path": f"{none_dir}/4.mp3", "dir_hash": "a"}
    status.online["online"] = A.online_cover.State(subject=object(), key="k", record=None)
    r["lasting"] = {"id": "lasting", "path": f"{none_dir}/5.mp3", "dir_hash": "a"}
    status.online["lasting"] = A.online_cover.State(subject=object(), key="k", record={"ok": False})
    out = await A.art_status(_JsonReq({"ids": list(r) + ["gone"]}))
    assert sorted(out["none"]) == ["lasting", "n1"]
    monkeypatch.setattr(A.online_cover, "lookups_off", lambda: True)   # offline: none can come
    out = await A.art_status(_JsonReq({"ids": ["online"]}))
    assert out["none"] == ["online"]


async def test_status_refuses_a_body_without_ids(status):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await A.art_status(_JsonReq({"x": 1}))
    out = await A.art_status(_JsonReq({"ids": ["a"] * 1000}))
    assert out == {"none": []}


# ── the scan: folder images from the walk, list thumbnails (A) ──────────────

def test_the_walk_records_each_folder_from_its_own_names(index, tmp_path):
    from soniqboom.core import scanner
    d = tmp_path / "A"
    d.mkdir()
    (d / "Cover.jpg").write_bytes(b"\xff\xd8")
    scanner._note_folder_image(str(d), ["01.mp3", "Cover.jpg"], ["cover.jpg"], complete=True, gen=fai.generation())
    e = fai.lookup(fai.key_for_local_dir(str(d)))
    assert e.name == "Cover.jpg" and e.mtime and e.dir_hash
    scanner._note_folder_image(str(tmp_path / "B"), ["01.mp3"], ["cover.jpg"], complete=False, gen=fai.generation())
    assert fai.lookup(fai.key_for_local_dir(str(tmp_path / "B"))) is None   # a partial listing
    scanner._note_folder_image(str(tmp_path / "B"), ["01.mp3"], ["cover.jpg"], complete=True, gen=fai.generation())
    assert fai.lookup(fai.key_for_local_dir(str(tmp_path / "B"))).name is None


def _aiff_with_cover(path: Path, cover: bytes) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1",
                        "-metadata", "title=T", "-write_id3v2", "1", str(path)], capture_output=True)
    if r.returncode:
        pytest.fail(r.stderr.decode()[-300:])
    from mutagen.aiff import AIFF
    from mutagen.id3 import APIC
    a = AIFF(str(path))
    a.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=cover))
    a.save()
    return path


def test_the_extract_worker_makes_the_list_thumbnail(tmp_path):
    from PIL import Image
    from soniqboom.core import scanner
    f = _aiff_with_cover(tmp_path / "t.aiff", _jpeg(800))
    _p, meta, sm, _lg = scanner._extract_one(f)
    assert not isinstance(meta, str) and meta.cover_art
    assert max(Image.open(io.BytesIO(sm)).size) <= 200
    out, _secs = scanner._extract_batch((str(f),), 60.0)
    assert out[0]["cover_art"] == f"/api/art/{out[0]['id']}"
    assert out[0]["_sm_thumb"] == sm                             # rides along, not the original
    plain = tmp_path / "p.aiff"
    shutil.copy(f, plain)
    from mutagen.aiff import AIFF
    a = AIFF(str(plain))
    a.tags.delall("APIC")
    a.save()
    assert scanner._extract_one(plain)[2] is None


def test_a_broken_cover_makes_no_thumbnail():
    from soniqboom.core import scanner
    assert scanner._scan_thumb(None) is None
    assert scanner._scan_thumb("data:image/jpeg;base64,!!!") is None
    assert scanner._scan_thumb("no-comma") is None


# ── per-share backfill width (I) ─────────────────────────────────────────────

async def test_embedded_covers_are_recovered_newest_first_at_each_shares_width(monkeypatch):
    """Fix 4 (2.1.2): a fast scroll asks hundreds of covers; the rows on screen
    are the ones asked last."""
    from soniqboom.core import art_backfill as AB
    AB._pending.clear(); AB._running.clear(); AB._inflight.clear(); AB._neg.clear()
    widths = {"ftp://a.invalid/M": 1, "ftp://b.invalid/M": 2}
    monkeypatch.setattr(AB, "_width", lambda root: widths.get(root, 4))
    gate, order = threading.Event(), []

    def fetch(path, size, ext):
        order.append(path.rsplit("/", 1)[1])
        gate.wait(5)
        return None, None
    monkeypatch.setattr(AB, "_fetch_remote_cover", fetch)
    mk = lambda tid, root: SimpleNamespace(id=tid, path=f"{root}:/x/{tid}.m4a", cover_art=f"/api/art/{tid}", file_size=1)
    a = "ftp://a.invalid/M"
    for tid in ("a1", "a2", "a3", "a4"):
        AB.request_backfill(mk(tid, a))
    await asyncio.sleep(0.05)
    assert order == ["a1.m4a"] and len(AB._running) == 1      # width 1: one at a time
    AB.request_backfill(mk("a2", a))                            # asked again: on screen now
    for tid in ("b1", "b2", "b3"):
        AB.request_backfill(mk(tid, "ftp://b.invalid/M"))
    await asyncio.sleep(0.05)
    assert order[1:] == ["b1.m4a", "b2.m4a"]                   # room for two: started at once
    gate.set()
    for _ in range(100):
        if not AB._pending and not AB._running:
            break
        await asyncio.sleep(0.02)
    # share A (width 1): a2 asked again → first, then the newest a4, then a3
    assert [n for n in order if n.startswith("a")] == ["a1.m4a", "a2.m4a", "a4.m4a", "a3.m4a"]
    assert not AB._inflight and not AB._running


async def test_the_backfill_queue_is_bounded_and_drops_the_oldest(monkeypatch):
    from soniqboom.core import art_backfill as AB
    AB._pending.clear(); AB._running.clear(); AB._inflight.clear(); AB._neg.clear()
    monkeypatch.setattr(AB, "_MAX_PENDING", 3)
    monkeypatch.setattr(AB, "_TOTAL_MAX", 0)                 # (nothing starts: only the queue)
    for i in range(5):
        AB.request_backfill(SimpleNamespace(id=f"t{i}", path=f"ftp://h.invalid/M:/t{i}.m4a",
                                            cover_art="/api/art/x", file_size=1))
    assert list(AB._pending) == ["t2", "t3", "t4"] and AB._inflight == {"t2", "t3", "t4"}
    AB._pending.clear(); AB._inflight.clear()


# ── end to end: a real FTP share's warm-up, a real local scan ───────────────

async def test_the_remote_warm_up_records_every_folder(tmp_path, monkeypatch):
    """The scan's folder-cover warm-up against a real FTP server: the image
    folder is cached and recorded, the image-less one recorded as such — so
    a cover request lists neither again."""
    from soniqboom.config import settings
    from soniqboom.core import scanner
    from soniqboom.core.data import path_hash
    from soniqboom.core.filesource import FTPFileSource
    from _ftp_server import FtpServer
    monkeypatch.setattr(settings, "art_cache_dir", str(tmp_path / "art"))
    fai._reset_for_tests()
    root = tmp_path / "share"
    (root / "A").mkdir(parents=True)
    (root / "B").mkdir()
    (root / "A" / "Folder.jpg").write_bytes(_jpeg(64))
    for d in ("A", "B"):
        (root / d / "01.mp3").write_bytes(b"\0" * 64)
    srv = FtpServer(root).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/")
        share_root = f"ftp://127.0.0.1:{srv.port}/"
        entries = [SimpleNamespace(path="/A/01.mp3"), SimpleNamespace(path="/B/01.mp3")]
        stats = await scanner._prefetch_folder_art_remote(share_root, src, entries)
        assert stats["warmed"] == 1 and stats["no_art"] == 1, stats
        a = fai.lookup(fai.key_for_remote_dir(share_root, "/A"))
        b = fai.lookup(fai.key_for_remote_dir(share_root, "/B"))
        assert a.name == "Folder.jpg" and a.dir_hash == path_hash("/A")
        assert b.name is None
        assert src._sb_link.fast is True                   # measured on the way (loopback)
        assert src._pool._interactive_reserve == 1         # measured (fast): the reserve is on
        listed, retrs = list(srv.listed), list(srv.retrs)
        stats = await scanner._prefetch_folder_art_remote(share_root, src, entries)
        assert stats["skipped_cached"] == 1                # A from the cache
        assert stats["no_art"] == 1                        # B known to hold none
        assert srv.listed == listed and srv.retrs == retrs  # nothing listed or fetched again
    finally:
        srv.stop()
        fai._reset_for_tests()


async def test_the_walk_records_share_folders_so_the_warm_up_lists_none(tmp_path, monkeypatch):
    """The remote scan's walk lists every folder; it records each folder's
    image from that listing, and the warm-up downloads known images by name
    without listing a folder again."""
    from soniqboom.config import settings
    from soniqboom.core import scanner
    from soniqboom.core.filesource import FTPFileSource
    from _ftp_server import FtpServer
    monkeypatch.setattr(settings, "art_cache_dir", str(tmp_path / "art"))
    fai._reset_for_tests()
    root = tmp_path / "share"
    (root / "A").mkdir(parents=True)
    (root / "B").mkdir()
    (root / "A" / "cover.jpg").write_bytes(_jpeg(64))
    for d in ("A", "B"):
        (root / d / "01.mp3").write_bytes(b"\0" * 64)
    srv = FtpServer(root).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/")
        share_root = f"ftp://127.0.0.1:{srv.port}/"
        entries, _p, done, _f = scanner._find_remote_audio_entries(share_root, src, commit_plan=False)
        assert done and {e.path for e in entries} == {"/A/01.mp3", "/B/01.mp3"}
        assert fai.lookup(fai.key_for_remote_dir(share_root, "/A")).name == "cover.jpg"
        assert fai.lookup(fai.key_for_remote_dir(share_root, "/B")).name is None
        walked = list(srv.listed)
        stats = await scanner._prefetch_folder_art_remote(share_root, src, entries)
        assert stats["warmed"] == 1 and stats["no_art"] == 1, stats
        assert srv.listed == walked                        # the warm-up listed nothing
        assert [p for p, _o in srv.retrs] == ["/A/cover.jpg"]
    finally:
        srv.stop()
        fai._reset_for_tests()


@pytest.fixture
def local_library(tmp_path, monkeypatch, tmp_data_dir):
    from soniqboom.config import settings
    from soniqboom.core import scanner
    from soniqboom.core import store as store_mod
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())

    async def _no_dup(*_a, **_k):
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _no_dup)
    monkeypatch.setattr(settings, "art_cache_dir", str(tmp_path / "art"))
    fai._reset_for_tests()
    root = tmp_path / "lib"
    for d in ("Tagged", "Folder", "Bare"):
        (root / d).mkdir(parents=True)
    _aiff_with_cover(root / "Tagged" / "t.aiff", _jpeg(700, (10, 200, 10)))
    _aiff_with_cover(root / "Folder" / "f.aiff", _jpeg(10))
    from mutagen.aiff import AIFF
    a = AIFF(str(root / "Folder" / "f.aiff"))
    a.tags.delall("APIC")
    a.save()
    (root / "Folder" / "folder.jpg").write_bytes(_jpeg(80))
    shutil.copy(root / "Folder" / "f.aiff", root / "Bare" / "b.aiff")
    store_mod._store._lib_root = root.resolve()          # (as test_watcher_scoped_scan's fixture)
    yield root.resolve(), store_mod._store, tmp_path / "art"
    fai._reset_for_tests()


async def test_a_local_scan_stores_list_thumbnails_and_records_folders(local_library):
    from PIL import Image
    from soniqboom.core import art_cache, scanner
    root, store, art_dir = local_library
    assert await scanner._run_scan([str(root)]) is True
    by_name = {Path(t["path"]).name: t for t in store._tracks.values()}
    tagged = by_name["t.aiff"]["id"]
    sm = art_cache.art_exists(tagged, "sm") and (await art_cache.get_art(tagged, "sm"))
    assert sm and max(Image.open(io.BytesIO(sm)).size) <= 200      # ready before any view
    assert not art_cache.art_exists(tagged, "full")                  # only the small one
    assert not art_cache.art_exists(by_name["f.aiff"]["id"], "sm")   # no embedded cover
    assert fai.lookup(fai.key_for_local_dir(str(root / "Folder"))).name == "folder.jpg"
    assert fai.lookup(fai.key_for_local_dir(str(root / "Bare"))).name is None
    assert fai.lookup(fai.key_for_local_dir(str(root / "Tagged"))).name is None


async def test_a_retagged_local_file_loses_its_old_cached_cover(local_library):
    from soniqboom.core import art_cache, scanner
    root, store, _art = local_library
    assert await scanner._run_scan([str(root)]) is True
    tid = next(t["id"] for t in store._tracks.values() if t["path"].endswith("t.aiff"))
    await art_cache.store_art(tid, b"old-lg", "lg")                  # cached by an earlier play
    await art_cache.store_art(tid, b"old-full", "full")
    f = root / "Tagged" / "t.aiff"
    from mutagen.aiff import AIFF
    from mutagen.id3 import APIC
    a = AIFF(str(f))
    a.tags.delall("APIC")
    a.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=_jpeg(300, (0, 0, 250))))
    a.save()
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 10))
    await scanner._run_scan([str(root)])          # (False: only the picture changed — no library change)
    assert not art_cache.art_exists(tid, "lg") and not art_cache.art_exists(tid, "full")
    from PIL import Image
    sm = await art_cache.get_art(tid, "sm")
    px = Image.open(io.BytesIO(sm)).convert("RGB").getpixel((50, 50))
    assert px[2] > 200 and px[0] < 60                                 # the new (blue) cover


def test_a_folder_that_changes_its_image_is_no_gain(index):
    k = fai.key_for_local_dir("/M/C")
    fai.record(k, "folder.jpg", dir_hash="h3")
    fai.record(k, "cover.jpg", dir_hash="h3")
    assert fai.take_flips() == []                   # it had one before: rows were never told "none"


class _WalkSrc:
    """A share whose ``/B`` listing fails (the walk then yields it empty and
    reports it to ``error_sink``, as the FTP walk does)."""

    def walk_with_stat(self, top, *, error_sink=None, skip_subtree_fn=None):
        from soniqboom.core.filesource import DirEntry
        a = DirEntry(name="A", path="/A", is_dir=True, size=0, mtime=1.0)
        b = DirEntry(name="B", path="/B", is_dir=True, size=0, mtime=1.0)
        yield "/", [a, b], []
        yield "/A", [], [DirEntry(name="01.mp3", path="/A/01.mp3", is_dir=False, size=9, mtime=1.0)]
        error_sink.append(("/B", OSError("550 timeout")))
        yield "/B", [], []


def test_a_folder_whose_listing_failed_is_not_recorded(index):
    from soniqboom.core import scanner
    scanner._find_remote_audio_entries("ftp://h.invalid/M", _WalkSrc(), commit_plan=False)
    assert fai.lookup(fai.key_for_remote_dir("ftp://h.invalid/M", "/A")).name is None
    assert fai.lookup(fai.key_for_remote_dir("ftp://h.invalid/M", "/B")) is None   # unknown, never "none"


async def test_the_warm_up_reads_several_folders_at_once_on_a_slow_link(index, monkeypatch):
    from soniqboom.core import scanner
    monkeypatch.setattr(ls, "measure", lambda src: False)
    monkeypatch.setattr(ls, "limits", lambda src: ls.SLOW)

    async def _no_store(*_a, **_k):
        return None
    monkeypatch.setattr(A.art_cache, "store_art", _no_store)

    async def _no_cached(*_a, **_k):
        return None
    monkeypatch.setattr(A.art_cache, "get_art", _no_cached)
    now, peak, lock = [0], [0], threading.Lock()

    class _Slow:
        def list_dir(self, d):
            with lock:
                now[0] += 1
                peak[0] = max(peak[0], now[0])
            time.sleep(0.05)
            with lock:
                now[0] -= 1
            return []
    entries = [SimpleNamespace(path=f"/f{i}/t.mp3") for i in range(8)]
    stats = await scanner._prefetch_folder_art_remote("ftp://h.invalid/M", _Slow(), entries)
    assert stats["no_art"] == 8
    assert peak[0] == ls.SLOW.warm_width            # 4 folders at once, not 1


# ── review fixes (2026-11-22) ────────────────────────────────────────────────

async def test_status_never_says_none_for_a_format_the_scan_cannot_see_covers_in(status, monkeypatch):
    monkeypatch.setattr(A.online_cover, "lookups_off", lambda: True)
    monkeypatch.setattr(A, "_verify_later", lambda t: None)
    fai.record(fai.key_for_local_dir("/M/none"), None)
    for tid, name in (("ogg", "a.ogg"), ("opus", "b.OPUS"), ("mp3", "c.mp3")):
        status.rows[tid] = {"id": tid, "path": f"/M/none/{name}", "dir_hash": "a"}
    out = await A.art_status(_JsonReq({"ids": ["ogg", "opus", "mp3"]}))
    assert out["none"] == ["mp3"]                   # Ogg / Opus pictures are not seen by the scan


async def test_status_has_a_local_none_folder_checked_live(status, monkeypatch):
    monkeypatch.setattr(A.online_cover, "lookups_off", lambda: True)
    checked = []
    monkeypatch.setattr(A, "_verify_later", lambda t: checked.append(t.id))
    fai.record(fai.key_for_local_dir("/M/none"), None)
    status.rows["l1"] = {"id": "l1", "path": "/M/none/a.mod", "dir_hash": "a"}
    fai.record(fai.key_for_remote_dir("ftp://h.invalid/M", "/A"), None)
    status.rows["r1"] = {"id": "r1", "path": "ftp://h.invalid/M:/A/a.mod", "dir_hash": "b"}
    out = await A.art_status(_JsonReq({"ids": ["l1", "r1"]}))
    assert sorted(out["none"]) == ["l1", "r1"] and checked == ["l1"]


async def test_an_announced_folder_clears_the_stored_marks_too(index, monkeypatch):
    sent, cleared = [], []

    async def _broadcast(m):
        sent.append(m)
    import soniqboom.api.library as LIB
    monkeypatch.setattr(LIB, "_broadcast", _broadcast)
    monkeypatch.setattr(A, "_clear_art_absent_persisted", cleared.append)
    monkeypatch.setattr(A, "get_store", lambda: SimpleNamespace(
        filter_tracks=lambda dir_hash, limit: [{"id": "t1"}], discard_art_absent=lambda tid: None))
    k = fai.key_for_local_dir("/M/A")
    fai.record(k, None, dir_hash="h1")
    fai.record(k, "folder.jpg", dir_hash="h1")
    await A.announce_folder_changes()
    assert cleared == ["t1"] and sent


def test_the_live_check_finds_every_changed_marked_track_of_the_folder(index, tmp_path, monkeypatch):
    d = tmp_path / "A"
    d.mkdir()
    files = {}
    for n in ("a", "b", "c"):
        f = d / f"{n}.mp3"
        f.write_bytes(b"x")
        files[n] = f
    os.utime(files["a"], (1000.0, 1000.0))          # unchanged since marked (mark at 2000)
    os.utime(files["b"], (3000.0, 3000.0))          # re-tagged after the mark
    os.utime(files["c"], (3000.0, 3000.0))          # changed, but has no mark (a cached cover)
    marked = {"a", "b"}
    monkeypatch.setattr(A, "get_store", lambda: SimpleNamespace(is_art_absent=lambda tid: tid in marked))
    monkeypatch.setattr(A, "_is_art_absent_persisted", lambda tid, live: live <= 2000.0)
    rows = [{"id": n, "path": str(f)} for n, f in files.items()]
    assert A._changed_since_marked(rows) == ["b"]


async def test_a_caller_without_art_ready_waits_for_the_folder_job(share, monkeypatch):
    cache = {}

    async def _get(key, size):
        return cache.get((key, size))

    async def _put(key, data, size):
        cache[(key, size)] = data
    monkeypatch.setattr(A.art_cache, "get_art", _get)
    monkeypatch.setattr(A.art_cache, "store_art", _put)
    monkeypatch.setattr(A, "_is_art_absent_persisted", lambda *a: False)
    monkeypatch.setattr(A, "get_store", lambda: SimpleNamespace(
        is_art_absent=lambda tid: False, discard_art_absent=lambda tid: None,
        get_config=lambda k, d=None: d))
    gate = threading.Event()
    share.make({"/A": {"folder.jpg": b"\xff\xd8folder"}}, gate=gate)
    t = _tr("r1", "/A/01.mp3")

    async def _get_track(tid):
        return t
    monkeypatch.setattr(A, "get_track", _get_track)
    threading.Timer(0.2, gate.set).start()
    data, _mime = await A._resolve_full_art("r1", wait_folder=3.0)
    assert data == b"\xff\xd8folder"                    # Subsonic / DLNA / cast get it now
    gate.clear()
    fc._reset_for_tests()
    t2 = _tr("r2", "/B/01.mp3", dir_hash="dh-b")
    share.src.folders["/B"] = {"folder.jpg": b"\xff\xd8b"}

    async def _get_track2(tid):
        return t2
    monkeypatch.setattr(A, "get_track", _get_track2)
    t0 = time.monotonic()
    data, _m = await A._resolve_full_art("r2", wait_folder=0.2)   # the share too slow: give up
    assert data is None and time.monotonic() - t0 < 1.5
    gate.set()
    await _drain()


def test_a_failed_probe_keeps_what_was_known():
    src = _Probed(0.064)
    assert ls.measure(src) is False
    src.rtt = OSError("pool busy")
    assert ls.measure(src) is False                 # still slow, reserve kept
    assert ls.limits(src) == ls.SLOW and src._pool.reserve == 1
    src.rtt = 0.002
    src.calls = 0
    src._sb_link = src._sb_link._replace(at=time.monotonic())
    assert ls.ensure_measured(src) is False and src.calls == 0     # fresh: no probe


def test_the_reserve_stays_free_beyond_playback():
    pool = _pool(5)
    pool.set_interactive_reserve(1)
    with pool._cond:
        pool._in_use_stream = 1
        pool._in_use_scan = 3
        assert pool._scan_at_reserve() is True      # 3 scan + 1 play: the 5th stays free
        pool._in_use_scan = 2
        assert pool._scan_at_reserve() is False
        pool._in_use_stream = 3
        assert pool._scan_at_reserve() is True      # (never below two for the scan)
        pool._in_use_scan = 1
        assert pool._scan_at_reserve() is False


async def test_a_folder_job_falls_back_to_the_next_image_name(share, monkeypatch):
    src = share.make({"/A": {"folder.jpg": b"", "cover.jpg": b"\xff\xd8c"}})
    monkeypatch.setattr(fai, "folder_image_names", lambda: ["folder.jpg", "cover.jpg"])
    fc.request(_tr("t1", "/A/01.mp3"))
    await _drain()
    assert share.persisted == [("t1", b"\xff\xd8c")]
    assert fai.lookup(fai.key_for_remote_dir(SHARE, "/A")).name == "cover.jpg"


async def test_a_busy_pool_is_not_an_unreachable_share(share, monkeypatch):
    src = share.make({"/A": {"folder.jpg": b"\xff\xd8"}})

    def busy(_d):
        raise TimeoutError("FTP pool 'x' acquire timed out after 60.0s (max=9, lane=browse)")
    src.list_dir = busy
    t = _tr("t1", "/A/01.mp3")
    fc.request(t)
    await _drain()
    assert not fc.unreachable(t) and share.online == []      # no online cover stands in
    assert fc.request(t) is False                             # but not asked again at once


async def test_a_setting_changed_during_a_folder_job_stores_nothing(share, monkeypatch):
    gate = threading.Event()
    share.make({"/A": {"folder.jpg": b"\xff\xd8"}}, gate=gate)
    fc.request(_tr("t1", "/A/01.mp3"))
    await asyncio.sleep(0.05)
    fai.forget_all()                                          # "Use folder art" / names / cache cleared
    gate.set()
    await _drain()
    assert share.persisted == [] and share.stored == []


async def test_replaced_thumbnails_go_but_the_full_cover_stays(tmp_path, monkeypatch):
    from soniqboom.core import art_cache
    monkeypatch.setattr(art_cache, "get_art_cache_dir", lambda: tmp_path)
    for size in ("sm", "lg", "full"):
        await art_cache.store_art("t1", b"x", size)
    await art_cache.delete_thumbs_batch(["t1", "missing"])
    assert not art_cache.art_exists("t1", "sm") and not art_cache.art_exists("t1", "lg")
    assert art_cache.art_exists("t1", "full")


def test_the_index_drops_the_longest_unconfirmed_folders_past_its_bound(index, monkeypatch):
    monkeypatch.setattr(fai, "_MAX_ENTRIES", 3)
    for i in range(5):
        fai.record(fai.key_for_local_dir(f"/M/{i}"), None)
        time.sleep(0.002)
    fai.flush()
    fai._reset_for_tests()
    assert fai.lookup(fai.key_for_local_dir("/M/0")) is None
    assert fai.lookup(fai.key_for_local_dir("/M/4")) is not None


async def test_clearing_the_art_cache_tells_open_pages(monkeypatch, tmp_path):
    from soniqboom.api import admin
    sent = []

    async def _broadcast(m):
        sent.append(m)
    import soniqboom.api.library as LIB
    monkeypatch.setattr(LIB, "_broadcast", _broadcast)
    monkeypatch.setattr(admin, "_purge_art_dir", lambda **k: (0, []))
    monkeypatch.setattr(admin, "get_store", lambda: SimpleNamespace(clear_art_absent=lambda: None))
    await admin.clear_art_cache(_tok="t")
    assert {"event": "art_refresh"} in sent


# ── next improvements (2026-11-22): 1 stale folder copies, 2 share-root keys,
#    3 draft resize, 5 Ogg / Opus pictures ──────────────────────────────────

def test_a_replaced_or_removed_folder_image_is_a_change(index):
    k = fai.key_for_local_dir("/M/A")
    fai.record(k, "folder.jpg", mtime=100.0, dir_hash="h1")
    fai.record(k, "folder.jpg", mtime=None, dir_hash="h1")       # a writer without the date
    assert fai.take_changed() == [] and fai.lookup(k).mtime == 100.0
    fai.record(k, "folder.jpg", mtime=100.4, dir_hash="h1")      # (same second)
    assert fai.take_changed() == []
    fai.record(k, "folder.jpg", mtime=200.0, dir_hash="h1")      # replaced in place
    assert fai.take_changed() == [(k, "h1")]
    fai.record(k, "cover.jpg", mtime=200.0, dir_hash="h1")       # another name
    assert fai.take_changed() == [(k, "h1")]
    fai.record(k, None, dir_hash="h1")                           # removed
    assert fai.take_changed() == [(k, "h1")]
    fai.record(k, "folder.jpg", mtime=1.0, dir_hash="h1")        # none → image: a flip, no change
    assert fai.take_changed() == [] and fai.take_flips() == ["h1"]


async def test_copies_of_a_changed_folder_image_go_and_own_covers_stay(index, monkeypatch):
    from soniqboom.core import art_cache
    monkeypatch.setattr(art_cache, "get_art_cache_dir", lambda: index / "art")
    k = fai.key_for_local_dir("/M/A")
    old = b"\xff\xd8old-folder"
    await art_cache.store_art(A._folder_art_cache_key("h1"), old, "full")
    for tid, data in (("marked", b"\xff\xd8copy"), ("legacy", old), ("own", b"\xff\xd8embedded")):
        await art_cache.store_art(tid, data, "full")
        await art_cache.store_art(tid, b"thumb", "sm")
    art_cache.mark_from_folder("marked")
    rows = [{"id": t, "path": f"/M/A/{t}.mp3"} for t in ("marked", "legacy", "own")]
    rows.append({"id": "elsewhere", "path": "/M/B/x.mp3"})        # (a dir_hash collision: left alone)
    await art_cache.store_art("elsewhere", old, "full")
    fields, sent = {}, []
    monkeypatch.setattr(A, "get_store", lambda: SimpleNamespace(
        filter_tracks=lambda dir_hash, limit: rows if dir_hash == "h1" else [],
        update_track_fields=lambda tid, f: fields.setdefault(tid, f),
        discard_art_absent=lambda tid: None))
    monkeypatch.setattr(A, "_clear_art_absent_persisted", lambda tid: None)

    async def _broadcast(m):
        sent.append(m["track_id"])
    import soniqboom.api.library as LIB
    monkeypatch.setattr(LIB, "_broadcast", _broadcast)
    fai.record(k, "folder.jpg", mtime=100.0, dir_hash="h1")
    fai.record(k, "folder.jpg", mtime=500.0, dir_hash="h1")       # replaced
    assert await A.refresh_changed_folders() == 2
    assert sorted(sent) == ["legacy", "marked"]
    for tid in ("marked", "legacy"):
        assert not art_cache.art_exists(tid, "full") and not art_cache.art_exists(tid, "sm")
        assert not art_cache.is_from_folder(tid)
    assert fields == {}                                 # ``cover_art`` (what the file carries) untouched
    assert art_cache.art_exists("own", "full") and art_cache.art_exists("elsewhere", "full")
    assert not art_cache.art_exists(A._folder_art_cache_key("h1"), "full")


async def test_a_cover_cached_from_the_folder_is_marked_and_an_own_one_is_not(index, monkeypatch):
    from soniqboom.core import art_cache
    monkeypatch.setattr(art_cache, "get_art_cache_dir", lambda: index / "art")
    monkeypatch.setattr(A, "_update_track_cover_ref", lambda tid: asyncio.sleep(0))
    import soniqboom.api.library as LIB

    async def _nothing(m):
        return None
    monkeypatch.setattr(LIB, "_broadcast", _nothing)
    A._persist_and_notify("t1", _jpeg(40), from_folder=True)
    for _ in range(50):
        await asyncio.sleep(0.01)
    assert art_cache.is_from_folder("t1")
    A._persist_and_notify("t1", _jpeg(40, (0, 0, 200)))           # its own picture, found later
    for _ in range(50):
        await asyncio.sleep(0.01)
    assert not art_cache.is_from_folder("t1")


def test_share_folders_are_cached_per_share(index):
    a = A._folder_cache_key("ftp://one.invalid/M:/Music/01.mp3", "same-hash")
    b = A._folder_cache_key("ftp://two.invalid/M:/Music/01.mp3", "same-hash")
    assert a != b and a.startswith("folder:R")
    assert A._folder_cache_key("/local/Music/01.mp3", "h9") == A._folder_art_cache_key("h9")
    assert A._folder_cache_key("/local/Music/01.mp3", None) is None


def test_the_draft_resize_keeps_size_and_picture():
    from PIL import Image
    from soniqboom.core.metadata import resize_cover
    src = _jpeg(3000, (30, 140, 220))
    out = resize_cover(src, 200)
    img = Image.open(io.BytesIO(out)).convert("RGB")
    assert img.size == (200, 200)
    r, g, b = img.getpixel((100, 100))
    assert abs(r - 30) < 12 and abs(g - 140) < 12 and abs(b - 220) < 12


def _picture_block(data: bytes, ptype: int = 3, mime: str = "image/jpeg") -> str:
    import base64
    from mutagen.flac import Picture
    p = Picture()
    p.type, p.mime, p.data = ptype, mime, data
    return base64.b64encode(p.write()).decode()


def test_ogg_and_opus_pictures_are_read_front_cover_first():
    import base64
    from soniqboom.core.metadata import vorbis_picture
    tags = {"metadata_block_picture": ["!!not base64", _picture_block(b"back", 4), _picture_block(b"front", 3)]}
    assert vorbis_picture(tags) == (b"front", "image/jpeg")
    assert vorbis_picture({"metadata_block_picture": [_picture_block(b"only", 0, "image/png")]}) == (b"only", "image/png")
    old = {"coverart": [base64.b64encode(b"legacy").decode()], "coverartmime": ["image/png"]}
    assert vorbis_picture(old) == (b"legacy", "image/png")
    assert vorbis_picture({}) == (None, None) and vorbis_picture(None) == (None, None)


@pytest.mark.parametrize("ext,codec,reader", [(".opus", "libopus", "OggOpus"),
                                               (".ogg", "libvorbis", "OggVorbis")])
def test_the_scan_records_an_ogg_picture(tmp_path, ext, codec, reader):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    f = tmp_path / f"t{ext}"
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1",
                        "-c:a", codec, str(f)], capture_output=True)
    if r.returncode:
        pytest.skip(f"this ffmpeg can't write {codec}")
    import mutagen.oggopus, mutagen.oggvorbis
    from soniqboom.core import scanner
    a = getattr(mutagen.oggopus if reader == "OggOpus" else mutagen.oggvorbis, reader)(str(f))
    a["metadata_block_picture"] = [_picture_block(_jpeg(300))]
    a.save()
    _p, meta, sm, _l = scanner._extract_one(f)
    assert not isinstance(meta, str) and meta.cover_art and sm      # recorded, and its list thumbnail
    data, _m = A._extract_cover(f)
    assert data == _jpeg(300)


async def test_an_ogg_row_is_none_only_once_its_file_was_read_without_a_cover(status, monkeypatch):
    monkeypatch.setattr(A.online_cover, "lookups_off", lambda: True)
    monkeypatch.setattr(A, "_verify_later", lambda t: None)
    fai.record(fai.key_for_local_dir("/M/none"), None)
    status.rows["o1"] = {"id": "o1", "path": "/M/none/a.ogg", "dir_hash": "a"}
    marked: set = set()
    store = A.get_store()
    monkeypatch.setattr(A, "get_store", lambda: SimpleNamespace(
        get_track=store.get_track, get_config=store.get_config, is_art_absent=lambda tid: tid in marked))
    monkeypatch.setattr(A, "_absent_sentinel_path", lambda tid: Path("/nonexistent/x"))
    assert (await A.art_status(_JsonReq({"ids": ["o1"]})))["none"] == []    # (an old scan: unknown)
    marked.add("o1")                                                         # read: no picture
    assert (await A.art_status(_JsonReq({"ids": ["o1"]})))["none"] == ["o1"]


async def test_a_replaced_folder_image_reaches_its_tracks_after_a_scan(local_library):
    """End to end on a real local scan: a track shows its folder's image (a
    cached copy); the image is replaced; the next scan's walk notices, the
    copy is dropped, and the track resolves the new image."""
    from PIL import Image
    from soniqboom.core import art_cache, scanner
    root, store, _art = local_library
    assert await scanner._run_scan([str(root)]) is True
    tid = next(t["id"] for t in store._tracks.values() if t["path"].endswith("folder0.aiff")
               or t["path"].endswith("f.aiff"))
    data, _m = await A._resolve_full_art(tid)
    for _ in range(100):
        if art_cache.art_exists(tid, "full") and art_cache.is_from_folder(tid):
            break
        await asyncio.sleep(0.02)
    assert art_cache.is_from_folder(tid)
    g = Image.open(io.BytesIO(data)).convert("RGB").getpixel((5, 5))
    img = root / "Folder" / "folder.jpg"
    img.write_bytes(_jpeg(80, (200, 20, 200)))                       # replaced: magenta
    st = img.stat()
    os.utime(img, (st.st_atime, st.st_mtime + 30))
    await scanner._run_scan([str(root)])
    for _ in range(100):
        if not art_cache.art_exists(tid, "full"):
            break
        await asyncio.sleep(0.02)
    assert not art_cache.art_exists(tid, "full")                      # the stale copy went
    data2, _m = await A._resolve_full_art(tid)
    px = Image.open(io.BytesIO(data2)).convert("RGB").getpixel((5, 5))
    assert px != g and px[0] > 150 and px[2] > 150 and px[1] < 80     # the new image


class _DatedWalk:
    """A share walk whose folder ``/A`` holds ``folder.jpg`` with a date."""

    def __init__(self, mtime):
        self.mtime = mtime

    def walk_with_stat(self, top, *, error_sink=None, skip_subtree_fn=None):
        from soniqboom.core.filesource import DirEntry
        yield "/", [DirEntry(name="A", path="/A", is_dir=True, size=0, mtime=1.0)], []
        yield "/A", [], [DirEntry(name="01.mp3", path="/A/01.mp3", is_dir=False, size=9, mtime=1.0),
                         DirEntry(name="folder.jpg", path="/A/folder.jpg", is_dir=False, size=9,
                                  mtime=self.mtime)]


def test_a_share_walk_notices_a_replaced_folder_image(index):
    from soniqboom.core import scanner
    root = "ftp://h.invalid/M"
    scanner._find_remote_audio_entries(root, _DatedWalk(1000.0), commit_plan=False)
    k = fai.key_for_remote_dir(root, "/A")
    assert fai.lookup(k).mtime == 1000.0
    assert fai.take_changed() == []
    scanner._find_remote_audio_entries(root, _DatedWalk(2000.0), commit_plan=False)
    assert [c[0] for c in fai.take_changed()] == [k]


async def test_a_folder_job_that_finds_a_new_image_refreshes_the_old_copies(share, monkeypatch):
    src = share.make({"/A": {"folder.jpg": b"\xff\xd8new"}})
    real_list = src.list_dir

    def dated(d):
        return [SimpleNamespace(**{**e.__dict__, "mtime": 5000.0}) for e in real_list(d)]
    src.list_dir = dated
    k = fai.key_for_remote_dir(SHARE, "/A")
    fai.record(k, "folder.jpg", mtime=1000.0, dir_hash="dh")      # what the last scan saw
    old_shared = b"\xff\xd8old-shared"
    share.stored.clear()
    cache = {(f"folder:{k}", "full"): old_shared}

    async def _get(key, size):
        return cache.get((key, size))
    monkeypatch.setattr(A.art_cache, "get_art", _get)
    deleted = []

    async def _del(ids):
        deleted.extend(ids)
        return len(ids), 0
    monkeypatch.setattr(A.art_cache, "delete_art_batch", _del)
    refreshed = []

    async def _refresh(key, dir_hash, old=None):
        refreshed.append((key, dir_hash, old))
        return 0
    monkeypatch.setattr(A, "refresh_folder", _refresh)
    fc.request(_tr("t1", "/A/01.mp3"))
    await _drain()
    assert fai.lookup(k).mtime == 5000.0                              # the job's listing date
    assert refreshed == [(k, "dh", old_shared)]                       # with the OLD image in hand
    assert f"folder:{k}" in deleted
    assert share.stored == [(f"folder:{k}", "full")]                  # then the new one stored
    assert fai.take_changed() == []                                   # (taken by the job)


async def test_a_recovered_embedded_cover_is_no_folder_copy(monkeypatch, tmp_path):
    from soniqboom.core import art_backfill as AB, art_cache
    monkeypatch.setattr(art_cache, "get_art_cache_dir", lambda: tmp_path)
    AB._pending.clear(); AB._running.clear(); AB._inflight.clear(); AB._neg.clear()
    art_cache.mark_from_folder("e1")                                  # it showed the folder's image
    monkeypatch.setattr(AB, "_fetch_remote_cover", lambda p, s, e: (_jpeg(50), "image/jpeg"))
    monkeypatch.setattr(AB, "_width", lambda root: 4)
    import soniqboom.api.library as LIB

    async def _nothing(m):
        return None
    monkeypatch.setattr(LIB, "_broadcast", _nothing)
    AB.request_backfill(SimpleNamespace(id="e1", path="ftp://h.invalid/M:/a.m4a",
                                        cover_art="/api/art/e1", file_size=1))
    for _ in range(100):
        if not AB._running and not AB._pending:
            break
        await asyncio.sleep(0.02)
    assert art_cache.art_exists("e1", "full") and not art_cache.is_from_folder("e1")



# ── review fixes of the next improvements ────────────────────────────────────

def test_ftp_list_and_timezone_dates_are_no_change():
    st = fai._same_time
    assert st(1700000037.0, 1700000020.0) is False or st(1700000040.0, 1700000040.0)
    assert st(1700000040.0, 1700000040.4)                    # (sub-second)
    assert st(1699999980.0, 1700000037.0)                    # LIST minute vs MLSD seconds
    assert st(1700000037.0, 1700000037.0 + 3600)             # server-local LIST vs UTC
    assert st(1700000037.0, 1700000037.0 - 7 * 3600)
    assert not st(1700000037.0, 1700000067.0)                # 30 s apart: replaced
    assert not st(1700000037.0, 1700000037.0 + 1800)         # half an hour: replaced
    assert not st(1700000037.0, 1700000037.0 + 20 * 3600)


def test_a_pending_change_survives_a_restart(index):
    k = fai.key_for_local_dir("/M/A")
    fai.record(k, "folder.jpg", mtime=100.0, dir_hash="h1")
    fai.record(k, "folder.jpg", mtime=900.0, dir_hash="h1")
    fai.flush()
    fai._reset_for_tests()
    assert fai.take_changed() == [(k, "h1")]
    fai.requeue([(k, "h1")])                                 # (a refresh that failed)
    assert fai.take_changed() == [(k, "h1")]


def test_a_change_the_queue_cannot_take_leaves_the_entry_alone(index, monkeypatch):
    monkeypatch.setattr(fai, "_MAX_FLIPS", 0)
    k = fai.key_for_local_dir("/M/A")
    fai.record(k, "folder.jpg", mtime=100.0, dir_hash="h1")
    fai.record(k, "cover.jpg", mtime=200.0, dir_hash="h1")
    assert fai.lookup(k).name == "folder.jpg"                # seen again on the next walk


async def test_a_share_track_with_its_own_picture_waits_for_it_not_for_the_folder(endpoint, monkeypatch):
    from soniqboom.core import art_backfill as AB
    AB._pending.clear(); AB._by_root.clear(); AB._running.clear(); AB._running_by_root.clear()
    AB._inflight.clear(); AB._neg.clear()
    src = endpoint.make({"/A": {"folder.jpg": b"\xff\xd8f"}})
    gate = threading.Event()
    monkeypatch.setattr(AB, "_fetch_remote_cover", lambda p, s, e: (gate.wait(5), (None, None))[1])
    monkeypatch.setattr(AB, "_width", lambda root: 4)
    endpoint.tracks["r1"] = _tr("r1", "/A/01.mp3", cover_art="/api/art/r1", file_size=1)
    r = await A.cover_art("r1", _Req(), size="sm", fallback="none")
    assert r.status_code == 204 and r.headers["Cache-Control"] == "no-cache"   # its own is coming
    await _drain()
    assert src.listed == [] and endpoint.persisted == []    # the folder image is NOT put in its place
    AB._neg["r1"] = time.monotonic()                          # (its recovery found no picture)
    gate.set()
    await A.cover_art("r1", _Req(), size="sm", fallback="none")
    await _drain()
    assert src.listed == ["/A"] and endpoint.persisted == [("r1", b"\xff\xd8f")]
    for _ in range(50):
        if not AB._running and not AB._pending:
            break
        await asyncio.sleep(0.02)


async def test_a_folder_copy_does_not_claim_the_file_has_a_picture(index, monkeypatch):
    from soniqboom.core import art_cache
    monkeypatch.setattr(art_cache, "get_art_cache_dir", lambda: index / "art")
    refs = []

    async def _ref(tid):
        refs.append(tid)
    monkeypatch.setattr(A, "_update_track_cover_ref", _ref)
    import soniqboom.api.library as LIB

    async def _nothing(m):
        return None
    monkeypatch.setattr(LIB, "_broadcast", _nothing)
    A._persist_and_notify("t1", _jpeg(40), from_folder=True)
    A._persist_and_notify("t2", _jpeg(40))
    for _ in range(50):
        await asyncio.sleep(0.01)
    assert refs == ["t2"]


def test_more_formats_show_the_picture_the_scan_records(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    f = tmp_path / "t.opus"
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1",
                        "-c:a", "libopus", str(f)], capture_output=True)
    if r.returncode:
        pytest.skip("no libopus")
    from mutagen.oggopus import OggOpus
    a = OggOpus(str(f))
    a["metadata_block_picture"] = [_picture_block(_jpeg(120))]
    a.save()
    oga = tmp_path / "t.oga"
    shutil.copy(f, oga)
    assert A._extract_cover(oga)[0] == _jpeg(120)                 # .oga as .ogg
    w = tmp_path / "t.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1",
                    "-write_id3v2", "1", str(w)], capture_output=True)
    from mutagen.wave import WAVE
    from mutagen.id3 import APIC
    wv = WAVE(str(w))
    if wv.tags is None:
        wv.add_tags()
    wv.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=_jpeg(130)))
    wv.save()
    from soniqboom.core.metadata import extract
    if not getattr(extract(w, "x"), "cover_art", None):
        pytest.skip("this scan reads no WAV picture")
    assert A._extract_cover(w)[0] == _jpeg(130)                   # via the scan's extractor


def test_odd_picture_modes_become_rgb_thumbnails():
    from PIL import Image
    from soniqboom.core.metadata import resize_cover
    for mode in ("LA", "I;16", "CMYK", "P", "RGBA"):     # (PA has no file format to arrive in)
        src = Image.new("RGB", (600, 600), (40, 160, 90)).convert(mode)
        buf = io.BytesIO()
        src.save(buf, "PNG" if mode not in ("CMYK",) else "JPEG")
        out = resize_cover(buf.getvalue(), 200)
        img = Image.open(io.BytesIO(out))
        assert img.format == "JPEG" and img.mode in ("RGB", "L") and max(img.size) == 200, mode


def test_a_picture_block_that_is_a_link_is_no_picture():
    from soniqboom.core.metadata import vorbis_picture
    assert vorbis_picture({"metadata_block_picture": [_picture_block(b"http://x.invalid/a.jpg", 3, "-->")]}) == (None, None)


async def test_four_thousand_queued_recoveries_cost_the_loop_little(monkeypatch):
    from soniqboom.core import art_backfill as AB
    AB._pending.clear(); AB._by_root.clear(); AB._running.clear(); AB._running_by_root.clear()
    AB._inflight.clear(); AB._neg.clear()
    monkeypatch.setattr(AB, "_width", lambda root: 0)        # (a share at its width: nothing starts)
    t0 = time.perf_counter()
    for i in range(4000):
        AB.request_backfill(SimpleNamespace(id=f"q{i}", path=f"ftp://h.invalid/M:/q{i}.m4a",
                                            cover_art="/api/art/x", file_size=1))
    took = time.perf_counter() - t0
    assert len(AB._pending) == 4000 and took < 1.5, took     # (was 22 s: every pump walked them all)
    AB._pending.clear(); AB._by_root.clear(); AB._inflight.clear()


def test_a_changed_folder_forgets_its_kept_listing(share):
    src = share.make({"/A": {"x.mp3": b""}})
    fc._listing(src, SHARE, "/A")
    k = fai.key_for_remote_dir(SHARE, "/A")
    fc._failed_at[k] = time.monotonic()
    fc.forget_folder(k)
    fc._listing(src, SHARE, "/A")
    assert src.listed == ["/A", "/A"] and k not in fc._failed_at


def _fresh_backfill(AB):
    AB._pending.clear(); AB._by_root.clear(); AB._asked_at.clear(); AB._running.clear()
    AB._running_by_root.clear(); AB._inflight.clear(); AB._neg.clear()


async def test_a_file_without_a_picture_has_its_row_asked_again(monkeypatch):
    from soniqboom.core import art_backfill as AB
    _fresh_backfill(AB)
    monkeypatch.setattr(AB, "_fetch_remote_cover", lambda p, s, e: (None, None))
    monkeypatch.setattr(AB, "_width", lambda root: 4)
    sent = []

    async def _broadcast(m):
        sent.append(m)
    import soniqboom.api.library as LIB
    monkeypatch.setattr(LIB, "_broadcast", _broadcast)
    AB.request_backfill(SimpleNamespace(id="n1", path="ftp://h.invalid/M:/a.m4a", cover_art="/api/art/n1", file_size=1))
    for _ in range(100):
        if not AB._running and not AB._pending:
            break
        await asyncio.sleep(0.02)
    assert {"event": "art_ready", "track_id": "n1"} in sent and AB.failed_recently("n1")


async def test_subsonic_and_cast_hear_that_an_own_cover_is_on_its_way(endpoint):
    from soniqboom.core import art_backfill as AB
    _fresh_backfill(AB)
    endpoint.make({})
    endpoint.tracks["r1"] = _tr("r1", "/A/01.mp3", cover_art="/api/art/r1")
    AB._inflight.add("r1")
    try:
        assert await A.online_cover_for("r1") == (None, True)
    finally:
        AB._inflight.discard("r1")


async def test_the_share_asked_last_is_served_first(monkeypatch):
    from soniqboom.core import art_backfill as AB
    _fresh_backfill(AB)
    monkeypatch.setattr(AB, "_TOTAL_MAX", 1)
    monkeypatch.setattr(AB, "_width", lambda root: 1)
    gate, order = threading.Event(), []

    def fetch(path, size, ext):
        order.append(path.rsplit("/", 1)[1])
        gate.wait(5)
        return None, None
    monkeypatch.setattr(AB, "_fetch_remote_cover", fetch)
    import soniqboom.api.library as LIB

    async def _nothing(m):
        return None
    monkeypatch.setattr(LIB, "_broadcast", _nothing)
    mk = lambda tid, host: SimpleNamespace(id=tid, path=f"ftp://{host}.invalid/M:/{tid}.m4a",
                                           cover_art="/api/art/x", file_size=1)
    AB.request_backfill(mk("a1", "a"))            # runs
    await asyncio.sleep(0.05)
    AB.request_backfill(mk("b1", "b"))            # waits
    await asyncio.sleep(0.01)
    AB.request_backfill(mk("a2", "a"))            # waits — share A asked last
    gate.set()
    for _ in range(100):
        if not AB._running and not AB._pending:
            break
        await asyncio.sleep(0.02)
    assert order == ["a1.m4a", "a2.m4a", "b1.m4a"]
