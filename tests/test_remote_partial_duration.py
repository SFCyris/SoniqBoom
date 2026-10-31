# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Network-share MP3 / Ogg files get their whole length, not their front
window's (``core/partial_length.py``).

The remote scan reads a front window of most files (``HEADER_BUDGET``, ×4,
then whole).  mutagen measured a CBR MP3 without a frame count, and an Ogg
Vorbis / Opus file, over that window: a 13 min 320 kbps MP3 was stored as
0:07 (512 KB window minus its cover).  The scan now re-computes the MP3
length from the listing size and the ID3v2 size, and reads an Ogg file's last
page; tracks stored before are corrected once per share by the backfill.

Real files made with ffmpeg (skipped without it), scanned through the test
FTP server (real ``read_partial`` / ``REST`` reads) and an in-memory source
that can fail, with a small budget so the window is a fraction of the file."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _ftp_server import FtpServer  # noqa: E402

from soniqboom.core import partial_length, scanner  # noqa: E402
from soniqboom.core.filesource import DirEntry, FileSource  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
# (the fixture below replaces it; the scheduling test calls the real one)
_REAL_SCHEDULE = partial_length.schedule_backfill

OLD = time.time() - 7200
SECS = 60
# Small budgets: the first window is a fraction of every file, and the MP3
# cover (300 KB) is larger than the first one (escalation to 4×).
BUDGET = {".mp3": 128 * 1024, ".ogg": 64 * 1024, ".opus": 64 * 1024}


def _encoders() -> str:
    try:
        return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=30).stdout
    except Exception:                                   # noqa: BLE001
        return ""


def _ffmpeg(*args: str) -> None:
    src = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={SECS}",
           "-f", "lavfi", "-i", f"anoisesrc=d={SECS}:a=0.1",
           "-filter_complex", "amix=inputs=2", "-ac", "2"]
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *src, *args],
                   check=True, timeout=120)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    """name → file: a CBR 320 kbps MP3 without a Xing / Info header (and a
    copy with a 300 KB cover), a LAME VBR MP3 (Xing frame count), an Ogg
    Vorbis and an Opus file — 60 s each — and, where ffmpeg has its own
    Vorbis encoder, an Ogg Vorbis that states no bitrate (vn.ogg)."""
    from mutagen.id3 import APIC, ID3, TIT2
    d = tmp_path_factory.mktemp("media")
    enc = _encoders()
    out: dict[str, Path] = {}
    _ffmpeg("-ar", "44100", "-c:a", "libmp3lame", "-b:a", "320k", "-write_xing", "0",
            str(d / "cbr.mp3"))
    out["cbr.mp3"] = d / "cbr.mp3"
    shutil.copy(d / "cbr.mp3", d / "cbr_cover.mp3")
    tag = ID3()
    tag.add(TIT2(encoding=3, text="Song Of Myself"))
    tag.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover",
                 data=os.urandom(300_000)))
    tag.save(d / "cbr_cover.mp3")
    out["cbr_cover.mp3"] = d / "cbr_cover.mp3"
    _ffmpeg("-ar", "44100", "-c:a", "libmp3lame", "-q:a", "2", str(d / "vbr.mp3"))
    out["vbr.mp3"] = d / "vbr.mp3"
    vorbis = (["-c:a", "libvorbis"] if " libvorbis " in enc
              else ["-c:a", "vorbis", "-strict", "-2"] if " vorbis " in enc else None)
    if vorbis:
        _ffmpeg(*vorbis, "-q:a", "6", str(d / "v.ogg"))
        out["v.ogg"] = d / "v.ogg"
    if " vorbis " in enc:                    # libvorbis states a nominal bitrate
        _ffmpeg("-c:a", "vorbis", "-strict", "-2", "-q:a", "6", str(d / "vn.ogg"))
        out["vn.ogg"] = d / "vn.ogg"
    opus = (["-c:a", "libopus"] if " libopus " in enc
            else ["-c:a", "opus", "-strict", "-2"] if " opus " in enc else None)
    if opus:
        _ffmpeg(*opus, "-b:a", "128k", str(d / "o.opus"))
        out["o.opus"] = d / "o.opus"
    return out


def _real(p: Path) -> float:
    import mutagen
    return mutagen.File(p).info.length


