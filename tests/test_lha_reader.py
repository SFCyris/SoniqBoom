# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The embedded LHA reader (``core.lha``) and LHA archives through the
archive layer (``core.archive``): every method (``-lh0-``, ``-lz4-``,
``-lh1-``, ``-lh4-`` … ``-lh7-``, ``-lz5-``, ``-lzs-``) at every header level
(0 – 3) reads back byte for byte — also with no ``lha`` program installed and
where ``lhafile`` turns the archive down; Amiga comments and directory
headers; archives padded or cut after their last member; damaged members;
the reader's names match ``lhafile``'s.  The archives come from
``tests/_lha_writer.py``; lhasa's ``lha t`` (when installed) checks them, so
the reader is not only checked against that writer.

Also the archive layer's member naming: Face The Music modules (``.ftm`` /
``FTM.x``, where the installed libopenmpt reads them), modules stored with no
extension (named by their bytes), FamiTracker's ``.ftm`` left out, and what
``census`` / ``describe_census`` say of an archive with nothing playable."""
from __future__ import annotations

import random
import shutil
import struct
import subprocess

import pytest

import _lha_writer as W
from soniqboom.core import archive, lha, metadata

METHODS = ["-lh0-", "-lz4-", "-lh1-", "-lh4-", "-lh5-", "-lh6-", "-lh7-", "-lz5-", "-lzs-"]
_rng = random.Random(7)
_MOTIF = bytes(_rng.randrange(256) for _ in range(300))
DATA = {
    "text": b"The quick brown fox jumps over the lazy dog. " * 120,
    "module": (b"M.K." + bytes(range(256)) * 8 + _MOTIF * 20
               + bytes(_rng.randrange(256) for _ in range(3000)) + b"\0" * 2000),
    "random": bytes(_rng.randrange(256) for _ in range(6000)),
    "runs": b"a" * 9000 + b"b" * 10 + b"ab" * 900,
    "tiny": b"x",
    "empty": b"",
}


def _members(method):
    return [{"name": f"MOD.{k}", "data": d, "method": method,
             "comment": "FTM-Modul" if k == "text" else None,
             "dirname": "songs/8voic" if k == "module" else None}
            for k, d in DATA.items()]


@pytest.fixture
def no_lhasa(monkeypatch):
    """No ``lha`` program: every archive is read in-process."""
    monkeypatch.setattr(archive, "_LHA_BIN", None)


@pytest.mark.parametrize("level", [0, 1, 2, 3])
@pytest.mark.parametrize("method", METHODS)
def test_every_method_and_header_level_reads_back(tmp_path, no_lhasa, method, level):
    mem = _members(method)
    if level == 0:
        for m in mem:
            m["dirname"] = None                     # (level 0: no directory header)
    p = tmp_path / f"t{level}{method.strip('-')}.lha"
    p.write_bytes(W.archive(mem, level=level))
    a = lha.LhaArchive(p)
    infos = a.infolist()
    assert len(infos) == len(mem)
    for info, m in zip(infos, mem):
        assert a.read(info.filename) == m["data"], (method, level, m["name"])
        assert info.compress_type == (method if m["data"] or method == "-lz4-" else "-lh0-")
        assert info.file_size == len(m["data"])
        if m["comment"]:
            assert info.comment == m["comment"]
        if m["dirname"]:
            assert info.filename.replace("\\", "/").endswith("songs/8voic/" + m["name"])
        if level in (1, 2, 3):
            assert info.os_id == "A"
    # The archive layer opens it the same way (lhafile, else the embedded reader).
    names = archive.list_members(p, strict=True)
    assert len(names) == len(mem)
    for disp in names:
        real = archive._members(p)[disp]
        want = next(m["data"] for m in mem if real.replace("\\", "/").endswith(m["name"]))
        assert archive.read_member(p, disp) == want


@pytest.mark.parametrize("method", ["-lh4-", "-lh5-", "-lh6-", "-lh7-"])
def test_a_member_of_several_huffman_blocks(tmp_path, method):
    data = bytes(random.Random(3).randrange(256) for _ in range(140_000))   # > 0xFFFF codes
    p = tmp_path / "big.lha"
    p.write_bytes(W.archive([("MOD.big", data, method)]))
    assert lha.LhaArchive(p).read("MOD.big") == data


@pytest.mark.skipif(shutil.which("lha") is None, reason="lhasa's lha not installed")
@pytest.mark.parametrize("level", [0, 1, 2, 3])
@pytest.mark.parametrize("method", METHODS)
def test_lhasa_decodes_what_the_test_writer_makes(tmp_path, method, level):
    """The fixtures are real LHA: the reference decoder (lhasa) tests every
    member's CRC — so the reader agrees with lhasa, not only with the writer."""
    p = tmp_path / "x.lha"
    p.write_bytes(W.archive(_members(method), level=level))
    r = subprocess.run(["lha", "t", str(p)], capture_output=True, text=True, timeout=60)
    out = (r.stdout + r.stderr).replace("\r", "\n")
    assert r.returncode == 0, out
    assert out.count("Tested") == len(DATA), out


