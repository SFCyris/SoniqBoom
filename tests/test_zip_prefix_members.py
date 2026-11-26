# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Members of a LOCAL ZIP named by an Amiga prefix uade leaves to another
engine (``mod.Astra_1``, ``MED.Tune``: Aminet module sets) are listed by the
scan and the folder browse under the name every archive reader gives them
(``mod.Astra_1.mod``, ``archive.owned_prefix_name``) — when their first bytes
hold a module that plays under that name, never a cover or a manifest that
merely starts with such a word.  Existing tracks keep their names and ids; an
automatic scan after the upgrade lists every archive once more."""
from __future__ import annotations

import io
import uuid
import zipfile

import pytest

from soniqboom.core import archive


def _pt(title: bytes) -> bytes:
    return title.ljust(20, b"\0") + b"\0" * 1060 + b"M.K." + b"\0" * 1024


def _st(title: bytes) -> bytes:
    """A 15-sample Soundtracker module (no signature): one audible sample,
    one pattern."""
    drum = b"drum".ljust(22, b"\0") + (1000).to_bytes(2, "big") + bytes([0, 64, 0, 0, 0, 1])
    return (title.ljust(20, b"\0") + drum + b"\0" * 30 * 14 + bytes([1, 0x78]) + b"\0" * 128
            + b"\0" * 1024 + b"\1" * 2000)


MMD0 = b"MMD0" + b"\0" * 2044
PAD = b"\0" * 2040
JPEG = b"\xff\xd8\xff\xe0\0\x10JFIF\0" + bytes(range(256)) * 8
TEXT = b'{"name": "mod", "files": ["It.Takes.Two.flac"]}\n' * 50
EXE = b"MZ\x90\0\x03\0\0\0\x04\0\0\0\xff\xff\0\0\xb8" + b"\0" * 2031


def _zip(path, members: dict) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _tid(path: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, path))


def _rel(paths, root) -> set[str]:
    return {str(p).split(f"{root}/", 1)[1] for p in paths}


def _prefix_lib(root):
    """Local ZIPs named like an Aminet module set — direct members and a ZIP
    inside a ZIP — beside files whose names start with a module prefix too
    but that hold no module."""
    root.mkdir()
    _zip(root / "pack.zip", {"mod.Astra_1": _pt(b"Astra"), "MOD.cool.It": _pt(b"Cool"),
                             "song.mod": _pt(b"Song"), "MED.Tune": MMD0,
                             "mod.Old": _st(b"Old"),          # Soundtracker: no signature
                             "mod.Set": _pt(b"Set"),          # titled like a companion half
                             "mod.X.nt": b"\0" * 64,          # a Startrekker companion half
                             "It.Takes.Two.jpg": JPEG, "It.Takes.Two.m3u": TEXT,
                             "Mod.Player.exe": EXE, "mod.json": TEXT,
                             "mod.Pack.zip": b"PK\x03\x04" + b"\7" * 600,   # a broken archive
                             "mod.readme": b"notes", "smpl.Song": b"\0" * 64})
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("mod.Inner", _pt(b"Inner"))
        zf.writestr("deep.mod", _pt(b"Deep"))
    _zip(root / "outer.zip", {"inner.zip": inner.getvalue()})
    return root.resolve()


LISTED = {"pack.zip::mod.Astra_1.mod", "pack.zip::MOD.cool.It.mod", "pack.zip::song.mod",
          "pack.zip::MED.Tune.med", "pack.zip::mod.Old.mod", "pack.zip::mod.Set.mod",
          "outer.zip::inner.zip::mod.Inner.mod", "outer.zip::inner.zip::deep.mod"}


def test_a_local_zip_lists_prefix_named_modules_as_every_reader_names_them(tmp_path):
    from soniqboom.api import fstree
    from soniqboom.core import scanner
    root = _prefix_lib(tmp_path / "lib")
    found, _errors = scanner._find_audio_files([str(root)])
    names = _rel((p for ps in found.values() for p in ps), root)
    assert names == LISTED
    # The folder browse names them the same (its rows carry the scanner's ids) …
    assert _rel(fstree._discover_audio(root, recursive=True), root) == names
    assert _rel(fstree._discover_audio(root, recursive=False), root) == names
    # … and so do the other archive readers (``archive.list_members``: LHA,
    # network shares, online collections — which take a prefix name by its
    # name alone).
    pack = {n.split("::", 1)[1] for n in names if n.startswith("pack.zip::")}
    assert pack <= set(archive.list_members(root / "pack.zip"))


def test_a_prefix_named_module_in_a_local_zip_reads_as_the_module(tmp_path):
    from soniqboom.core import scanner
    root = _prefix_lib(tmp_path / "lib")
    direct = f"{root}/pack.zip::mod.Astra_1.mod"
    nested = f"{root}/outer.zip::inner.zip::mod.Inner.mod"
    assert scanner._read_from_zip_path(direct) == (_pt(b"Astra"), "mod.Astra_1.mod")
    assert scanner._read_from_zip_path(nested) == (_pt(b"Inner"), "mod.Inner.mod")
    nz = scanner._NestedZips()                      # (a scan worker's nested reads)
    try:
        assert nz.read(nested.split("::")) == _pt(b"Inner")
    finally:
        nz.clear()
    for path in (direct, nested):
        assert scanner._extract_from_zip(path, "t").format == "ProTracker"


@pytest.mark.asyncio
async def test_a_prefix_named_module_in_a_local_zip_is_extracted_for_playback(tmp_path,
                                                                              tmp_data_dir):
    from soniqboom.api import stream
    root = _prefix_lib(tmp_path / "lib")
    for path, title in ((f"{root}/pack.zip::mod.Astra_1.mod", b"Astra"),
                        (f"{root}/outer.zip::inner.zip::mod.Inner.mod", b"Inner")):
        local = await stream._get_or_extract_zip_member(path, _tid(path))
        assert local is not None and local.suffix == ".mod"
        assert local.read_bytes() == _pt(title)


def test_which_prefix_named_members_are_listed_and_under_which_name(monkeypatch):
    from soniqboom.core import metadata, uade_formats

    def name(real, head=_pt(b"T"), size=None, taken=()):
        return archive.owned_prefix_name(real, lambda _r: head, size, taken)
    # A module of a format that plays under the prefix's extension: listed
    # as ``_build_map`` (so ``list_members``) names it.
    modules = {
        "mod.Astra_1": _pt(b"A"), "MOD.First": _pt(b"F"), "mod.Set": _pt(b"S"),
        "mod.Old": _st(b"Old"), "mod.Chip_1": b"\0" * 952 + b"KRIS" + PAD,       # ChipTracker
        "mod.Ref_8": b"\0" * 1464 + b"MTN\0" + PAD,                             # ST 2.6
        "mod.Iff": b"FORM\0\0\x10\0MODLVERS" + PAD,                             # PT 3.6
        "mod.Patt": b"\0" * 1080 + b"PATT" + PAD, "mod.Cn": b"\0" * 1080 + b"16CN" + PAD,
        "mod.Packed": b"PP20" + PAD, "mod.Squashed": b"XPKF\0\0\x08\0SQSH" + PAD,
        "MED.Tune": MMD0, "MED.Old": b"MED\x04" + PAD,           # MED 2–4: zxtune
        "MED.Moved": _pt(b"M"),                  # a module under another tracker prefix
        "xm.Song": b"Extended Module: " + PAD, "s3m.Song": b"\0" * 44 + b"SCRM" + PAD,
        "it.Song": b"IMPM" + PAD, "okt.Song": b"OKTASONG" + PAD, "dbm.Song": b"DBM0" + PAD,
        "mtm.Song": b"MTM\x10" + PAD,
        "stm.Song": b"Song".ljust(20, b"\0") + b"SWavePro\x1a\x02\x02\x15" + PAD,
        "digi.Song": b"DIGI Booster module\0" + PAD,
        "ptm.Song": b"Poly".ljust(44, b"\0") + b"PTMF" + PAD,
        "hvl.Song": b"HVL\0" + PAD, "psid.Song": b"PSID" + PAD,
        "Tunes/mod.Sub": _pt(b"S"), "Tunes\\mod.Back": _pt(b"B"),
    }
    for real, head in modules.items():
        assert archive._build_map([real]) == {name(real, head): real}, real
    assert name("mod.Astra_1") == "mod.Astra_1.mod" and name("MED.Tune", MMD0) == "MED.Tune.med"
    # Not other bytes under such a name: a cover, a playlist, a program, a
    # manifest, a companion half, an archive, a module a C64 / HivelyTracker
    # name doesn't play, a Soundtracker header without room for its patterns.
    for real, head in {"It.Takes.Two.jpg": JPEG, "It.Takes.Two.m3u": TEXT,
                       "Mod.Player.exe": EXE, "mod.json": TEXT, "mod.X.nt": b"\0" * 64,
                       "MOD.Song.ins": b"\0" * 2048, "mod.Pack.zip": b"PK\x03\x04" + PAD,
                       "MED.Captain": b"MMDC" + PAD, "hvl.Song": _pt(b"H"),
                       "psid.Song": _pt(b"P"), "mod.Tiny_1": b"x"}.items():
        assert name(real, head) is None, real
    assert name("mod.Old", _st(b"Old"), size=1600) is None       # (its pattern ends at 1624)
    assert name("mod.Old", _st(b"Old"), size=len(_st(b"Old"))) == "mod.Old.mod"
    # Never a text file by its name, a name without such a prefix, a name the
    # music-name test reads (that one keeps the listing it always had).
    for real in ("mod.readme", "mod.notes.txt", "smpl.Song", "Song", "mod", "song.mod",
                 "mdat.Song", "ahx.Song"):
        assert name(real) is None, real
    # Bytes that can't be read: the name reading, as every byte reader falls
    # back to (a track listed before stays listed).  Never a name another
    # member has.
    def boom(exc):
        def head_of(_r):
            raise exc
        return head_of
    import zlib
    for exc in (zlib.error("invalid stored block lengths"), zipfile.BadZipFile("bad header"),
                EOFError(), RuntimeError("encrypted"), NotImplementedError("implode"),
                OSError(5, "Input/output error")):
        assert archive.owned_prefix_name("mod.Astra_1", boom(exc)) == "mod.Astra_1.mod", exc
    assert name("mod.Dup", taken={"mod.Dup", "mod.Dup.mod"}) is None
    assert name("mod.Dup", taken={"mod.Dup"}) == "mod.Dup.mod"
    # Without uade the uade prefixes stay unlisted — never ``ahx.Song.ahx``,
    # an id that would change once uade is installed.
    monkeypatch.setattr(uade_formats, "player_map", lambda: {})
    assert not metadata.is_supported_music_name("ahx.Song")
    assert name("ahx.Song", b"THX\0" + PAD) is None and name("mdat.Song") is None
    assert name("ahx.Song", _pt(b"A")) is None and name("sid.Song", _pt(b"S")) is None
    assert name("mod.Astra_1") == "mod.Astra_1.mod"              # (the same either way)


def test_a_name_two_members_would_share_and_a_module_named_zip(tmp_path):
    """``mod.Dup`` beside a stored ``mod.Dup.mod``: one track, the stored
    member's (as nested reads resolve it).  ``mod.Foo.zip`` holding a module
    (not a zip): listed by both readers; a broken zip by that name by neither."""
    from soniqboom.api import fstree
    from soniqboom.core import scanner
    root = tmp_path / "lib"
    root.mkdir()
    _zip(root / "a.zip", {"mod.Dup": _pt(b"Prefix"), "mod.Dup.mod": _pt(b"Suffix"),
                          "mod.Foo.zip": _pt(b"Foo"), "mod.Pack.zip": b"PK\x03\x04" + PAD})
    root = root.resolve()
    found, _errors = scanner._find_audio_files([str(root)])
    listed = [str(p).split(f"{root}/", 1)[1] for ps in found.values() for p in ps]
    browsed = [str(p).split(f"{root}/", 1)[1] for p in fstree._discover_audio(root, False)]
    assert sorted(listed) == sorted(browsed) == ["a.zip::mod.Dup.mod", "a.zip::mod.Foo.zip.mod"]
    with zipfile.ZipFile(root / "a.zip") as zf:
        assert scanner._zip_stored(zf, "mod.Dup.mod") == "mod.Dup.mod"
    assert scanner._read_from_zip_path(f"{root}/a.zip::mod.Foo.zip.mod")[0] == _pt(b"Foo")


def test_a_nested_read_finds_a_prefix_named_member_without_listing_the_archive():
    heads = []

    def head_of(real):
        heads.append(real)
        return _pt(b"x")
    names = {"mod.Astra_1", "mod.Opus", "song.mod"}
    assert archive.stored_name(names, "mod.Astra_1.mod", head_of) == "mod.Astra_1"
    assert heads == []                      # (no member map built: no bytes read)


def _encrypted(members: dict) -> bytes:
    """A ZIP whose members are all flagged encrypted (unreadable without a
    password, like one made with ``zip -e``)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, data in members.items():
            zf.writestr(n, data)
    raw = bytearray(buf.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as zf:
        heads, cdir = [zi.header_offset for zi in zf.infolist()], zf.start_dir
    for off in heads:
        raw[off + 6] |= 1                               # local header flag bits
    i = raw.find(b"PK\x01\x02", cdir)
    while i >= 0:
        raw[i + 8] |= 1                                 # central directory flag bits
        i = raw.find(b"PK\x01\x02", i + 4)
    return bytes(raw)


def test_the_folder_browse_skips_a_nested_zip_it_cant_read_as_the_scanner_does(tmp_path):
    from soniqboom.api import fstree
    from soniqboom.core import scanner
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("mod.Inner", _pt(b"Inner"))
    root = tmp_path / "lib"
    root.mkdir()
    (root / "locked.zip").write_bytes(_encrypted({"inner.zip": inner.getvalue(),
                                                  "song.mod": _pt(b"Song")}))
    root = root.resolve()
    failed: set[str] = set()
    found, _errors = scanner._find_audio_files([str(root)], failed_archives=failed)
    names = _rel((p for ps in found.values() for p in ps), root)
    assert names == {"locked.zip::song.mod"} and failed == {f"{root}/locked.zip::inner.zip"}
    assert _rel(fstree._discover_audio(root, recursive=False), root) == names


def test_a_zip_with_a_name_zipfile_cant_decode_doesnt_fail_the_folder_browse(tmp_path):
    """A member name flagged UTF-8 that isn't (zipfile raises on opening):
    the scan logs the archive as unreadable, the browse lists the rest."""
    from soniqboom.api import fstree
    from soniqboom.core import scanner
    root = tmp_path / "lib"
    root.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("café.mod", _pt(b"Cafe"))      # stored as UTF-8, flag 0x800
    (root / "bad.zip").write_bytes(buf.getvalue().replace("é".encode(), b"\xff\xfe"))
    _zip(root / "good.zip", {"mod.Astra_1": _pt(b"Astra")})
    root = root.resolve()
    with pytest.raises(UnicodeDecodeError):
        zipfile.ZipFile(root / "bad.zip")
    failed: set[str] = set()
    found, _errors = scanner._find_audio_files([str(root)], failed_archives=failed)
    assert _rel((p for ps in found.values() for p in ps), root) == {"good.zip::mod.Astra_1.mod"}
    assert failed == {f"{root}/bad.zip"}
    assert _rel(fstree._discover_audio(root, recursive=False), root) == {
        "good.zip::mod.Astra_1.mod"}


@pytest.fixture
def prefix_scan_lib(tmp_path, monkeypatch, tmp_data_dir):
    """A fresh store and the library of ``_prefix_lib``."""
    from soniqboom.core import scanner
    from soniqboom.core import store as store_mod
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())

    async def _no_full_dups():
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _no_full_dups)
    root = _prefix_lib(tmp_path / "lib")
    store_mod._store._lib_root = root
    return root, store_mod._store