def _ffprobe(p: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


# ── the helpers on their own ────────────────────────────────────────────────

def test_mp3_length_from_a_window(media):
    from mutagen.mp3 import MPEGInfo
    import io
    for name in ("cbr.mp3", "cbr_cover.mp3"):
        data = media[name].read_bytes()
        start = partial_length.mp3_audio_start(data)
        head = data[:start + 200_000]
        info = MPEGInfo(io.BytesIO(head))
        assert info.length < 6                          # the bug: the window's length
        got = partial_length.mp3_length(head, len(data), info.length, info.bitrate)
        assert got is not None and abs(got - _real(media[name])) < 0.05, (name, got)
    # A Xing frame count: already the file's length.
    data = media["vbr.mp3"].read_bytes()
    info = MPEGInfo(io.BytesIO(data[:200_000]))
    assert abs(info.length - _real(media["vbr.mp3"])) < 0.05
    assert partial_length.mp3_length(data[:200_000], len(data), info.length, info.bitrate) is None
    # A window ending inside the cover tells nothing; nor does the whole file.
    data = media["cbr_cover.mp3"].read_bytes()
    assert partial_length.mp3_length(data[:100_000], len(data), 1.0, 320000) is None
    assert partial_length.mp3_length(data, len(data), _real(media["cbr_cover.mp3"]), 320000) is None


def test_mp3_audio_start_reads_the_id3v2_size(media):
    data = media["cbr_cover.mp3"].read_bytes()
    from mutagen.mp3 import MP3
    assert partial_length.mp3_audio_start(data[:10]) == MP3(media["cbr_cover.mp3"]).info.frame_offset
    assert partial_length.mp3_audio_start(b"\xff\xfb\x90\x00" * 4) == 0
    # A footer flag adds 10 bytes; stacked tags are all stepped over.
    footer = b"ID3\x04\x00\x10\x00\x00\x00\x05"
    assert partial_length.mp3_audio_start(footer) == 10 + 5 + 10
    two = b"ID3\x03\x00\x00\x00\x00\x00\x02" + b"\x00\x00" + b"ID3\x03\x00\x00\x00\x00\x00\x03"
    assert partial_length.mp3_audio_start(two + b"\x00" * 3) == 12 + 13


@pytest.mark.parametrize("name", ["v.ogg", "o.opus"])
def test_ogg_length_from_head_and_tail(media, name):
    if name not in media:
        pytest.skip(f"ffmpeg has no encoder for {name}")
    data = media[name].read_bytes()
    got = partial_length.ogg_length(data[:64 * 1024], data[-partial_length.OGG_TAIL:])
    assert got is not None and abs(got - _real(media[name])) < 0.01, got
    # Not an Ogg file / a tail of another stream.
    assert partial_length.ogg_length(b"\x00" * 100, data[-1000:]) is None
    assert partial_length.ogg_length(data[:64 * 1024], b"no pages here") is None


# ── harness ─────────────────────────────────────────────────────────────────

@pytest.fixture
def blank(tmp_path, monkeypatch, tmp_data_dir):
    from soniqboom.core import remote_dirstate
    from soniqboom.core import store as store_mod
    from soniqboom.core.metadata import HEADER_BUDGET
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())

    async def _none():
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _none)
    monkeypatch.setattr(scanner, "_spawn_scene_autoapply", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_schedule_folder_album_pass", lambda *a, **k: None)
    # (the backfill is run by hand here, never in the background)
    monkeypatch.setattr(partial_length, "schedule_backfill", lambda: None)
    monkeypatch.setattr(partial_length, "_scan_pending", set())
    monkeypatch.setattr(partial_length, "_settled", set())
    monkeypatch.setattr(partial_length, "_status", partial_length._idle_status())
    for ext, b in BUDGET.items():
        monkeypatch.setitem(HEADER_BUDGET, ext, b)
    with remote_dirstate._states_lock:
        remote_dirstate._states.clear()
    return store_mod._store


def _lib(tmp_path: Path, media: dict[str, Path]) -> Path:
    lib = tmp_path / "lib"
    alb = lib / "Artist" / "Album"
    alb.mkdir(parents=True)
    for name, p in media.items():
        shutil.copy(p, alb / name)
    for q in sorted(lib.rglob("*"), key=lambda q: -len(q.parts)):
        os.utime(q, (OLD, OLD))
    os.utime(lib, (OLD, OLD))
    return lib


class _Ftp:
    def __init__(self, lib: Path):
        from soniqboom.core.filesource import FTPFileSource, register_source
        self.srv = FtpServer(lib.parent).start()
        self.root = f"ftp://127.0.0.1/{lib.name}"
        self.src = FTPFileSource("127.0.0.1", port=self.srv.port, remote_path=f"/{lib.name}")
        register_source(self.root, self.src)

    def stop(self):
        from soniqboom.core import filesource
        filesource.remove_source(self.root)
        with filesource._FTP_POOLS_LOCK:
            pools = [filesource._FTP_POOLS.pop(k) for k in list(filesource._FTP_POOLS)
                     if k[1] == self.srv.port]
        for p in pools:
            p.close_all()
        self.srv.stop()


class _Disk(FileSource):
    """A local folder as a share with real range reads (``read_partial`` /
    ``read_at`` overridden), counting them; ``fail_at`` / ``fail_partial``
    make those reads raise a transient error."""

    def __init__(self, base: Path):
        self.base = base
        self.reads: list[tuple[str, str, int, int]] = []
        self.fail_at = False
        self.fail_partial = False

    def _p(self, path: str) -> Path:
        return self.base / path.lstrip("/")

    def list_dir(self, path):
        d = self._p(path or "/")
        out = []
        for c in sorted(d.iterdir()):
            st = c.stat()
            out.append(DirEntry(c.name, "/" + str(c.relative_to(self.base)), c.is_dir(),
                                0 if c.is_dir() else st.st_size, st.st_mtime))
        return out

    def walk(self, top):
        raise NotImplementedError

    def read_file(self, path, *, lane="stream"):
        self.reads.append(("file", path, 0, -1))
        return self._p(path).read_bytes()

    def read_partial(self, path, max_bytes, *, lane="scan"):
        self.reads.append(("partial", path, 0, max_bytes))
        if self.fail_partial:
            raise ConnectionResetError("share dropped")
        with open(self._p(path), "rb") as fh:
            return fh.read(max_bytes)

    def read_at(self, path, offset, length, *, lane="scan"):
        self.reads.append(("at", path, offset, length))
        if self.fail_at:
            raise TimeoutError("timed out")
        with open(self._p(path), "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    def stat(self, path, *, lane="scan"):
        raise FileNotFoundError(path)

    def is_dir(self, path):
        return self._p(path).is_dir()


def _by_name(store) -> dict[str, dict]:
    return {t["path"].rsplit("/", 1)[-1]: t for t in store._tracks.values()}


def _root_hash(root: str) -> str:
    return hashlib.sha256(root.encode()).hexdigest()[:16]


def _without_fix(monkeypatch) -> None:
    """The scan as it was before the fix: the window's length is stored."""
    monkeypatch.setattr(partial_length, "mp3_length", lambda *a, **k: None)
    monkeypatch.setattr(partial_length, "ogg_length", lambda *a, **k: None)


# ── the scan ────────────────────────────────────────────────────────────────

async def test_ftp_scan_stores_whole_lengths(blank, media, tmp_path, monkeypatch):
    store = blank
    lib = _lib(tmp_path, media)
    sh = _Ftp(lib)
    try:
        # Before: what the scan stored without the fix (reproduces the bug).
        with monkeypatch.context() as m:
            _without_fix(m)
            await scanner.start_remote_scan("", sh.root, sh.src)
        before = {n: t["duration"] for n, t in _by_name(store).items()}
        for t in list(store._tracks.values()):
            store.delete_track(t["id"])
        remote_dirstate_clear()
        await scanner.start_remote_scan("", sh.root, sh.src)
        got = _by_name(store)
    finally:
        sh.stop()
    assert set(got) == set(media)
    for name, p in media.items():
        real = _real(p)
        assert abs(got[name]["duration"] - real) < 1.0, (name, got[name]["duration"], real)
        probe = _ffprobe(p)
        if probe:
            assert abs(got[name]["duration"] - probe) < 1.0, (name, probe)
    # The bug was real for each partial-read format, and the fix only grew it.
    for name in ("cbr.mp3", "cbr_cover.mp3", "v.ogg", "o.opus"):
        if name in media:
            assert before[name] < SECS / 4, (name, before[name])
    assert abs(before["vbr.mp3"] - got["vbr.mp3"]["duration"]) < 1e-6    # Xing: untouched
    # The cover took the window to 4× (the escalation stage) — still fixed.
    assert got["cbr_cover.mp3"]["title"] == "Song Of Myself"


def remote_dirstate_clear() -> None:
    from soniqboom.core import remote_dirstate
    with remote_dirstate._states_lock:
        remote_dirstate._states.clear()


async def test_a_scan_marks_the_root_done_when_nothing_is_left_to_check(
        blank, media, tmp_path, monkeypatch):
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    calls = []
    monkeypatch.setattr(partial_length, "schedule_backfill", lambda: calls.append(1))
    # (ffmpeg's own Vorbis encoder states no bitrate: such a file stays a
    # candidate after any scan — below)
    lib = _lib(tmp_path, {n: p for n, p in media.items() if n not in ("v.ogg", "vn.ogg")})
    src, root = _Disk(lib), "smb://mem/first-scan"
    register_source(root, src)
    try:
        await scanner.start_remote_scan("", root, src)
    finally:
        remove_source(root)
    assert partial_length.backfill_done(_root_hash(root), store) and not calls
    if "vn.ogg" not in media:
        pytest.skip("this ffmpeg has no Vorbis encoder of its own")
    lib2 = _lib(tmp_path / "two", {"v.ogg": media["vn.ogg"]})
    src2, root2 = _Disk(lib2), "smb://mem/vorbis-without-bitrate"
    register_source(root2, src2)
    try:
        await scanner.start_remote_scan("", root2, src2)
        t = _by_name(store)["v.ogg"]
        assert not t.get("bitrate") and abs(t["duration"] - _real(media["vn.ogg"])) < 1.0
        assert not partial_length.backfill_done(_root_hash(root2), store) and calls == [1]
        res = await partial_length.run_backfill(store)      # read once: already right
        assert res["fixed"] == 0 and res["skipped"] == 1, res
        assert partial_length.backfill_done(_root_hash(root2), store)
    finally:
        remove_source(root2)


async def test_ogg_end_read_failure_keeps_the_window_and_leaves_the_root_due(
        blank, media, tmp_path):
    from soniqboom.core.filesource import register_source, remove_source
    oggs = {n: p for n, p in media.items() if n in ("v.ogg", "o.opus")}
    if not oggs:
        pytest.skip("ffmpeg has no Vorbis / Opus encoder")
    store = blank
    lib = _lib(tmp_path, oggs)
    src, root = _Disk(lib), "smb://mem/ogg-tail-fails"
    src.fail_at = True
    register_source(root, src)
    try:
        await scanner.start_remote_scan("", root, src)
        got = _by_name(store)
        for name in oggs:
            assert got[name]["duration"] < SECS / 4            # the window's, kept
            assert partial_length.backfill_kind(got[name]) == "ogg"
        assert not partial_length.backfill_done(_root_hash(root), store)
        # The share answers again: the backfill corrects them.
        src.fail_at = False
        res = await partial_length.run_backfill(store)
        assert res["fixed"] == len(oggs), res
        for name, p in oggs.items():
            assert abs(_by_name(store)[name]["duration"] - _real(p)) < 1.0
        assert partial_length.backfill_done(_root_hash(root), store)
    finally:
        remove_source(root)


# ── the backfill of stored tracks ───────────────────────────────────────────

async def _old_library(store, monkeypatch, src, root) -> dict[str, dict]:
    """Scan as a version before the fix did, and forget the done marker its
    first full scan earned (the library predates the backfill)."""
    with monkeypatch.context() as m:
        _without_fix(m)
        await scanner.start_remote_scan("", root, src)
    store.set_config(partial_length.BACKFILL_ROOTS_KEY, [])
    return {n: dict(t) for n, t in _by_name(store).items()}


async def test_backfill_fixes_capped_lengths_once(blank, media, tmp_path, monkeypatch):
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    lib = _lib(tmp_path, media)
    src, root = _Disk(lib), "smb://mem/backfill"
    register_source(root, src)
    try:
        old = await _old_library(store, monkeypatch, src, root)
        kinds = {n: partial_length.backfill_kind(t) for n, t in old.items()}
        assert kinds["cbr.mp3"] == kinds["cbr_cover.mp3"] == "mp3"
        assert kinds["vbr.mp3"] is None                         # Xing length: not capped
        # A hand-edited length is never touched.
        edited = old["cbr.mp3"]["id"]
        store.update_track_fields(edited, {"user_edited": ["duration"]})
        store.take_dup_dirty()
        n0 = len(src.reads)
        res = await partial_length.run_backfill(store)
        reads = src.reads[n0:]
        got = _by_name(store)
        fixed = [n for n in media if n not in ("cbr.mp3", "vbr.mp3")]
        assert res["fixed"] == len(fixed) and res["unreachable"] == 0, res
        for name in fixed:
            assert abs(got[name]["duration"] - _real(media[name])) < 1.0, name
        assert got["cbr.mp3"]["duration"] == old["cbr.mp3"]["duration"]
        assert got["vbr.mp3"]["duration"] == old["vbr.mp3"]["duration"]
        # Small range reads only: 10 bytes of an MP3, an Ogg file's first
        # page and last 64 KB — never a whole file.
        assert not [r for r in reads if r[0] == "file"], reads
        mp3_reads = [r for r in reads if r[1].endswith("cbr_cover.mp3")]
        assert mp3_reads == [("partial", "/Artist/Album/cbr_cover.mp3", 0, 10)]
        assert all(r[3] <= partial_length.OGG_TAIL for r in reads)
        # The duplicate grouping follows (a duration is one of its inputs).
        dirty, _over = store.take_dup_dirty()
        assert {got[n]["id"] for n in fixed} <= set(dirty)
        assert partial_length.backfill_done(_root_hash(root), store)
        # Done: a second run reads nothing.
        n1 = len(src.reads)
        res2 = await partial_length.run_backfill(store)
        assert res2["fixed"] == 0 and len(src.reads) == n1
        # Not done any more: already-right lengths are not written again.
        store.set_config(partial_length.BACKFILL_ROOTS_KEY, [])
        res3 = await partial_length.run_backfill(store)
        assert res3["fixed"] == 0, res3
        assert {n: t["duration"] for n, t in _by_name(store).items()} == {
            n: t["duration"] for n, t in got.items()}
    finally:
        remove_source(root)


async def test_backfill_defers_an_unreachable_root(blank, media, tmp_path, monkeypatch):
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    lib = _lib(tmp_path, {n: media[n] for n in ("cbr.mp3", "cbr_cover.mp3")})
    src, root = _Disk(lib), "smb://mem/unreachable"
    register_source(root, src)
    try:
        old = await _old_library(store, monkeypatch, src, root)
    finally:
        remove_source(root)
    h = _root_hash(root)
    # Not connected: deferred, nothing read, not done.
    n0 = len(src.reads)
    res = await partial_length.run_backfill(store)
    assert res["deferred_roots"] == 1 and res["fixed"] == 0 and len(src.reads) == n0
    assert not partial_length.backfill_done(h, store)
    register_source(root, src)
    try:
        # Connected, but its reads fail (the share dropped): still not done.
        src.fail_partial = True
        res = await partial_length.run_backfill(store)
        assert res["unreachable"] == 2 and res["fixed"] == 0, res
        assert not partial_length.backfill_done(h, store)
        assert {n: t["duration"] for n, t in _by_name(store).items()} == {
            n: t["duration"] for n, t in old.items()}
        # Marked offline: deferred without a read.
        store.upsert_scan_dir(root, status="unavailable")
        src.fail_partial = False
        n1 = len(src.reads)
        res = await partial_length.run_backfill(store)
        assert res["deferred_roots"] == 1 and len(src.reads) == n1
        # Back: fixed and done.
        store.upsert_scan_dir(root, status="ok")
        res = await partial_length.run_backfill(store)
        assert res["fixed"] == 2, res
        assert partial_length.backfill_done(h, store)
    finally:
        remove_source(root)


async def test_backfill_skips_a_file_the_share_refuses(blank, media, tmp_path, monkeypatch):
    """A file that is gone (a permanent error) is skipped, and the root is
    still marked done — it can't keep the backfill pending forever."""
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    lib = _lib(tmp_path, {"cbr.mp3": media["cbr.mp3"]})
    src, root = _Disk(lib), "smb://mem/refused"
    register_source(root, src)
    try:
        await _old_library(store, monkeypatch, src, root)
        (lib / "Artist" / "Album" / "cbr.mp3").unlink()
        res = await partial_length.run_backfill(store)
        assert res["skipped"] == 1 and res["unreachable"] == 0, res
        assert partial_length.backfill_done(_root_hash(root), store)
    finally:
        remove_source(root)


def test_a_scan_that_kept_a_window_length_unmarks_its_root(blank, monkeypatch):
    store = blank
    calls = []
    monkeypatch.setattr(partial_length, "schedule_backfill", lambda: calls.append(1))
    h = _root_hash("ftp://h/a")
    partial_length.mark_done(h, store)
    partial_length.after_remote_scan("ftp://h/a", {"walked": 1})
    assert partial_length.backfill_done(h, store) and not calls       # done: nothing
    partial_length.note_pending(h, store)                 # an Ogg end it couldn't read
    assert not partial_length.backfill_done(h, store)
    partial_length.mark_done(h, store)                    # a backfill run ending meanwhile
    partial_length.after_remote_scan("ftp://h/a", {"walked": 1})
    assert not partial_length.backfill_done(h, store) and calls == [1]


def test_backfill_kind_rules():
    from soniqboom.core.metadata import HEADER_BUDGET
    base = HEADER_BUDGET[".mp3"]
    capped = {"path": "ftp://h/s:/a/x.mp3", "file_size": 33_000_000,
              "duration": 7.0, "bitrate": 320000}
    assert partial_length.backfill_kind(capped) == "mp3"
    assert partial_length.backfill_kind(dict(capped, duration=817.0)) is None   # right
    assert partial_length.backfill_kind(dict(capped, user_edited=["duration"])) is None
    assert partial_length.backfill_kind(dict(capped, path="/local/x.mp3")) is None
    assert partial_length.backfill_kind(dict(capped, path="ftp://h/s:/a/p.zip::x.mp3")) is None
    assert partial_length.backfill_kind(dict(capped, file_size=base * 2)) is None  # read whole
    assert partial_length.backfill_kind(dict(capped, file_size=None)) is None
    assert partial_length.backfill_kind(dict(capped, duration=0)) is None
    assert partial_length.backfill_kind(dict(capped, bitrate=None)) is None
    assert partial_length.backfill_kind(dict(capped, path="ftp://h/s:/a/x.flac")) is None
    ogg = {"path": "smb://h/s:/a/x.ogg", "file_size": 9_000_000, "duration": 13.0,
           "bitrate": 0}
    assert partial_length.backfill_kind(ogg) == "ogg"                  # no bitrate: read it
    assert partial_length.backfill_kind(dict(ogg, bitrate=160000)) == "ogg"
    assert partial_length.backfill_kind(dict(ogg, bitrate=160000, duration=450.0)) is None


async def test_schedule_backfill_runs_in_the_background(monkeypatch):
    runs = []

    async def _fake(store=None):
        runs.append(1)
        if len(runs) == 1:
            _REAL_SCHEDULE()            # called while it runs: once more after it
        await asyncio.sleep(0)
        return {}
    monkeypatch.setattr(partial_length, "run_backfill", _fake)
    monkeypatch.setattr(partial_length, "_task", None)
    _REAL_SCHEDULE()
    _REAL_SCHEDULE()                    # before it started: the same run
    await partial_length._task
    assert runs == [1, 1]


def _vbr_with_trailing_cover(d: Path) -> Path:
    """A 20 s LAME VBR MP3 (Xing frame count) followed by a 900 KB APEv2
    cover: its audio fits a front window, but it is no window estimate."""
    from mutagen.apev2 import APEv2, APEValue, BINARY
    p = d / "vbr_ape.mp3"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
                    "-f", "lavfi", "-i", "anoisesrc=d=20:a=0.1",
                    "-filter_complex", "amix=inputs=2", "-ac", "2", "-ar", "44100",
                    "-c:a", "libmp3lame", "-q:a", "2", str(p)], check=True, timeout=120)
    tag = APEv2()
    tag["Cover Art (Front)"] = APEValue(b"cover.jpg\x00" + os.urandom(900_000), BINARY)
    tag.save(p)
    return p