@pytest.mark.skipif(not archive.HAVE_LHA, reason="lhafile not installed")
@pytest.mark.parametrize("level", [0, 1, 2])
@pytest.mark.parametrize("method", ["-lh0-", "-lh5-", "-lh6-", "-lh7-"])
def test_the_embedded_reader_names_members_as_lhafile_does(tmp_path, method, level):
    import lhafile
    mem = _members(method)
    p = tmp_path / "x.lha"
    p.write_bytes(W.archive(mem, level=level))
    a, b = lha.LhaArchive(p), lhafile.LhaFile(str(p))
    assert a.namelist() == b.namelist()
    for n in a.namelist():
        assert a.read(n) == b.read(n)


@pytest.mark.parametrize("method", ["-lh1-", "-lh4-", "-lz4-", "-lz5-", "-lzs-"])
def test_methods_lhafile_lacks_open_without_lhasa(tmp_path, no_lhasa, method):
    p = tmp_path / "Old.lha"
    tune = DATA["module"]
    p.write_bytes(W.archive([("MOD.Old", tune, method)], level=1))
    assert archive.list_members(p, strict=True) == ["MOD.Old.mod"]
    assert archive.read_member(p, "MOD.Old.mod") == tune


def test_level_3_headers_open_without_lhasa(tmp_path, no_lhasa):
    p = tmp_path / "L3.lha"
    p.write_bytes(W.archive([("MOD.Three", DATA["module"], "-lh5-")], level=3))
    assert archive.list_members(p, strict=True) == ["MOD.Three.mod"]
    assert archive.read_member(p, "MOD.Three.mod") == DATA["module"]


@pytest.mark.parametrize("end", [b"\x00", b"", b"\x1a" * 100, b"\x00" * 128, b"\x00GARBAGE" * 3],
                         ids=["end-marker", "no-end-marker", "xmodem-padding", "zero-padding",
                              "junk"])
def test_what_follows_the_last_member_doesnt_matter(tmp_path, no_lhasa, end):
    p = tmp_path / "e.lha"
    p.write_bytes(W.archive([("MOD.a", DATA["text"], "-lh5-"), ("MOD.b", DATA["runs"], "-lh5-")],
                            level=1, end=end))
    assert archive.list_members(p, strict=True) == ["MOD.a.mod", "MOD.b.mod"]
    assert archive.read_member(p, "MOD.b.mod") == DATA["runs"]


