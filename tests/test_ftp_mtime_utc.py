# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FTP listing times are UTC whatever the host's zone, and the times an
earlier build stored as host-local are converted once
(``core.ftp_mtime_migration``) — so an FTP share scanned before the fix is
NOT read again after the upgrade, while a file changed afterwards still is.

Host zones are switched in-process (``TZ`` + ``time.tzset``)."""
from __future__ import annotations

import calendar
import datetime
import io
import json
import os
import random
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402
from test_remote_fast_walk import _mod, fresh  # noqa: E402,F401

try:                                                # (absent before the fix: the
    from soniqboom.core import ftp_mtime_migration as mig  # noqa: E402  parser tests
except ImportError:                                 # pragma: no cover  still run there)
    mig = None
from soniqboom.core import scanner  # noqa: E402
from soniqboom.core.filesource import FTPFileSource  # noqa: E402

ZONES = ["America/Los_Angeles", "Europe/Berlin", "Asia/Kolkata",
         "Australia/Lord_Howe", "Pacific/Chatham", "UTC"]


@pytest.fixture
def zone():
    """``zone("Europe/Berlin")`` switches this process's local time zone."""
    old = os.environ.get("TZ")

    def set_(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()
    yield set_
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


# ── the pre-fix parsers (host-local), to build a store as the old build did ──

def _legacy_mlsd(val):
    if not val or len(val) < 14:
        return 0.0
    try:
        return datetime.datetime.strptime(val[:14], "%Y%m%d%H%M%S").timestamp()
    except (ValueError, OSError):
        return 0.0


def _legacy_list_unix(mon, day, year_or_time):
    mon_i = FTPFileSource._LIST_MONTHS.get(str(mon)[:3].lower())
    if mon_i is None:
        return 0.0
    try:
        day_i = int(day)
    except (ValueError, TypeError):
        return 0.0
    now = datetime.datetime.now()
    try:
        if ":" in year_or_time:
            hh, mm = year_or_time.split(":", 1)
            cand = datetime.datetime(now.year, mon_i, day_i, int(hh), int(mm))
            if cand.timestamp() > now.timestamp() + 86400:
                cand = cand.replace(year=now.year - 1)
            dt = cand
        else:
            dt = datetime.datetime(int(year_or_time), mon_i, day_i)
        return dt.timestamp()
    except (ValueError, OSError, OverflowError):
        return 0.0


def _legacy_parsers(monkeypatch):
    monkeypatch.setattr(FTPFileSource, "_parse_mtime", staticmethod(_legacy_mlsd))
    monkeypatch.setattr(FTPFileSource, "_parse_list_mtime_unix", staticmethod(_legacy_list_unix))
    # the old build didn't mark the archive sidecar
    monkeypatch.setattr(scanner, "_stamp_enum_sidecar_utc", lambda d: None)


# ── parsing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tz", ZONES)
def test_listing_times_are_utc_in_every_host_zone(zone, tz):
    zone(tz)
    want = calendar.timegm((2026, 3, 15, 12, 0, 7))
    assert FTPFileSource._parse_mtime("20260315120007") == want
    assert FTPFileSource._parse_mtime("20260315120007.123") == want
    assert FTPFileSource._parse_list_mtime_unix("Mar", "15", "2026") == \
        calendar.timegm((2026, 3, 15, 0, 0, 0))
    assert FTPFileSource._parse_list_mtime_windows("03-15-26", "12:00PM") == \
        calendar.timegm((2026, 3, 15, 12, 0, 0))
    # the recent form: a minute ago (UTC) is a minute ago, not hours away
    t = time.time() - 60
    g = time.gmtime(t)
    got = FTPFileSource._parse_list_mtime_unix(time.strftime("%b", g), str(g.tm_mday),
                                               time.strftime("%H:%M", g))
    assert abs(got - (t - g.tm_sec)) < 1


@pytest.mark.parametrize("mlsd", [True, False], ids=["MLSD", "LIST"])
def test_a_file_written_now_is_listed_at_now_not_hours_off(zone, tmp_path, mlsd):
    """The symptom: on a Pacific-time host every FTP folder looked 7 h in the
    future (east of UTC: recent changes looked old)."""
    zone("America/Los_Angeles")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.mod").write_bytes(b"x" * 10)
    srv = FtpServer(tmp_path, mlsd=mlsd).start()
    try:
        src = FTPFileSource("127.0.0.1", port=srv.port, remote_path="/")
        if not mlsd:
            src._use_mlsd = False
            src._mlsd_disabled_at = time.time()
        (e,) = [x for x in src._list_entries("/d") if x.name == "a.mod"]
        real = (tmp_path / "d" / "a.mod").stat().st_mtime
        assert abs(e.mtime - real) < (1.5 if mlsd else 61), (e.mtime, real)
    finally:
        _drop_pools(srv.port)
        srv.stop()


# ── the conversion ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("tz", ZONES)
def test_conversion_inverts_the_old_local_reading_exactly(zone, tz):
    zone(tz)
    rnd = random.Random(tz)
    wall = [datetime.datetime(2000, 1, 1) + datetime.timedelta(seconds=rnd.randrange(0, 27 * 365 * 86400))
            for _ in range(4000)]
    # every hour of the two DST-change days of a few years, by the minute
    for year in (2019, 2024, 2026):
        for month in range(1, 13):
            for day in (1, 5, 6, 7, 8, 25, 26, 27, 28, 29, 30, 31):
                try:
                    base = datetime.datetime(year, month, day)
                except ValueError:
                    continue
                if (base + datetime.timedelta(days=1)).timestamp() - base.timestamp() == 86400:
                    continue                      # no change that day in this zone
                wall += [base + datetime.timedelta(minutes=m) for m in range(0, 24 * 60, 7)]
    gap_hits = 0
    fast = mig._Converter()
    for w in wall:
        old = w.timestamp()                       # what the old parse stored
        new = mig.local_epoch_to_utc_epoch(old)
        assert fast(old) == new, (tz, w)          # the per-day shortcut agrees
        truth = calendar.timegm(w.timetuple())
        if new != truth:
            # Only in a spring-forward gap: two wall times share the stored
            # value; the earlier one is taken.
            twins = [x for x in (w - datetime.timedelta(minutes=m) for m in (30, 60, 120))
                     if x.timestamp() == old]
            assert twins, (tz, w, old, new, truth)
            assert new == calendar.timegm(min(twins).timetuple())
            gap_hits += 1
    if tz == "UTC":
        assert gap_hits == 0


def test_the_fall_back_hour_is_exact_and_the_gap_takes_the_earlier(zone):
    zone("America/Los_Angeles")
    first = datetime.datetime(2026, 11, 1, 1, 30)       # 01:30 happens twice
    assert mig.local_epoch_to_utc_epoch(first.timestamp()) == \
        calendar.timegm(first.timetuple())
    gap = datetime.datetime(2026, 3, 8, 2, 30)          # 02:30 never happens
    later = datetime.datetime(2026, 3, 8, 3, 30)
    assert gap.timestamp() == later.timestamp()          # one stored value, two walls
    assert mig.local_epoch_to_utc_epoch(later.timestamp()) == calendar.timegm(gap.timetuple())


def test_a_utc_host_changes_nothing(zone):
    zone("UTC")
    for v in (1.0, 1e9, 1.7e9 + 0.25, time.time()):
        assert mig.local_epoch_to_utc_epoch(v) == v


# ── the store migration ──────────────────────────────────────────────────────

def _store_with(tracks):
    from soniqboom.core.store import TrackStore
    st = TrackStore()
    for i, (path, m) in enumerate(tracks):
        st.upsert_track({"id": f"t{i}", "path": path, "title": f"T{i}", "format": "mod",
                         "mtime": m, "file_size": 100 + i})
    return st


def _berlin_rows():
    w = datetime.datetime(2026, 7, 1, 12, 0, 5)
    old = w.timestamp()
    from soniqboom.core import modland_catalog
    return w, old, [
        ("ftp://h/share:/A/x.mod", old),                         # 0 converted
        ("ftp://h/share:/A/pack.zip::m1.mod", old),              # 1 converted
        ("ftp://h/share:/A/pack.zip::m2.mod", old),              # 2 converted
        (modland_catalog.ROOT + ":/Protracker/a/b.mod", old),    # 3 catalogue: kept
        ("smb://h/s:/y.mod", old),                               # 4 kept
        ("webdav://h/s:/y.mod", old),                            # 5 kept
        ("/local/z.mod", old),                                   # 6 kept
        ("ftp://h/share:/A/never.mod", 0.0),                     # 7 unknown: kept
    ]


def test_migration_converts_only_ftp_listing_times_once(zone, tmp_data_dir):
    zone("Europe/Berlin")
    w, old, rows = _berlin_rows()
    st = _store_with(rows)
    out = mig.run(st)
    assert out["tracks"] == 3
    want = calendar.timegm(w.timetuple())
    got = {t["id"]: t["mtime"] for t in st.all_tracks()}
    assert got["t0"] == got["t1"] == got["t2"] == want == old + 7200
    assert [got[f"t{i}"] for i in (3, 4, 5, 6)] == [old] * 4 and got["t7"] == 0.0
    assert st.get_config(mig.MARKER_KEY)["state"] == "done"
    assert mig.run(st) == {"skipped": True}                 # never twice
    assert st.get_track("t0")["mtime"] == want


def test_a_crash_after_the_batch_never_shifts_twice(zone, tmp_data_dir, monkeypatch):
    zone("Europe/Berlin")
    w, old, rows = _berlin_rows()
    st = _store_with(rows)
    real = st.set_config

    def crash_at_done(key, value):
        if key == mig.MARKER_KEY and value.get("state") == "done":
            raise SystemExit("power cut")
        return real(key, value)
    monkeypatch.setattr(st, "set_config", crash_at_done)
    with pytest.raises(SystemExit):
        mig.run(st)
    monkeypatch.undo()
    assert st.get_config(mig.MARKER_KEY)["state"] == "pending"
    out = mig.run(st)                                         # the next start
    assert out["resumed"] is True and out["tracks"] == 0
    assert st.get_track("t0")["mtime"] == calendar.timegm(w.timetuple())


def test_a_crash_before_the_batch_converts_on_the_next_start(zone, tmp_data_dir, monkeypatch):
    zone("Europe/Berlin")
    w, old, rows = _berlin_rows()
    st = _store_with(rows)

    def crash(items):
        raise SystemExit("power cut")
    monkeypatch.setattr(st, "update_track_fields_batch", crash)
    with pytest.raises(SystemExit):
        mig.run(st)
    monkeypatch.undo()
    assert st.get_track("t0")["mtime"] == old
    out = mig.run(st)
    assert out["tracks"] == 3
    assert st.get_track("t0")["mtime"] == calendar.timegm(w.timetuple())


@pytest.mark.parametrize("then", ["AOF replay", "snapshot merge"])
def test_the_conversion_is_journaled_and_survives_a_restart(zone, tmp_path, monkeypatch, then):
    """Through the store's normal write path: the next start (AOF replay, or
    the snapshot the AOF was merged into) has the converted values and the
    marker — and does not convert again."""
    zone("Europe/Berlin")
    from soniqboom.core import persistence
    from soniqboom.core import store as store_mod
    from soniqboom.core.aof import AOFWriter
    monkeypatch.setattr(store_mod, "_store", None)
    w0, old, rows = _berlin_rows()
    persistence.init_persistence(tmp_path)
    st = store_mod.get_store()
    w = AOFWriter(tmp_path / "library.aof")
    st._aof_append = w.append
    for i, (path, m) in enumerate(rows):
        st.upsert_track({"id": f"t{i}", "path": path, "title": f"T{i}", "format": "mod",
                         "mtime": m, "file_size": 100 + i})
    w.flush_sync()
    persistence.write_snapshot_sync(tmp_path, consume_aof=True)   # scanned by the old build
    assert mig.run(st)["tracks"] == 3
    w.flush_sync()
    if then == "snapshot merge":
        persistence.write_snapshot_sync(tmp_path, consume_aof=True)
        assert (tmp_path / "library.aof").stat().st_size == 0
    w.stop()
    monkeypatch.setattr(store_mod, "_store", None)                 # restart
    persistence.init_persistence(tmp_path)
    st2 = store_mod.get_store()
    want = calendar.timegm(w0.timetuple())
    assert [st2.get_track(f"t{i}")["mtime"] for i in range(8)] == \
        [want, want, want, old, old, old, old, 0.0]
    assert st2.get_config(mig.MARKER_KEY)["state"] == "done"
    assert mig.run(st2) == {"skipped": True}


def test_the_archive_sidecar_is_converted_once(zone, tmp_data_dir):
    zone("America/Los_Angeles")
    w = datetime.datetime(2026, 1, 10, 8, 0, 0)
    old = w.timestamp()
    side = scanner._enum_sidecar_path()
    side.write_text(json.dumps({
        "barren": {"ftp://h/share|/A/empty.zip": [old, 10], "smb://h/s|/e.zip": [old, 10]},
        "named": {"ftp://h/share|/A/pack.zip": [old, 99, 2], "local|/x.zip": [old, 2]},
        "schemas": {"ftp://h/share": "abc"}}))
    st = _store_with([])
    assert mig.run(st)["sidecar"] == 2
    d = json.loads(side.read_text())
    want = calendar.timegm(w.timetuple())
    assert d["barren"]["ftp://h/share|/A/empty.zip"] == [want, 10]
    assert d["named"]["ftp://h/share|/A/pack.zip"] == [want, 99, 2]
    assert d["barren"]["smb://h/s|/e.zip"] == [old, 10]
    assert d["named"]["local|/x.zip"] == [old, 2] and d["schemas"] == {"ftp://h/share": "abc"}
    assert mig._convert_sidecar() == 0                      # marked: never twice
    assert json.loads(side.read_text())["named"]["ftp://h/share|/A/pack.zip"][0] == want


def test_a_sidecar_written_by_this_build_is_marked(tmp_data_dir):
    scanner._update_enum_sidecar("ftp://h/share", enum_report={"/a.zip": (1.7e9, 5, 1)},
                                 indexed_arcs={"/a.zip"}, failed_fetch_arcs=set(),
                                 stamp_schema=False)
    assert json.loads(scanner._enum_sidecar_path().read_text())[mig.SIDECAR_MARKER] == 1


# ── end to end: scanned before the fix, upgraded, polled ─────────────────────

def _drop_pools(port):
    from soniqboom.core import filesource
    with filesource._FTP_POOLS_LOCK:
        pools = [filesource._FTP_POOLS.pop(k) for k in list(filesource._FTP_POOLS)
                 if k[1] == port]
    for p in pools:
        p.close_all()


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("m1.mod", _mod(b"Zip one"))
        z.writestr("m2.mod", _mod(b"Zip two"))
    return buf.getvalue()


class _Share:
    def __init__(self, lib: Path, mlsd: bool):
        from soniqboom.core.filesource import register_source
        self.srv = FtpServer(lib.parent, mlsd=mlsd).start()
        self.root = f"ftp://127.0.0.1/{lib.name}"
        self.mlsd = mlsd
        self.src = self._new_src()
        register_source(self.root, self.src)

    def _new_src(self):
        src = FTPFileSource("127.0.0.1", port=self.srv.port, remote_path="/" + self.root.rsplit("/", 1)[1])
        if not self.mlsd:
            src._use_mlsd = False
            src._mlsd_disabled_at = time.time()
        return src

    def restart(self):
        """A new process: new source objects (and no folder state)."""
        from soniqboom.core import remote_dirstate
        from soniqboom.core.filesource import register_source, remove_source
        remove_source(self.root)
        _drop_pools(self.srv.port)
        with remote_dirstate._states_lock:
            remote_dirstate._states.clear()
        self.src = self._new_src()
        register_source(self.root, self.src)

    async def poll(self, fast: bool):
        self.srv.commands.clear()
        r0 = len(self.srv.retrs)
        cap = (time.time() - 15 * 60) if fast else None
        plan = await scanner.start_remote_scan("", self.root, self.src, dir_mtime_cap=cap)
        c = self.srv.commands
        return plan, {"RETR": len(self.srv.retrs) - r0, "SIZE": c["SIZE"],
                      "MLSD": c["MLSD"], "LIST": c["LIST"], "MDTM": c["MDTM"]}

    def stop(self):
        from soniqboom.core.filesource import remove_source
        remove_source(self.root)
        _drop_pools(self.srv.port)
        self.srv.stop()


@pytest.fixture
def ftp_share(fresh, request):
    lib, store = fresh
    (lib / "Other" / "pack.zip").write_bytes(_zip_bytes())
    old = time.time() - 7200
    os.utime(lib / "Other" / "pack.zip", (old, old))
    os.utime(lib / "Other", (old, old))
    sh = _Share(lib, mlsd=request.param)
    try:
        yield sh, lib, store
    finally:
        sh.stop()


def _ftp_mtimes(store):
    return {t["path"]: t["mtime"] for t in store.all_tracks() if t["path"].startswith("ftp://")}


@pytest.mark.parametrize("tz", ["America/Los_Angeles", "Europe/Berlin"])
@pytest.mark.parametrize("ftp_share", [True, False], ids=["MLSD", "LIST"], indirect=True)
@pytest.mark.parametrize("upgrade", ["migrated", "not-migrated"])
async def test_an_upgraded_share_is_not_read_again(zone, ftp_share, monkeypatch, tz, upgrade):
    sh, lib, store = ftp_share
    zone(tz)
    # 1. The old build scans the share (host-local listing times).
    mp = pytest.MonkeyPatch()
    _legacy_parsers(mp)
    plan, cnt = await sh.poll(fast=False)
    assert plan["new"] == 9, plan                     # 7 loose files + 2 zip members
    plan, idle_old = await sh.poll(fast=False)        # the old build is stable with itself
    assert plan["extract"] == 0 and idle_old["RETR"] == 0
    stored_old = _ftp_mtimes(store)
    mp.undo()

    # 2. Upgrade: a new process with the fixed parsers (+ the conversion).
    sh.restart()
    side_old = json.loads(scanner._enum_sidecar_path().read_text())
    (zkey,) = [k for k in side_old["named"] if k.endswith("/Other/pack.zip")]
    assert mig.SIDECAR_MARKER not in side_old
    if upgrade == "migrated":
        out = mig.run(store)
        assert out["tracks"] == 9 and out["sidecar"] >= 1, out
        side = json.loads(scanner._enum_sidecar_path().read_text())
        assert side["named"][zkey][0] == mig.local_epoch_to_utc_epoch(side_old["named"][zkey][0])
        assert side["named"][zkey][0] == _ftp_mtimes(store)[f"{sh.root}:/Other/pack.zip::m1.mod"]
    plan, cnt = await sh.poll(fast=False)              # the first poll after a restart
    print(f"[{tz} {'MLSD' if sh.mlsd else 'LIST'} {upgrade}] old-build idle poll {idle_old}; "
          f"first poll after upgrade: extract={plan['extract']} {cnt}")
    if upgrade == "not-migrated":
        # The falsifier: without the conversion every file is read again.
        assert plan["extract"] == 9 and cnt["RETR"] >= 7, (plan, cnt)
        return
    assert plan["extract"] == 0 and plan["new"] == 0 and plan["ghosts"] == 0, plan
    assert plan["mtime_refresh"] == 0
    assert cnt["RETR"] == 0 and cnt["SIZE"] == 0 and cnt["MDTM"] == 0, cnt
    # listings: exactly what the old build's own idle full poll made
    assert (cnt["MLSD"], cnt["LIST"]) == (idle_old["MLSD"], idle_old["LIST"]), (cnt, idle_old)
    new = _ftp_mtimes(store)
    assert set(new) == set(stored_old)
    for p, m in new.items():
        assert m == mig.local_epoch_to_utc_epoch(stored_old[p])
    plan, cnt = await sh.poll(fast=True)
    assert plan["extract"] == 0 and cnt["RETR"] == 0, (plan, cnt)

    # 3. A file changed after the upgrade is still found (same size, new time).
    target = lib / "Artist" / "Album" / "x1.mod"
    size = target.stat().st_size
    target.write_bytes(_mod(b"X one edited"))
    assert target.stat().st_size == size
    later = time.time() + 120                          # a later minute (LIST precision)
    os.utime(target, (later, later))
    plan, cnt = await sh.poll(fast=False)
    print(f"  after a post-upgrade edit: extract={plan['extract']} {cnt}")
    assert plan["extract"] == 1 and cnt["RETR"] == 1, (plan, cnt)
    t = [t for t in store.all_tracks() if t["path"].endswith("/x1.mod")][0]
    assert t["title"] == "X one edited"
    assert abs(t["mtime"] - later) < (1.5 if sh.mlsd else 61)


@pytest.mark.parametrize("backup_from", ["old build", "this build"])
def test_a_web_import_converts_an_old_builds_backup_once(zone, tmp_data_dir, monkeypatch, backup_from):
    """A backup imported while the server runs replaces the store config (and
    its marker): one from a build that stored host times is converted at the
    import, one from this build (marker "done") is left as it is."""
    import asyncio
    import gzip
    from soniqboom.api import admin
    from soniqboom.core import persistence
    from soniqboom.core import store as store_mod
    zone("Europe/Berlin")
    monkeypatch.setattr(store_mod, "_store", None)
    persistence.init_persistence(tmp_data_dir)
    w = datetime.datetime(2026, 7, 1, 12, 0, 5)
    local_old, utc = w.timestamp(), float(calendar.timegm(w.timetuple()))
    row = {"id": "a", "path": "ftp://h/s:/a.mod", "title": "A", "format": "mod", "file_size": 1}
    if backup_from == "old build":
        data = {"tracks": {"a": {**row, "mtime": local_old}}, "config": {}}
    else:
        data = {"tracks": {"a": {**row, "mtime": utc}},
                "config": {mig.MARKER_KEY: {"state": "done"}}}

    class _Up:
        async def read(self):
            return gzip.compress(json.dumps({"version": 2, "data": data}).encode())
    asyncio.run(admin.admin_import(file=_Up(), _tok="x"))
    st = store_mod.get_store()
    assert st.get_track("a")["mtime"] == utc
    assert st.get_config(mig.MARKER_KEY)["state"] == "done"
    assert mig.run(st) == {"skipped": True}                    # the next start: nothing more
    assert st.get_track("a")["mtime"] == utc