async def test_a_right_length_with_bytes_behind_the_audio_is_left_alone(blank, tmp_path):
    """A short Xing VBR file with a big trailing tag: its stored length is
    right — neither the scan nor the backfill may lengthen it."""
    import io
    from mutagen.mp3 import MPEGInfo
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    d = tmp_path / "lib" / "A"
    d.mkdir(parents=True)
    p = _vbr_with_trailing_cover(d)
    data = p.read_bytes()
    real = _real(p)
    for w in (128 * 1024, 512 * 1024):
        info = MPEGInfo(io.BytesIO(data[:w]))
        assert abs(info.length - real) < 0.05
        assert partial_length.mp3_length(data[:w], len(data), info.length, info.bitrate) is None
    for q in sorted((tmp_path / "lib").rglob("*"), key=lambda q: -len(q.parts)):
        os.utime(q, (OLD, OLD))
    src, root = _Disk(tmp_path / "lib"), "smb://mem/trailing-tag"
    register_source(root, src)
    try:
        await scanner.start_remote_scan("", root, src)
        t = _by_name(store)["vbr_ape.mp3"]
        assert abs(t["duration"] - real) < 0.05, t["duration"]
        store.set_config(partial_length.BACKFILL_ROOTS_KEY, [])
        assert partial_length.backfill_kind(t) == "mp3"          # read to tell
        res = await partial_length.run_backfill(store)
        assert res["fixed"] == 0 and res["skipped"] == 1, res
        assert abs(_by_name(store)["vbr_ape.mp3"]["duration"] - real) < 0.05
    finally:
        remove_source(root)