def test_a_damaged_member_fails_its_crc_and_a_cut_archive_is_refused(tmp_path):
    good = W.archive([("MOD.a", DATA["text"], "-lh5-")])
    p = tmp_path / "bad.lha"
    raw = bytearray(good)
    raw[-20] ^= 0xFF                                  # a byte of the packed data
    p.write_bytes(bytes(raw))
    with pytest.raises(lha.LhaError):
        lha.LhaArchive(p).read("MOD.a")
    p.write_bytes(good[: len(good) // 2])             # cut inside the member's data
    with pytest.raises(lha.LhaError):
        lha.LhaArchive(p)
    p.write_bytes(b"no archive at all, just text")
    with pytest.raises(lha.LhaError):
        archive.open_lha(p)


def test_read_head_decodes_only_the_start(tmp_path):
    p = tmp_path / "h.lha"
    p.write_bytes(W.archive([("MOD.a", DATA["module"], "-lh5-"), ("MOD.b", DATA["text"], "-lh1-")]))
    a = lha.LhaArchive(p)
    assert a.read_head("MOD.a", 1084) == DATA["module"][:1084]
    assert a.read_head("MOD.b", 10) == DATA["text"][:10]


def test_open_lha_falls_back_from_lhafile_to_the_embedded_reader(tmp_path, monkeypatch):
    p = tmp_path / "f.lha"
    p.write_bytes(W.archive([("MOD.a", DATA["text"], "-lh5-")]))

    class _Refusing:
        def __init__(self, *a, **k):
            raise RuntimeError("Unsupported file is contained")
    monkeypatch.setattr(archive, "HAVE_LHA", True)
    monkeypatch.setattr(archive, "_lhafile", type("M", (), {"LhaFile": _Refusing}))
    monkeypatch.setattr(archive, "_LHA_BIN", None)
    arc = archive.open_lha(p)
    assert isinstance(arc, lha.LhaArchive) and arc.read("MOD.a") == DATA["text"]


def test_a_wrapped_ym_is_read_by_the_embedded_reader_too(tmp_path, monkeypatch):
    """YM files are LHA-wrapped: their payload magic is read by the same
    readers (an archive lhafile refuses — here: no end marker — included)."""
    payload = b"YM5!LeOnArD!" + b"\0" * 400
    p = tmp_path / "tune.ym"
    p.write_bytes(W.archive([("tune.bin", payload, "-lh5-")], end=b""))
    monkeypatch.setattr(archive, "_LHA_BIN", None)
    assert metadata._ym_payload_magic(p) == b"YM5!"


# ── Member naming ───────────────────────────────────────────────────────────

def _ftm(title=b"Antom", artist=b"SSilk") -> bytes:
    wave = bytes(([100] * 8 + [156] * 8) * 256)
    h = (b"FTMN" + bytes([3, 1]) + struct.pack(">HH", 4, 0x2000) + bytes([0, 0xFF, 63, 1, 4, 24])
         + title.ljust(32, b"\0") + artist.ljust(32, b"\0") + bytes([0, 0]))
    ev = bytes([0xA0, 0x4D, 0xF0, 0x03]) * 16
    chans = struct.pack(">hI", 0, len(ev)) + ev + b"".join(struct.pack(">hI", 0, 0)
                                                           for _ in range(7))
    return (h + b"Inst".ljust(30, b"\0") + b"\0\0" + chans
            + struct.pack(">HH", 0, len(wave) // 2) + wave)


def _mod(title=b"t") -> bytes:
    return title.ljust(20, b"\0") + b"\0" * 1060 + b"M.K." + b"\0" * 2048


needs_ftm = pytest.mark.skipif(".ftm" not in getattr(metadata, "OPENMPT_EXTRA_EXTS", {".ftm"}),
                               reason="the installed libopenmpt doesn't read Face The Music")


@needs_ftm
def test_face_the_music_members_are_listed(tmp_path):
    p = tmp_path / "8voic.lha"
    p.write_bytes(W.archive([("Antom.FTM", _ftm(), "-lh5-"), ("FTM.Other", _ftm(b"O"), "-lh5-"),
                             ("Antom.readme", b"Short: FTM-Modul", "-lh5-")]))
    assert archive.list_members(p, strict=True) == ["Antom.FTM", "FTM.Other.ftm"]
    assert archive.read_member(p, "FTM.Other.ftm") == _ftm(b"O")


def test_a_famitracker_ftm_is_not_listed_where_its_bytes_can_be_read(tmp_path):
    p = tmp_path / "nes.lha"
    p.write_bytes(W.archive([("tune.ftm", b"FamiTracker Module" + b"\0" * 200, "-lh5-")]))
    assert archive.list_members(p, strict=True) == []


@needs_ftm
def test_modules_without_an_extension_are_named_by_their_bytes(tmp_path):
    """``Antom`` (a Face The Music module) → ``Antom.ftm``; ``Song`` (a
    ProTracker module) is NOT renamed over the ``Song.mod`` beside it; a
    program or a text with no extension stays unlisted."""
    p = tmp_path / "plain.lha"
    p.write_bytes(W.archive([("Antom", _ftm(), "-lh5-"), ("Song.mod", _mod(b"a"), "-lh5-"),
                             ("Song", _mod(b"b"), "-lh5-"), ("Other", _mod(b"c"), "-lh1-"),
                             ("Player", b"\x00\x00\x03\xf3" + b"\0" * 64, "-lh5-"),
                             ("ReadMe", b"just text", "-lh0-")]))
    names = archive.list_members(p, strict=True)
    assert names == ["Song.mod", "Antom.ftm", "Other.mod"]
    assert archive.read_member(p, "Antom.ftm") == _ftm()
    assert archive.read_member(p, "Song.mod") == _mod(b"a")
    assert archive.read_member(p, "Other.mod") == _mod(b"c")


@needs_ftm
def test_a_zip_scan_lists_a_module_with_no_extension(tmp_path):
    import zipfile
    from soniqboom.core import scanner
    d = tmp_path / "lib"
    d.mkdir()
    with zipfile.ZipFile(d / "pack.zip", "w") as zf:
        zf.writestr("Antom", _ftm())
        zf.writestr("readme", b"text")
    found, _errs = scanner._find_audio_files([str(d)], True)
    files = [f for fs in found.values() for f in fs]
    assert [str(f).rsplit("/", 1)[-1] for f in files] == ["pack.zip::Antom.ftm"]
    data, _name = scanner._read_from_zip_path(str(d / "pack.zip") + "::Antom.ftm")
    assert data == _ftm()


@needs_ftm
def test_face_the_music_metadata(tmp_path):
    p = tmp_path / "Antom.ftm"
    p.write_bytes(_ftm())
    m = metadata.extract(p, "id1")
    assert m.format == "Face The Music" and m.title == "Antom" and m.artist == "SSilk"
    assert m.duration > 0                                # (libopenmpt measured it)


@needs_ftm
def test_a_loose_famitracker_ftm_is_not_indexed(tmp_path):
    p = tmp_path / "nes.ftm"
    p.write_bytes(b"FamiTracker Module" + b"\0" * 200)
    with pytest.raises(ValueError):
        metadata.extract(p, "id2")


@pytest.mark.parametrize("names,heads,says", [
    (["Revolutions", "Revolutions.info", "file_id.diz"], {"Revolutions": b"\x00\x00\x03\xf3"},
     "an Amiga program"),
    (["DISK.EXE", "readme.txt"], {"DISK.EXE": b"MZ"}, "a PC program"),
    (["disk.dms"], {}, "an Amiga disk image in DMS format, which SoniqBoom doesn't open"),
    (["disk.lzx"], {}, "an LZX archive, which SoniqBoom doesn't open"),
    (["a.txt", "b.iff", "c.info"], {}, "only text and pictures"),
    (["x.bin"], {"x.bin": b"FamiTracker Module"}, "a FamiTracker module, a format SoniqBoom doesn't play"),
    (["x.bin"], {}, "no file in a format SoniqBoom plays"),
])
def test_what_an_archive_with_nothing_playable_holds(names, heads, says):
    c = archive.census(names, lambda r: heads.get(r, b""))
    assert says in archive.describe_census(c)


def _ftm_without_samples() -> bytes:
    raw = bytearray(_ftm())
    raw[13] &= 0xFE                                    # "module with samples" flag off
    return bytes(raw)


@needs_ftm
def test_a_face_the_music_module_without_its_samples_is_not_listed(tmp_path):
    """libopenmpt loads only a module saved with its samples: one whose
    samples are separate files would be listed and fail to play."""
    p = tmp_path / "nosmp.lha"
    p.write_bytes(W.archive([("Song.ftm", _ftm_without_samples(), "-lh5-"),
                             ("Plain", _ftm_without_samples(), "-lh5-")]))
    assert archive.list_members(p, strict=True) == []
    c = archive.census(lha.LhaArchive(p).namelist(),
                       lambda r: lha.LhaArchive(p).read_head(r, 64))
    assert "2 Face The Music modules saved without their samples" in archive.describe_census(c)
    loose = tmp_path / "Song.ftm"
    loose.write_bytes(_ftm_without_samples())
    with pytest.raises(ValueError):
        metadata.extract(loose, "id3")


def test_a_hostile_member_cant_make_the_reader_decode_past_its_data(tmp_path):
    """A header claiming 1 GB for a few bytes of -lh5- data: the reader stops
    where the data ends (an error), it doesn't decode zeros up to 1 GB."""
    import time
    packed = W.lzh_compress(b"x" * 50, "-lh5-")
    raw = bytearray(W.member("MOD.big", b"x" * 50, "-lh5-", packed=packed))
    struct.pack_into("<I", raw, 11, 1 << 30)            # original size: 1 GB
    raw[1] = sum(raw[2:raw[0] + 2]) & 0xFF              # (header checksum)
    p = tmp_path / "bomb.lha"
    p.write_bytes(bytes(raw) + b"\0")
    t0 = time.monotonic()
    with pytest.raises(lha.LhaError):
        lha.LhaArchive(p).read("MOD.big")
    assert time.monotonic() - t0 < 5


def test_a_huffman_table_longer_than_16_bits_is_refused():
    with pytest.raises(lha.LhaError):
        lha._Table([17, 1])


# ── Hostile / malformed archives (QA review B1, M1) ─────────────────────────

class _BitW:
    def __init__(self):
        self.v = self.n = 0
        self.out = bytearray()

    def put(self, val, nb):
        for i in range(nb - 1, -1, -1):
            self.v = (self.v << 1) | ((val >> i) & 1)
            self.n += 1
            if self.n == 8:
                self.out.append(self.v)
                self.v = self.n = 0

    def flush(self):
        while self.n:
            self.put(0, 1)
        return bytes(self.out)


def _const_table_bomb(blocks: int) -> bytes:
    """An -lh5- stream of one-symbol tables: a literal, then ``blocks`` blocks
    of 65,535 matches of 256 bytes that read no bit at all."""
    bw = _BitW()

    def block(n, c):
        bw.put(n, 16)
        bw.put(0, 5); bw.put(0, 5)                     # length-of-length table: constant
        bw.put(0, 9); bw.put(c, 9)                     # literal/length table: constant c
        bw.put(0, 4); bw.put(0, 4)                     # position table: constant 0
    block(1, 65)
    for _ in range(blocks):
        block(65535, 509)
    return bw.flush()


def _run_capped(fn, seconds=5.0):
    """``fn()`` in a daemon thread; its exception, or a failure when it is
    still running after ``seconds``."""
    import threading
    box = {}

    def go():
        try:
            fn()
        except BaseException as exc:                  # noqa: BLE001
            box["exc"] = exc
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "still running (an endless loop)"
    return box.get("exc")


@pytest.mark.parametrize("level,total", [(3, 0), (3, 31), (2, 0), (2, 25)])
def test_a_header_whose_size_doesnt_cover_itself_is_refused_at_once(tmp_path, no_lhasa, level, total):
    if level == 3:
        h = (struct.pack("<H", 4) + b"-lh0-" + struct.pack("<II", 0, 0) + b"\0\0\0\0" + b"\x20\x03"
             + struct.pack("<H", 0) + b"A" + struct.pack("<II", total, 0))
    else:
        h = (struct.pack("<H", total) + b"-lh0-" + struct.pack("<II", 0, 0) + b"\0\0\0\0"
             + b"\x20\x02" + struct.pack("<H", 0) + b"A" + struct.pack("<H", 0))
    p = tmp_path / "loop.lha"
    p.write_bytes(h + b"\0")
    exc = _run_capped(lambda: lha.LhaArchive(p))
    assert isinstance(exc, lha.LhaError), exc
    exc = _run_capped(lambda: archive.list_members(p, strict=True))
    assert exc is not None


def test_a_member_count_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(lha, "_MAX_MEMBERS", 50)
    p = tmp_path / "many.lha"
    p.write_bytes(W.archive([(f"f{i}", b"", "-lh0-") for i in range(51)]))
    with pytest.raises(lha.LhaError):
        lha.LhaArchive(p)


def test_a_bomb_claiming_more_than_its_data_can_hold_is_refused(tmp_path, no_lhasa):
    """85 bytes claiming 201 MB: refused when the archive is opened — by the
    embedded reader and on the lhafile path alike — nothing is decoded."""
    packed = _const_table_bomb(12)
    raw = bytearray(W.member("bomb.mod", b"", "-lh5-", packed=packed))
    # (``member`` stored it as -lh0- of 0 bytes: make it the -lh5- bomb)
    raw[2:7] = b"-lh5-"
    struct.pack_into("<II", raw, 7, len(packed), 201 * 1024 * 1024)
    raw[1] = sum(raw[2:raw[0] + 2]) & 0xFF
    p = tmp_path / "bomb.lha"
    p.write_bytes(bytes(raw) + b"\0")
    exc = _run_capped(lambda: lha.LhaArchive(p))
    assert isinstance(exc, lha.LhaError)
    exc = _run_capped(lambda: archive.open_lha(p))
    assert isinstance(exc, lha.LhaError)


def test_a_block_that_reads_no_bit_stops_the_decode(tmp_path):
    """Under the size cap (60 MB claimed): one-symbol tables in block after
    block — the second such block ends the decode, not 60 MB of output."""
    packed = _const_table_bomb(8)
    raw = bytearray(W.member("bomb.mod", b"", "-lh5-", packed=packed))
    raw[2:7] = b"-lh5-"
    struct.pack_into("<II", raw, 7, len(packed), 60 * 1024 * 1024)
    raw[1] = sum(raw[2:raw[0] + 2]) & 0xFF
    p = tmp_path / "bomb2.lha"
    p.write_bytes(bytes(raw) + b"\0")
    a = lha.LhaArchive(p)
    exc = _run_capped(lambda: a.read("bomb.mod"), seconds=20)
    assert isinstance(exc, lha.LhaError) and "block without data" in str(exc)


def test_a_method_the_reader_lacks_is_refused_not_listed_short(tmp_path, no_lhasa):
    p = tmp_path / "pm.lha"
    good = W.member("MOD.a", DATA["module"], "-lh5-")
    pm = bytearray(W.member("MOD.b", DATA["text"], "-lh0-"))
    pm[2:7] = b"-pm2-"
    pm[1] = sum(pm[2:pm[0] + 2]) & 0xFF
    p.write_bytes(good + bytes(pm) + b"\0")
    with pytest.raises(lha.LhaUnsupported) as ei:
        archive.list_members(p, strict=True)
    assert ei.value.method == "-pm2-"


def test_decoding_runs_outside_the_archive_lock(tmp_path, no_lhasa, monkeypatch):
    """A slow pure-Python decode doesn't hold up every other archive read."""
    import threading
    import zipfile
    p = tmp_path / "slow.lha"
    p.write_bytes(W.archive([("MOD.a", DATA["module"], "-lh1-")]))
    archive.list_members(p, strict=True)              # (open + map cached)
    gate, entered = threading.Event(), threading.Event()
    real_read = lha.LhaArchive.read

    def slow(self, name):
        entered.set()
        gate.wait(10)
        return real_read(self, name)
    monkeypatch.setattr(lha.LhaArchive, "read", slow)
    t = threading.Thread(target=lambda: archive.read_member(p, "MOD.a.mod"), daemon=True)
    t.start()
    assert entered.wait(5)
    z = tmp_path / "other.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("x.mod", b"\0" * 1080 + b"M.K." + b"\0" * 100)
    exc = _run_capped(lambda: archive.list_members(z), seconds=3)
    gate.set()
    t.join(5)
    assert exc is None


# ── Face The Music without samples, under any reading (QA review M2) ─────────

@needs_ftm
@pytest.mark.parametrize("name", ["FTM.NoSmp", "FTM.One", "NoSmp.ftm", "Plain"])
def test_a_face_the_music_module_without_samples_is_not_listed_under_any_name(tmp_path, name):
    p = tmp_path / "x.lha"
    p.write_bytes(W.archive([(name, _ftm_without_samples(), "-lh5-")]))
    assert archive.list_members(p, strict=True) == []


@needs_ftm
@pytest.mark.parametrize("with_samples", [True, False])
def test_an_aminet_ftm_upload_lists_its_module_only(tmp_path, with_samples):
    """The layout of Aminet's mods/8voic uploads (Jonahara.lha): an icon text
    ``.DisplayMe``, the Amiga player program ``PlayFTM``, the module with no
    extension and its readme — only the module is a tune, and only when it
    was saved with its samples."""
    tune = _ftm(b"Jonahara") if with_samples else _ftm_without_samples()
    p = tmp_path / "Jonahara.lha"
    p.write_bytes(W.archive([(".DisplayMe", b"\x9b0;33;40mJonahara\n" * 30, "-lh5-"),
                             ("PlayFTM", b"\x00\x00\x03\xf3" + bytes(range(256)) * 50, "-lh5-"),
                             ("Jonahara", tune, "-lh5-"),
                             ("Jonahara.readme", b"Short: FTM Tune by \"SSilk\".\n", "-lh5-")]))
    assert archive.list_members(p, strict=True) == (["Jonahara.ftm"] if with_samples else [])
    if with_samples:
        assert archive.read_member(p, "Jonahara.ftm") == tune


@needs_ftm
def test_a_zip_scan_leaves_out_ftm_without_samples_under_a_uade_suffix(tmp_path):
    import zipfile
    from soniqboom.core import scanner
    d = tmp_path / "lib"
    d.mkdir()
    with zipfile.ZipFile(d / "pack.zip", "w") as zf:
        zf.writestr("FTM.One", _ftm_without_samples())
        zf.writestr("FTM.Two", _ftm())
    found, _errs = scanner._find_audio_files([str(d)], True)
    names = sorted(str(f).rsplit("::", 1)[-1] for fs in found.values() for f in fs)
    assert "FTM.One" not in names and all(not n.startswith("FTM.One") for n in names)


# ── What an archive holds: the words (QA review minor 3) ────────────────────

def test_census_words_plurals_and_articles():
    nos = _ftm_without_samples()[:64]
    say = lambda names, heads=None, **kw: archive.describe_census(   # noqa: E731
        archive.census(names, lambda r: (heads or {}).get(r, b""), **kw))
    s = say(["A.ftm", "B.ftm"], {"A.ftm": nos, "B.ftm": nos})
    assert "2 Face The Music modules saved without their samples, which libopenmpt doesn't load" in s
    assert "sampless" not in s and "format this server doesn't play" not in s
    assert "2 Amiga disk images with no tune file SoniqBoom reads on them" in say(
        ["a.adf", "b.adf"], opened={"a.adf", "b.adf"})
    assert "an Amiga disk image SoniqBoom didn't open" in say(["a.adf"])
    assert "an xz archive" in say(["x.xz"])
    assert "a ZIP archive SoniqBoom didn't open" in say(["in.zip"])
    assert "a ZIP archive with no tunes in it" in say(["in.zip"], opened={"in.zip"})
    both = say(["Prog", "t.ftm"], {"Prog": b"\x00\x00\x03\xf3", "t.ftm": nos})
    assert "not tune files" not in both and "an Amiga program and a Face The Music" in both
    assert archive.describe_census({"files": 0, "unsupported_method": "-pm2-"}) == (
        "it is packed with a method SoniqBoom doesn't unpack (-pm2-)")


def test_census_tells_whether_an_archive_could_now_hold_a_tune(monkeypatch):
    prog = archive.census(["Player", "Player.info"], lambda r: b"\x00\x00\x03\xf3")
    assert not archive.could_hold_a_tune(prog)
    ftm = archive.census(["Antom"], lambda r: _ftm()[:archive.HEAD_BYTES])
    if ".ftm" in archive._EXT_SET:
        assert archive.could_hold_a_tune(ftm)
    monkeypatch.setattr(archive, "_EXT_SET", archive._EXT_SET - {".ftm"})
    assert not archive.could_hold_a_tune(ftm)
    assert archive.could_hold_a_tune(archive.census(["in.zip"]))      # (not opened: unknown)


# ── lhasa's names → the built-in reader's (QA review minor 5) ────────────────

def test_a_member_lhasa_listed_hands_its_user_data_to_its_new_name():
    import uuid
    from soniqboom.core import scanner
    arc = "/music/old.lha"
    old_path, new_path = f"{arc}::DIR/song.mod", f"{arc}::DIR\\song.mod"
    old_id = str(uuid.uuid5(uuid.NAMESPACE_URL, old_path))
    new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, new_path))

    class _Store:
        _tracks = {old_id: {"path": old_path}, new_id: {"path": new_path}}
    assert scanner._renamed_member_ids([old_id], _Store()) == {old_id: new_id}