@pytest.mark.asyncio
async def test_an_automatic_rescan_adds_the_prefix_named_members_and_changes_nothing_else(
        prefix_scan_lib):
    import copy
    from soniqboom.core import scanner
    root, store = prefix_scan_lib
    with pytest.MonkeyPatch.context() as mp:        # the library as a scan before this left it
        mp.setattr(archive, "owned_prefix_name", lambda *a, **k: None)
        mp.setattr(scanner, "_ARCHIVE_LISTING_VERSION", 4)
        assert await scanner._run_scan([str(root)]) is True
    assert _rel((t["path"] for t in store._tracks.values()), root) == {
        "pack.zip::MOD.cool.It.mod", "pack.zip::song.mod", "outer.zip::inner.zip::deep.mod"}
    rated = _tid(f"{root}/pack.zip::song.mod")
    store.set_rating(rated, 4)
    store.record_play(rated, uid="")
    store.create_playlist("pl1", "Mix", [rated])
    old = copy.deepcopy(store._tracks)
    # The first automatic scan after the upgrade lists every archive again …
    assert await scanner._run_scan([str(root)], light=True) is True
    added = {t: row for t, row in store._tracks.items() if t not in old}
    assert {r["path"].split(f"{root}/", 1)[1]: r["format"] for r in added.values()} == {
        "pack.zip::mod.Astra_1.mod": "ProTracker", "pack.zip::MED.Tune.med": "OctaMED",
        "pack.zip::mod.Old.mod": "ProTracker", "pack.zip::mod.Set.mod": "ProTracker",
        "outer.zip::inner.zip::mod.Inner.mod": "ProTracker"}
    assert all(added[t]["id"] == t == _tid(r["path"]) for t, r in added.items())
    # … and every track the library had is the same row under the same id.
    assert {t: store._tracks.get(t) for t in old} == old
    assert store.get_rating(rated) == 4 and store.get_play_stats(rated, uid="")["count"] == 1
    assert store._playlists["pl1"]["track_ids"] == [rated]
    # The next one lists the archives from the library: nothing changes.
    now = copy.deepcopy(store._tracks)
    assert await scanner._run_scan([str(root)], light=True) is False
    assert store._tracks == now