async def test_a_file_that_never_reads_is_given_up(blank, media, tmp_path, monkeypatch):
    """Retries read only the files not read yet; a file that keeps failing is
    given up after ``_MAX_UNREACHABLE_RUNS`` counted runs, so its root is
    done instead of pending forever."""
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    lib = _lib(tmp_path, {n: media[n] for n in ("cbr.mp3", "cbr_cover.mp3")})
    src, root = _Disk(lib), "smb://mem/never-reads"
    register_source(root, src)
    monkeypatch.setattr(partial_length, "_UNREACHABLE_INTERVAL_S", 0.0)
    real_partial = src.read_partial

    def flaky(path, n, *, lane="scan"):
        if path.endswith("/cbr.mp3"):
            src.reads.append(("partial", path, 0, n))
            raise ConnectionResetError("dropped")
        return real_partial(path, n, lane=lane)
    try:
        await _old_library(store, monkeypatch, src, root)
        src.read_partial = flaky
        h = _root_hash(root)
        res = await partial_length.run_backfill(store)
        assert res["fixed"] == 1 and res["unreachable"] == 1, res
        assert not partial_length.backfill_done(h, store)
        for run in range(2, partial_length._MAX_UNREACHABLE_RUNS + 1):
            n0 = len(src.reads)
            res = await partial_length.run_backfill(store)
            # only the failing file is read again
            assert {r[1].rsplit("/", 1)[-1] for r in src.reads[n0:]} == {"cbr.mp3"}
            assert partial_length.backfill_done(h, store) == (
                run == partial_length._MAX_UNREACHABLE_RUNS), (run, res)
        assert not store.get_config(partial_length.BACKFILL_ATTEMPTS_KEY)
    finally:
        remove_source(root)


