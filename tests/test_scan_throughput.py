# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan throughput without a change in what is indexed.

* An ``.m4a``'s codec comes from mutagen's sample entry ("alac",
  "mp4a.40.2" …); ffprobe (a ~20 ms process per file) is asked only for a
  bare "mp4a" entry.
* In a scan worker a tracker module's duration comes from libopenmpt
  in-process (``openmpt_vu.module_duration``: what ``openmpt123 --info``
  prints, to the millisecond) from the bytes the header parse read, which
  also give the scene MD5; in the server's own threads (and without the
  library) from the CLI, its "mm:ss.mmm" read in full — the old parse kept
  whole seconds.
* Extraction jobs carry batches of files (``_extract_batch``) whose
  results are plain dicts; the store rows built from them
  (``_scan_row``) equal the ``Track``-validated rows of before.
* A scan worker reads each inner archive of a nested zip once
  (``_NestedZips``), not once per member.
* The unchanged-file check stats in threads where stats are slow, and a
  chunk is sent only its own entries (``_compute_incremental(by_path)``)."""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import struct
import subprocess
import uuid
import wave
import zipfile
from pathlib import Path

import pytest

from soniqboom.core import metadata, openmpt_vu, scanner
from soniqboom.models.track import Track

REPO = Path(__file__).resolve().parents[1]
SAMPLES = REPO / "internal" / "format-samples"         # private (gitignored): skipped when absent


def _wav(path: Path, freq: float = 440.0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 8000)))
                               for i in range(4000)))
    return path


def _mod(path: Path, rows: int = 64, tail: bytes = b"") -> Path:
    """A minimal 4-channel ProTracker module: one pattern of ``rows`` rows
    (the rest empty), a one-sample instrument, a note on the first row — at
    the default speed it plays 64 × 6 × 20 ms = 7.68 s."""
    head = b"tiny test song".ljust(20, b"\x00")
    smp = b"lead".ljust(22, b"\x00") + struct.pack(">HBBHH", 16, 0, 64, 0, 1)
    head += smp + (b"\x00" * 30) * 30
    head += bytes([1, 127]) + bytes([0]) + b"\x00" * 127 + b"M.K."
    pat = bytearray(1024)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, 0x00])          # sample 1, period 428 (C-3)
    if rows < 64:
        pat[(rows - 1) * 16 + 2] = 0x0D                 # pattern break → ``rows`` rows
    data = bytes(head) + bytes(pat) + bytes(32) + tail
    path.write_bytes(data)
    return path


def _cli_duration(path: Path) -> "float | None":
    out = subprocess.run(["openmpt123", "--info", str(path)], capture_output=True,
                         text=True, timeout=30).stdout
    m = re.search(r"Duration\.*:\s*(?:(\d+):)?(\d+):(\d+\.\d+)", out)
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


_HAVE_LIB = openmpt_vu.is_available()
_HAVE_CLI = shutil.which("openmpt123") is not None
_HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _no_process(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError(f"started a process: {a[0] if a else k}")
    monkeypatch.setattr(metadata.forksafe, "run", refuse)


# ── m4a ───────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _HAVE_FFMPEG, reason="needs ffmpeg")
@pytest.mark.parametrize("codec,label", [("alac", "ALAC"), ("aac", "AAC")])
def test_an_m4a_codec_is_read_without_ffprobe(tmp_path, monkeypatch, codec, label):
    f = tmp_path / f"t.{codec}.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", codec, str(f)], check=True, timeout=60)
    probe = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "a:0", "-show_entries",
                            "stream=codec_name", "-of", "default=nw=1:nk=1", str(f)],
                           capture_output=True, text=True, timeout=30).stdout.strip()
    assert probe == codec                       # what the old ffprobe call read
    _no_process(monkeypatch)
    assert metadata.extract(f, "x").format == label


@pytest.mark.skipif(not (SAMPLES / "mainstream").is_dir(), reason="needs internal/format-samples")
@pytest.mark.parametrize("name,label", [("sample-alac.m4a", "ALAC"), ("sample-aac.m4a", "AAC")])
def test_real_m4a_samples_keep_their_labels(monkeypatch, name, label):
    _no_process(monkeypatch)
    assert metadata.extract(SAMPLES / "mainstream" / name, "x").format == label


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="needs ffmpeg")
@pytest.mark.parametrize("answer,label", [("alac", "ALAC"), ("aac", "AAC"), ("", "AAC")])
def test_a_bare_mp4a_entry_asks_ffprobe(tmp_path, monkeypatch, answer, label):
    f = tmp_path / "t.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "aac", str(f)], check=True, timeout=60)
    real_mp4 = metadata.MP4

    def ambiguous(p):
        a = real_mp4(p)
        a.info.codec = "mp4a"                   # an entry mutagen couldn't read further
        return a
    monkeypatch.setattr(metadata, "MP4", ambiguous)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=answer + "\n", stderr="")
    monkeypatch.setattr(metadata.forksafe, "run", fake_run)
    assert metadata.extract(f, "x").format == label
    assert calls and calls[0][0] == "ffprobe"


# ── tracker modules ───────────────────────────────────────────────────────────

@pytest.mark.skipif(not (_HAVE_LIB and _HAVE_CLI), reason="needs libopenmpt and openmpt123")
def test_a_module_duration_matches_the_cli_to_the_millisecond(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "_LIBOPENMPT_IN_PROCESS", True)     # as in a scan worker
    mods = [_mod(tmp_path / "a.mod"), _mod(tmp_path / "b.mod", rows=21, tail=b"junk")]
    if SAMPLES.is_dir():
        for ext in (".xm", ".it", ".s3m", ".ult", ".669", ".mtm"):
            mods += sorted(SAMPLES.rglob(f"*{ext}"))[:2]
    cli = {m: _cli_duration(m) for m in mods}
    _no_process(monkeypatch)
    checked = 0
    for m in mods:
        got = metadata.extract(m, "x").duration
        if cli[m] is None:
            assert got == 0.0
            continue
        assert abs(got - cli[m]) < 0.0015, (m, got, cli[m])     # the CLI prints ms, truncated
        assert got != int(got) or cli[m] == int(cli[m])          # fractions kept (30.72 s, not 30)
        checked += 1
    assert checked >= 2
    assert abs(metadata.extract(mods[0], "x").duration - 7.68) < 0.002


@pytest.mark.skipif(not _HAVE_LIB, reason="needs libopenmpt")
def test_a_module_libopenmpt_cannot_open_has_no_duration_and_logs_nothing(tmp_path, monkeypatch, capfd):
    monkeypatch.setattr(metadata, "_LIBOPENMPT_IN_PROCESS", True)
    f = tmp_path / "noise.mod"
    f.write_bytes(os.urandom(3000))
    _no_process(monkeypatch)
    assert metadata.extract(f, "x").duration == 0.0
    assert "openmpt" not in capfd.readouterr().err


@pytest.mark.parametrize("in_worker", [True, False])
@pytest.mark.parametrize("printed,seconds", [("01:02.500", 62.5), ("00:30.720", 30.72),
                                             ("1:02:03.456", 3723.456)])
def test_the_cli_gives_the_duration_in_the_server_and_without_the_library(
        tmp_path, monkeypatch, in_worker, printed, seconds):
    f = _mod(tmp_path / "a.mod")
    monkeypatch.setattr(metadata, "_LIBOPENMPT_IN_PROCESS", in_worker)
    if in_worker:
        monkeypatch.setattr(openmpt_vu, "module_duration", lambda data: None)    # no library
    else:
        def never(data):
            raise AssertionError("libopenmpt in the server process")
        monkeypatch.setattr(openmpt_vu, "module_duration", never)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"Duration...: {printed}\n", stderr="")
    monkeypatch.setattr(metadata.forksafe, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/openmpt123")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "openmpt123_path", "", raising=False)
    assert metadata.extract(f, "x").duration == seconds        # the fraction kept
    assert calls and calls[0][1] == "--info"


@pytest.mark.skipif(not _HAVE_CLI, reason="needs openmpt123")
def test_in_the_server_the_duration_is_the_clis_to_the_millisecond(tmp_path):
    assert metadata._LIBOPENMPT_IN_PROCESS is False
    f = _mod(tmp_path / "a.mod")
    assert metadata.extract(f, "x").duration == _cli_duration(f) == 7.68


def test_a_module_scene_md5_comes_from_the_bytes_already_read(tmp_path, monkeypatch):
    f = _mod(tmp_path / "a.mod", tail=b"x" * 100)
    want = hashlib.md5(f.read_bytes()).hexdigest()
    reads = []
    real = Path.read_bytes

    def spy(self):
        reads.append(self)
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", spy)
    assert metadata.extract(f, "x").file_md5 == want
    assert f not in reads                       # not read a second time for the MD5


# ── store rows from batches ───────────────────────────────────────────────────

def _old_row(meta, root, parent, hashes):
    """The row a scan stored before: ``_build_track`` + ``Track.model_dump()``
    less the empty embedding (``data.upsert_tracks_batch``)."""
    track, _art = scanner._build_track(meta, root, parent, hashes)
    d = track.model_dump()
    if not d.get("embedding"):
        d.pop("embedding")
    return d


def _corpus(tmp_path: Path) -> list[Path]:
    lib = tmp_path / "lib"
    lib.mkdir()
    files = [_wav(lib / "a.wav"), _mod(lib / "b.mod")]
    with zipfile.ZipFile(lib / "pack.zip", "w") as zf:
        zf.write(lib / "b.mod", "in/c.mod")
        zf.write(lib / "a.wav", "d.wav")
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.write(lib / "b.mod", "e.mod")
    with zipfile.ZipFile(lib / "outer.zip", "w") as zf:
        zf.write(inner, "inner.zip")
    files += [Path(f"{lib}/pack.zip::in/c.mod"), Path(f"{lib}/pack.zip::d.wav"),
              Path(f"{lib}/outer.zip::inner.zip::e.mod")]
    if _HAVE_FFMPEG:
        jpg = tmp_path / "c.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=1",
                        "-frames:v", "1", str(jpg)], check=True, timeout=60)
        for codec in ("aac", "alac"):
            raw = tmp_path / f"raw.{codec}.m4a"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=330:duration=1", "-c:a", codec, "-metadata", "title=T",
                            str(raw)], check=True, timeout=60)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-i", str(jpg), "-map", "0",
                            "-map", "1", "-c", "copy", "-disposition:v:0", "attached_pic",
                            str(lib / f"art.{codec}.m4a")], check=True, timeout=60)
            files.append(lib / f"art.{codec}.m4a")
    for rel in ("mainstream/sample.flac", "mainstream/sample.mp3", "mainstream/sample.opus"):
        if (SAMPLES / rel).is_file():
            shutil.copy(SAMPLES / rel, lib / Path(rel).name)
            files.append(lib / Path(rel).name)
    return files


def test_batch_rows_equal_the_validated_track_rows(tmp_path):
    files = _corpus(tmp_path)
    root = str(tmp_path / "lib")
    hashes = {root: "r" * 16}
    for p in files:
        hashes[os.path.dirname(str(p).split("::", 1)[0])] = "d" * 16
    results, _secs = scanner._extract_batch(tuple(map(str, files)), 60.0)
    assert len(results) == len(files)
    arts = 0
    for p, res in zip(files, results):
        assert isinstance(res, dict), (p, res)
        _path, meta, _sm, _lg = scanner._extract_one(p)
        parent = os.path.dirname(str(p).split("::", 1)[0])
        want = _old_row(meta, root, parent, hashes)
        res = dict(res)
        res["added_at"] = want["added_at"]                     # stamped at each extract
        got = scanner._scan_row(res, hashes[parent], hashes[root])
        assert got == want, p
        assert list(got) == list(want), p                     # same key order: same JSON
        arts += bool(want["cover_art"])
    if _HAVE_FFMPEG:
        assert arts >= 2                                       # (the m4a covers) became /api/art URLs


def test_a_batch_stops_after_its_budget_and_reports_errors(tmp_path):
    a, b, c = (_wav(tmp_path / f"{n}.wav") for n in "abc")
    results, secs = scanner._extract_batch((str(a), str(b), str(c)), 0.0)
    assert len(results) == 1 and results[0]["path"] == str(a) and secs >= 0
    missing = tmp_path / "gone.wav"
    results, _ = scanner._extract_batch((str(a), str(missing)), 60.0)
    assert isinstance(results[1], str)                         # the error text, not a raise


# ── nested zips ───────────────────────────────────────────────────────────────

def _nested(tmp_path: Path, inner_count: int = 3, members: int = 3) -> Path:
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_STORED) as zf:
        for k in range(inner_count):
            import io
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as inner:
                for i in range(members):
                    inner.writestr(f"m{i}.mod", f"inner {k} member {i} ".encode() * 50)
            zf.writestr(f"in{k}.zip", buf.getvalue())
    return outer


@pytest.fixture
def private_tmp(tmp_path, monkeypatch):
    """Temp files go to a folder of this test's own (never the shared one
    concurrent runs write to)."""
    import tempfile
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def test_nested_members_read_their_inner_archive_once(tmp_path, monkeypatch, private_tmp):
    outer = _nested(tmp_path)
    paths = [f"{outer}::in{k}.zip::m{i}.mod" for k in range(3) for i in range(3)]
    monkeypatch.setattr(scanner, "_NESTED", None)
    plain = [scanner._read_from_zip_path(p) for p in paths]   # the server's per-read path
    cache = scanner._NestedZips()
    monkeypatch.setattr(scanner, "_NESTED", cache)
    spilled = []
    real = scanner._copy_stream
    monkeypatch.setattr(scanner, "_copy_stream", lambda src, dst, **k: spilled.append(1) or real(src, dst))
    assert [scanner._read_from_zip_path(p) for p in paths] == plain
    assert len(spilled) == 3                                   # one spill per inner archive
    assert [scanner._read_from_zip_path(p) for p in paths] == plain
    assert len(spilled) == 3                                   # all cached
    assert list(private_tmp.iterdir()) == []                   # spills deleted once open
    cache.clear()
    assert not cache._open


def test_the_nested_cache_evicts_and_follows_the_outer_archive(tmp_path, monkeypatch, private_tmp):
    outer = _nested(tmp_path, inner_count=3, members=1)
    cache = scanner._NestedZips(size=1)
    paths = [f"{outer}::in{k}.zip::m0.mod" for k in range(3)]
    got = [cache.read(p.split("::")) for p in paths]
    assert len(cache._open) == 1 and got[2].startswith(b"inner 2 member 0")
    # the outer archive is rewritten: its inner archives are read again
    os.utime(outer, (os.stat(outer).st_atime, os.stat(outer).st_mtime + 10))
    spilled = []
    real = scanner._copy_stream
    monkeypatch.setattr(scanner, "_copy_stream", lambda src, dst, **k: spilled.append(1) or real(src, dst))
    assert cache.read(paths[2].split("::")) == got[2] and spilled == [1]
    with pytest.raises(KeyError):
        cache.read(f"{outer}::in9.zip::m0.mod".split("::"))
    assert list(private_tmp.iterdir()) == []
    by_bytes = scanner._NestedZips(size=10, max_bytes=1)       # a cap on what is kept spilled
    for p in paths:
        by_bytes.read(p.split("::"))
    assert len(by_bytes._open) == 1                            # (the one in use stays)
    cache.clear()
    by_bytes.clear()


def test_scan_workers_cache_nested_archives_and_the_server_does_not(monkeypatch):
    import _scan_crash
    assert scanner._NESTED is None                             # the server (and this test process)
    monkeypatch.setattr(scanner, "_pools_closed", False)
    pool = scanner._process_pool(1)
    try:
        assert pool.submit(_scan_crash.nested_cache_ready).result(timeout=60) is True
        assert pool.submit(_scan_crash.libopenmpt_in_process).result(timeout=60) is True
    finally:
        scanner._kill_pool(pool)


# ── the unchanged-file check ──────────────────────────────────────────────────

def test_slow_stats_run_in_threads(tmp_path, monkeypatch):
    paths = [str(_wav(tmp_path / f"f{i}.wav")) for i in range(3)] * 60 + [str(tmp_path / "gone")]
    fast = scanner._stat_all(paths)
    pools = []
    real_tp = scanner.ThreadPoolExecutor
    monkeypatch.setattr(scanner, "ThreadPoolExecutor", lambda *a, **k: pools.append(1) or real_tp(*a, **k))
    assert [s.st_mtime if s else None for s in scanner._stat_all(paths)] == \
        [s.st_mtime if s else None for s in fast] and not pools          # local disk: in line
    real_stat = os.stat

    def slow(p, *a, **k):
        import time
        time.sleep(0.001)
        return real_stat(p, *a, **k)
    monkeypatch.setattr(scanner.os, "stat", slow)
    slow_out = scanner._stat_all(paths)
    assert pools == [1]
    assert [s.st_size if s else None for s in slow_out] == [s.st_size if s else None for s in fast]


def test_the_check_by_path_decides_as_the_check_by_id(tmp_path):
    same = _wav(tmp_path / "same.wav")
    grown = _wav(tmp_path / "grown.wav")
    touched = _wav(tmp_path / "touched.wav")
    zf = tmp_path / "a.zip"
    with zipfile.ZipFile(zf, "w") as z:
        z.write(same, "m.wav")
    member = f"{zf}::m.wav"
    other_id = _wav(tmp_path / "other.wav")
    files = [str(same), str(grown), str(touched), member, str(other_id), str(tmp_path / "new.wav"),
             str(tmp_path / "gone.wav")]
    st = {p: os.stat(p.split("::")[0]) for p in files[:5]}
    tid = {p: str(uuid.uuid5(uuid.NAMESPACE_URL, p)) for p in files}
    stored = {
        str(same): (st[str(same)].st_mtime, st[str(same)].st_size),
        str(grown): (st[str(grown)].st_mtime, st[str(grown)].st_size - 1),
        str(touched): (st[str(touched)].st_mtime - 5, st[str(touched)].st_size),
        member: (st[member].st_mtime, 123),                    # a member stores its own size
    }
    by_id = {tid[p]: v for p, v in stored.items()}
    by_path = {p: (*v, tid[p]) for p, v in stored.items()}
    # a track at other.wav whose id isn't that path's: not this file's track
    by_path[str(other_id)] = (st[str(other_id)].st_mtime, st[str(other_id)].st_size, "someone-else")
    a = scanner._compute_incremental(files, by_id)
    b = scanner._compute_incremental(files, by_path, True)
    assert a == b
    assert a[0] == {str(same), member}
    assert a[1] == tid


def test_a_missing_libopenmpt_is_looked_for_once(monkeypatch):
    import ctypes.util
    looked = []
    monkeypatch.setattr(openmpt_vu, "_LIB", None)
    monkeypatch.setattr(openmpt_vu, "_LOAD_FAILED", False)
    monkeypatch.setattr(openmpt_vu, "_candidate_paths", lambda: ["/nonexistent/libopenmpt.so"])
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: looked.append(name) or None)
    assert openmpt_vu.module_duration(b"x" * 100) is None
    assert openmpt_vu.module_duration(b"x" * 100) is None
    assert openmpt_vu.is_available() is False
    assert looked == ["openmpt"]                               # (on Linux each look ran ldconfig)


@pytest.mark.skipif(not (_HAVE_LIB and _HAVE_CLI), reason="needs libopenmpt and openmpt123")
def test_in_process_and_cli_durations_are_the_same_number(tmp_path, monkeypatch):
    """A file re-extracted by the server (CLI) after a scan worker
    (libopenmpt) must not read as changed: the very same float."""
    mods = [_mod(tmp_path / "a.mod"), _mod(tmp_path / "b.mod", rows=21)]
    if SAMPLES.is_dir():
        mods += sorted(p for p in SAMPLES.rglob("*") if p.suffix.lower() in metadata._TRACKER_EXTS)
    for m in mods:
        monkeypatch.setattr(metadata, "_LIBOPENMPT_IN_PROCESS", True)
        a = metadata.extract(m, "x").duration
        monkeypatch.setattr(metadata, "_LIBOPENMPT_IN_PROCESS", False)
        assert metadata.extract(m, "x").duration == a, m


def test_a_lone_retry_reads_durations_with_the_cli(tmp_path, monkeypatch):
    """``isolate`` (a file that runs alone after its job's pool died): no
    libopenmpt in-process — a module that only crashes the library indexes."""
    f = _mod(tmp_path / "a.mod")
    monkeypatch.setattr(metadata, "_LIBOPENMPT_IN_PROCESS", True)

    def crash(data):
        raise AssertionError("libopenmpt in-process for a lone retry")
    monkeypatch.setattr(openmpt_vu, "module_duration", crash)
    monkeypatch.setattr(metadata.forksafe, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="Duration...: 00:07.680\n", stderr=""))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/openmpt123")
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "openmpt123_path", "", raising=False)
    _p, meta, _s, _l = scanner._extract_one(f, True)
    assert meta.duration == 7.68 and metadata._LIBOPENMPT_IN_PROCESS is True   # restored after
    results, _secs = scanner._extract_batch((str(f), str(f)), 60.0, True)
    assert [r["duration"] for r in results] == [7.68, 7.68]


def test_quick_stats_tell_gone_from_unreadable_and_never_pile_up(tmp_path, monkeypatch):
    ok = _wav(tmp_path / "ok.wav")
    real = os.stat

    def stat(p, *a, **k):
        if str(p).endswith("denied.wav"):
            raise PermissionError(13, "Permission denied")
        return real(p, *a, **k)
    monkeypatch.setattr(scanner.os, "stat", stat)
    got = scanner._stats_quick([str(ok), str(tmp_path / "gone.wav"), str(tmp_path / "denied.wav"),
                                str(ok / "x")], timeout=5)
    assert got[str(ok)][1] == os.path.getsize(ok)
    assert got[str(tmp_path / "gone.wav")] is None and got[str(ok / "x")] is None   # gone
    assert str(tmp_path / "denied.wav") not in got                                  # can't tell
    held = [scanner._QUICK_STATS.acquire(blocking=False) for _ in range(10)]
    try:
        assert scanner._stats_quick([str(ok)], timeout=5) == {}      # all blocked: no new thread
    finally:
        for h in held:
            if h:
                scanner._QUICK_STATS.release()
    assert scanner._stats_quick([str(ok)], timeout=5)