@pytest.mark.asyncio
async def test_a_member_whose_bytes_went_corrupt_keeps_its_track(prefix_scan_lib):
    """Corrupt compressed data in a listed ``mod.X`` member: still listed by
    its name (as a ``song.mod`` with the same damage is) — its row, rating
    and playlist entries aren't deleted."""
    from soniqboom.core import scanner
    root, store = prefix_scan_lib
    z = root / "deflated.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mod.Astra_1", _pt(b"Astra") + bytes(range(256)) * 40)
        zf.writestr("song.mod", _pt(b"Song") + bytes(range(256)) * 40)
    assert await scanner._run_scan([str(root)]) is True
    astra, song = _tid(f"{z}::mod.Astra_1.mod"), _tid(f"{z}::song.mod")
    assert astra in store._tracks and song in store._tracks
    store.set_rating(astra, 5)
    raw = bytearray(z.read_bytes())
    with zipfile.ZipFile(z) as zf:
        for zi in zf.infolist():                      # damage each member's deflate stream
            start = zi.header_offset + 30 + len(zi.filename.encode()) + len(zi.extra)
            for i in range(start + 2, start + 40):
                raw[i] ^= 0xA5
    z.write_bytes(bytes(raw))
    with zipfile.ZipFile(z) as zf, pytest.raises(Exception):
        zf.open("mod.Astra_1").read(archive.HEAD_BYTES)
    await scanner._run_scan([str(root)])
    assert astra in store._tracks and song in store._tracks
    assert store.get_rating(astra) == 5


@pytest.mark.asyncio
async def test_an_io_error_reading_a_member_keeps_the_archives_tracks(prefix_scan_lib,
                                                                       monkeypatch):
    """A share that drops while a member's first bytes are read: the archive
    is unreadable this time — its tracks stay (no prune, no later scan that
    lists it from the library without them)."""
    from soniqboom.core import scanner
    root, store = prefix_scan_lib
    assert await scanner._run_scan([str(root)]) is True
    astra = _tid(f"{root}/pack.zip::mod.Astra_1.mod")
    before = set(store._tracks)
    assert astra in before
    real_open = zipfile.ZipFile.open

    def flaky(self, name, *a, **k):
        n = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if n == "mod.Astra_1":
            raise OSError(5, "Input/output error")
        return real_open(self, name, *a, **k)
    monkeypatch.setattr(zipfile.ZipFile, "open", flaky)
    await scanner._run_scan([str(root)])
    assert set(store._tracks) == before