# ── progress, status, concurrency ───────────────────────────────────────────

def _mp3_lib(tmp_path: Path, media: dict[str, Path], n: int) -> Path:
    """``n`` copies of the capped CBR MP3 (one share, ``n`` candidates)."""
    return _lib(tmp_path, {f"c{i:02d}.mp3": media["cbr.mp3"] for i in range(n)})


async def test_backfill_status_and_progress_log(blank, media, tmp_path, monkeypatch, caplog):
    """While a run reads a share the status names it (without credentials)
    and counts its candidates; progress is logged every ``_LOG_EVERY_N``
    files; afterwards ``last`` holds the summary and ``running`` is off."""
    import logging
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    lib = _mp3_lib(tmp_path, media, 5)
    src, root = _Disk(lib), "smb://bob:s3cret@mem/status"
    register_source(root, src)
    seen: list[dict] = []
    real_partial = src.read_partial

    def slow(path, n, *, lane="scan"):
        seen.append(partial_length.get_status())
        return real_partial(path, n, lane=lane)
    try:
        await _old_library(store, monkeypatch, src, root)
        assert partial_length.get_status()["running"] is False
        src.read_partial = slow
        monkeypatch.setattr(partial_length, "_LOG_EVERY_N", 2)
        monkeypatch.setattr(partial_length, "_LOG_EVERY_S", 3600.0)
        caplog.set_level(logging.INFO, logger="soniqboom.core.partial_length")
        t_before = time.time()
        res = await partial_length.run_backfill(store)
    finally:
        remove_source(root)
    assert res["fixed"] == 5, res
    # During the run.
    assert len(seen) == 5
    for st in seen:
        assert st["running"] is True and st["root"] == "smb://mem/status"
        assert st["label"] == "smb://mem/status"
        assert st["current"]["total"] == st["overall"]["total"] == 5
        assert st["started_at"] >= t_before - 1
    checked = [st["current"]["checked"] for st in seen]
    assert checked == sorted(checked) and checked[0] < 5 and max(checked) >= 3, checked
    # After it.
    st = partial_length.get_status()
    assert st["running"] is False and st["root"] is None and st["roots_done"] == 1
    assert st["roots_pending"] == 0
    assert st["current"] == st["overall"] == {
        "total": 5, "checked": 5, "fixed": 5, "skipped": 0, "unreachable": 0}
    last = st["last"]
    assert (last["checked"], last["fixed"], last["roots"], last["deferred_roots"]) == (5, 5, 1, 0)
    assert last["label"] == "smb://mem/status" and last["finished_at"] >= t_before
    # The log: start, progress by count and by time, result — no password.
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cret" not in text and "s3cret" not in repr(st)
    assert "checking 5 track(s) on smb://mem/status" in text
    progress = [r.getMessage().split(",")[0] for r in caplog.records
                if " of 5 checked" in r.getMessage()]
    assert progress == ["Length backfill of smb://mem/status: 2 of 5 checked",
                        "Length backfill of smb://mem/status: 4 of 5 checked"], progress
    assert "Length backfill of smb://mem/status: 5 of 5 track(s) fixed" in text
    # A run with nothing left to do keeps the last summary.
    await partial_length.run_backfill(store)
    assert partial_length.get_status()["last"] == last


async def test_backfill_progress_heartbeat(blank, media, tmp_path, monkeypatch, caplog):
    """A run whose reads are slow logs its progress every ``_LOG_EVERY_S``
    seconds although no ``_LOG_EVERY_N`` boundary was crossed."""
    import logging
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    lib = _mp3_lib(tmp_path, media, 2)
    src, root = _Disk(lib), "smb://mem/heartbeat"
    register_source(root, src)
    real_partial = src.read_partial

    def slow(path, n, *, lane="scan"):
        time.sleep(0.3)
        return real_partial(path, n, lane=lane)
    try:
        await _old_library(store, monkeypatch, src, root)
        src.read_partial = slow
        monkeypatch.setattr(partial_length, "_LOG_EVERY_N", 10 ** 6)
        monkeypatch.setattr(partial_length, "_LOG_EVERY_S", 0.1)
        caplog.set_level(logging.INFO, logger="soniqboom.core.partial_length")
        res = await partial_length.run_backfill(store)
    finally:
        remove_source(root)
    assert res["fixed"] == 2, res
    beats = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("Length backfill of smb://mem/heartbeat: 0 of 2 checked")]
    assert beats, [r.getMessage() for r in caplog.records]


async def test_backfill_status_while_waiting_for_a_scan(blank, monkeypatch, caplog):
    import logging
    calls = []

    def scanning():
        calls.append(partial_length.get_status()["waiting_for_scan"])
        return len(calls) < 3
    monkeypatch.setattr(scanner, "is_scanning", scanning)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(partial_length.asyncio, "sleep", lambda s: real_sleep(0))
    caplog.set_level(logging.INFO, logger="soniqboom.core.partial_length")
    await partial_length._wait_for_scans()
    assert calls == [False, True, True]
    assert partial_length.get_status()["waiting_for_scan"] is False
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == ["Length backfill: waiting for the running scan to finish"], msgs


def test_display_root_drops_credentials():
    assert partial_length.display_root("ftp://u:p@w@h:2121/a/b") == "ftp://h:2121/a/b"
    assert partial_length.display_root("smb://user@nas/share") == "smb://nas/share"
    assert partial_length.display_root("ftp://h/x") == "ftp://h/x"


def test_concurrency_follows_the_ftp_scan_budget(monkeypatch):
    from soniqboom.core import filesource
    from soniqboom.core.filesource import FTPFileSource

    class _Pool:
        _max_size = 9
    src = FTPFileSource("192.0.2.7", port=2121)
    monkeypatch.setattr(FTPFileSource, "_pool", property(lambda self: _Pool))
    conf: dict = {}
    monkeypatch.setattr("soniqboom.config.load_local_conf", lambda: conf)
    assert filesource.ftp_lane_budgets("192.0.2.7", 2121) == (
        filesource._FTP_POOL_SCAN_DEFAULT, filesource._FTP_POOL_STREAM_DEFAULT,
        filesource._FTP_POOL_BROWSE_DEFAULT)
    conf["ftp_pools"] = {"192.0.2.7:2121": {"scan": 6, "stream": 2, "browse": 1}}
    assert partial_length._concurrency(src) == 6
    conf["ftp_pools"]["192.0.2.7:2121"]["scan"] = 3
    assert partial_length._concurrency(src) == 3
    conf["ftp_pools"]["192.0.2.7:2121"]["scan"] = 12                 # capped
    _Pool._max_size = 15
    assert partial_length._concurrency(src) == partial_length._MAX_CONCURRENCY == 6
    _Pool._max_size = 4                       # clamped to a detected server cap
    assert partial_length._concurrency(src) == 2                     # 4 − stream 2
    _Pool._max_size = 2
    assert partial_length._concurrency(src) == 1
    assert partial_length._concurrency(_Disk(Path("/nonexistent"))) == partial_length._CONCURRENCY


async def test_ftp_backfill_reads_at_the_scan_budget(blank, media, tmp_path, monkeypatch):
    """Over the test FTP server, with the default budgets, the backfill keeps
    ``_concurrency`` reads going at once (6, was 2) and fixes every file."""
    store = blank
    lib = _mp3_lib(tmp_path, media, 9)
    sh = _Ftp(lib)
    inflight = [0, 0]
    lock = __import__("threading").Lock()
    real_partial = type(sh.src).read_partial

    def counted(self, path, n, *, lane="scan"):
        with lock:
            inflight[0] += 1
            inflight[1] = max(inflight[1], inflight[0])
        try:
            time.sleep(0.15)
            return real_partial(self, path, n, lane=lane)
        finally:
            with lock:
                inflight[0] -= 1
    try:
        await _old_library(store, monkeypatch, sh.src, sh.root)
        conc = partial_length._concurrency(sh.src)
        assert conc == min(6, partial_length._MAX_CONCURRENCY)
        monkeypatch.setattr(type(sh.src), "read_partial", counted)
        res = await partial_length.run_backfill(store)
        peak_conns = sh.srv.peak_conns
    finally:
        sh.stop()
    assert res["fixed"] == 9 and res["unreachable"] == 0, res
    assert inflight[1] == conc, inflight
    assert peak_conns <= 9                    # within the pool (scan 6 + stream 2 + browse 1)
    for name, t in _by_name(store).items():
        assert abs(t["duration"] - _real(media["cbr.mp3"])) < 1.0, name


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from soniqboom.api import admin
    from soniqboom.core import users
    ustore = users.UserStore(tmp_path)
    monkeypatch.setattr(users, "_instance", ustore)
    toks = {}
    for name, role in (("adm1", "admin"), ("ed1", "edit"), ("ro1", "readonly")):
        u = ustore.create(name, "Password123!x", role)
        toks[role] = ustore.issue_session(u.id)[0]
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    return TestClient(app), toks


def test_status_endpoint_for_admins_only(admin_client, monkeypatch):
    client, toks = admin_client
    st = partial_length._idle_status()
    st.update(running=True, root="ftp://nas/music", label="Music")
    st["current"].update(total=18400, checked=1200, fixed=1150)
    monkeypatch.setattr(partial_length, "_status", st)
    url = "/api/admin/length-backfill/status"
    assert client.get(url).status_code == 401
    client.cookies.set("sb_session", "not-a-session")
    assert client.get(url).status_code == 401
    for role in ("edit", "readonly"):
        client.cookies.set("sb_session", toks[role])
        assert client.get(url).status_code == 403
    client.cookies.set("sb_session", toks["admin"])
    r = client.get(url)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["running"] is True and got["root"] == "ftp://nas/music" and got["label"] == "Music"
    assert got["current"] == {"total": 18400, "checked": 1200, "fixed": 1150,
                              "skipped": 0, "unreachable": 0}
    assert got["last"] is None and got["waiting_for_scan"] is False


async def test_backfill_status_over_several_roots(blank, media, tmp_path, monkeypatch):
    """A run over a done root, a not-connected one and a read one: ``overall``
    counts the read root only, every root ends up in ``roots_done``, and
    ``last`` names the deferral."""
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    libs = {k: _mp3_lib(tmp_path / k, media, n) for k, n in (("a", 2), ("b", 3), ("c", 1))}
    roots = {k: f"smb://mem/{k}" for k in libs}
    srcs = {k: _Disk(lib) for k, lib in libs.items()}
    for k in libs:
        register_source(roots[k], srcs[k])
        await _old_library(store, monkeypatch, srcs[k], roots[k])
    partial_length.mark_done(_root_hash(roots["c"]), store)          # done already
    remove_source(roots["b"])                                        # not connected
    try:
        res = await partial_length.run_backfill(store)
    finally:
        for k in ("a", "c"):
            remove_source(roots[k])
    assert res["fixed"] == 2 and res["deferred_roots"] == 1, res
    st = partial_length.get_status()
    assert st["running"] is False and st["roots_pending"] == 0
    assert st["roots_done"] == 2                    # a (read) + b (deferred); c was skipped up front
    assert st["overall"] == {"total": 2, "checked": 2, "fixed": 2, "skipped": 0, "unreachable": 0}
    last = st["last"]
    assert (last["roots"], last["deferred_roots"], last["label"]) == (2, 1, None), last


async def test_a_failing_run_leaves_the_status_not_running(blank, media, tmp_path, monkeypatch):
    from soniqboom.core.filesource import register_source, remove_source
    store = blank
    lib = _mp3_lib(tmp_path, media, 2)
    src, root = _Disk(lib), "smb://mem/boom"
    register_source(root, src)
    try:
        await _old_library(store, monkeypatch, src, root)

        def boom(items):
            raise RuntimeError("write failed")
        monkeypatch.setattr(store, "update_track_fields_batch", boom)
        with pytest.raises(RuntimeError):
            await partial_length.run_backfill(store)
    finally:
        remove_source(root)
    st = partial_length.get_status()
    assert st["running"] is False and st["root"] is None
    assert st["last"]["checked"] == 2 and st["last"]["fixed"] == 0
    assert not [t for t in asyncio.all_tasks() if "heartbeat" in repr(t.get_coro())]


async def test_the_give_up_threshold_grows_with_the_concurrency(blank, media, tmp_path, monkeypatch):
    """Consecutive failures that end a root's run: ``_MAX_FAILS_IN_A_ROW`` at
    concurrency 2, plus one per extra read at once (a burst of 6 parallel
    failures is not "the share went away")."""
    from soniqboom.core.filesource import register_source, remove_source
    assert partial_length._max_fails_in_a_row(1) == partial_length._MAX_FAILS_IN_A_ROW
    assert partial_length._max_fails_in_a_row(2) == partial_length._MAX_FAILS_IN_A_ROW
    assert partial_length._max_fails_in_a_row(6) == partial_length._MAX_FAILS_IN_A_ROW + 4
    store = blank
    lib = _mp3_lib(tmp_path, media, 14)
    src, root = _Disk(lib), "smb://mem/burst"
    register_source(root, src)
    try:
        await _old_library(store, monkeypatch, src, root)
        src.fail_partial = True
        cands = await partial_length._candidates(store, _root_hash(root))
        for conc in (2, 6):
            partial_length._settled.clear()
            n0 = len(src.reads)
            counts = await partial_length._backfill_root(store, src, cands, None, conc)
            assert counts["unreachable"] == 14, counts
            # reads started before the threshold was reached, plus at most
            # the ones already in flight then
            got, limit = len(src.reads) - n0, partial_length._max_fails_in_a_row(conc)
            assert limit <= got <= limit + conc - 1, (conc, got)
    finally:
        remove_source(root)


async def test_the_candidate_sweep_yields_and_keeps_its_order(blank, monkeypatch):
    """The candidates of a big share are looked at a slice at a time (the
    loop is free between slices) and come back by track id, as one pass."""
    store = blank
    root = "smb://mem/big"
    rh = _root_hash(root)
    store.upsert_tracks_batch([
        {"id": f"t{(i * 7919) % 10007:05d}", "path": f"{root}:/m/{i}.mp3", "scan_root_hash": rh,
         "file_size": 50_000_000, "duration": 10.0, "bitrate": 128_000}
        for i in range(12_000)])
    want = sorted((tid, k) for tid in store.get_track_ids_for_scan_root(rh)
                  if (k := partial_length.backfill_kind(store.get_track(tid))))
    assert want                                              # (some are candidates)
    monkeypatch.setattr(partial_length, "_CANDIDATE_SLICE", 1_000)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1
    t = asyncio.ensure_future(tick())
    try:
        got = await partial_length._candidates(store, rh)
    finally:
        t.cancel()
    assert got == want
    assert ticks >= 10                                       # the loop ran between slices

