# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Amiga prefix names that also read as a suffix name (``mod.Opus``,
``mod.Sun``, ``MOD.cool.It``) are decided by the member's bytes
(``archive._content_display_name``): the prefix reading when they carry that
format's signature — or neither format's, where the suffix format has one —
the name reading otherwise; the same in every reader (local archives, ranged
ZIP subsets).  A change of the naming rules makes the scanner enumerate such
archives again, once; a tune renamed by it keeps its user data."""
from __future__ import annotations

import io
import zipfile

import pytest

from soniqboom.core import archive


def _pt(title: bytes) -> bytes:
    return title.ljust(20, b"\0") + b"\0" * 1060 + b"M.K." + b"\0" * 1024


OGG = b"OggS" + b"\0" * 2044


def _zip(path, members: dict) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_a_prefix_name_with_a_module_inside_reads_as_the_module(tmp_path):
    z = tmp_path / "pack.zip"
    _zip(z, {"mod.Opus": _pt(b"Opus"), "mod.Sun": _pt(b"Sun"), "MOD.cool.It": _pt(b"Cool"),
             "mod.Ogg": OGG, "song.opus": OGG, "dw.Sun": b"\0" * 64, "mod.mod": _pt(b"M")})
    names = archive.list_members(z)
    assert set(names) == {"mod.Opus.mod", "mod.Sun.mod", "MOD.cool.It.mod",
                          "mod.Ogg",                 # Ogg bytes: the name reading stays
                          "song.opus",               # no prefix: a plain name
                          "dw.Sun",                  # no signature for the token: as before
                          "mod.mod"}
    assert archive.read_member(z, "mod.Opus.mod")[:4] == b"Opus"
    # An old path (before the content rule) still reads its bytes.
    assert archive.read_member(z, "mod.Opus")[:4] == b"Opus"


def test_without_the_bytes_the_name_reading_is_kept():
    assert archive._build_map(["mod.Opus"]) == {"mod.Opus": "mod.Opus"}
    boom = lambda r: (_ for _ in ()).throw(OSError("unreadable"))   # noqa: E731
    assert archive._build_map(["mod.Opus"], boom) == {"mod.Opus": "mod.Opus"}


def test_a_ranged_zip_subset_names_the_member_the_same_way(tmp_path):
    from soniqboom.core import remote_zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("mod.Opus", _pt(b"Opus"))
        zf.writestr("other/big.bin", b"\1" * 200_000)   # (not beside it: no companion)
    data = buf.getvalue()

    class Src:
        def read_at(self, path, off, n, *, lane="scan"):
            return data[off:off + n]
    dest = tmp_path / "subset.zip"
    assert remote_zip.build_subset(Src(), "/pack.zip", len(data), 0.0, "mod.Opus.mod", dest,
                                   scan_root="t")
    assert archive.list_members(dest) == ["mod.Opus.mod"]


def test_a_local_zip_lists_and_reads_the_member_by_its_bytes(tmp_path):
    from soniqboom.core import scanner
    lib = tmp_path / "lib"
    lib.mkdir()
    _zip(lib / "pack.zip", {"mod.Opus": _pt(b"Opus"), "mod.Ogg": OGG, "song.mod": _pt(b"S")})
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("mod.Sun", _pt(b"Sun"))
    _zip(lib / "outer.zip", {"inner.zip": inner.getvalue()})
    found, _errors = scanner._find_audio_files([str(lib)])
    names = sorted(str(p).split(str(lib) + "/", 1)[1] for ps in found.values() for p in ps)
    assert "pack.zip::mod.Opus.mod" in names and "pack.zip::mod.Ogg" in names
    assert "outer.zip::inner.zip::mod.Sun.mod" in names
    data, member = scanner._read_from_zip_path(f"{lib}/pack.zip::mod.Opus.mod")
    assert data[:4] == b"Opus" and member == "mod.Opus.mod"
    data, _m = scanner._read_from_zip_path(f"{lib}/outer.zip::inner.zip::mod.Sun.mod")
    assert data[:3] == b"Sun"
    # Its metadata: the module it is.
    meta = scanner._extract_from_zip(f"{lib}/pack.zip::mod.Opus.mod", "t")
    assert meta.format == "ProTracker"


def test_an_archive_inside_an_lha_reads_through_the_chain(tmp_path, monkeypatch):
    from soniqboom.core import scanner
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("mod.Opus", _pt(b"Opus"))
    real = archive.read_member
    monkeypatch.setattr(archive, "read_member",
                        lambda local, name: inner.getvalue() if str(local).endswith("pkg.lha")
                        else real(local, name))
    data, member = scanner._read_from_zip_path(f"{tmp_path}/pkg.lha::inner.zip::mod.Opus.mod")
    assert data[:4] == b"Opus" and member == "mod.Opus.mod"


def test_only_archives_with_names_their_bytes_decide_are_listed_again():
    from soniqboom.core import scanner
    assert scanner._content_named(["/a/x.zip::mod.Opus"])            # listed before the rule
    assert scanner._content_named(["/a/x.lha::Tunes\\mod.Sun"])
    assert not scanner._content_named(["/a/x.zip::mod.Opus.mod"])    # decided already
    assert not scanner._content_named(["/a/x.zip::song.mod", "/a/x.zip::mdat.Sun"])
    assert not scanner._content_named(["/a/x.zip::dw.Sun"])          # no signature: as before


@pytest.fixture
def scan_lib(tmp_path, monkeypatch, tmp_data_dir):
    """A fresh store and a library of two ZIPs of modules, one of them
    named both ways (``mod.Opus``)."""
    from soniqboom.core import scanner
    from soniqboom.core import store as store_mod
    from soniqboom.core.store import TrackStore
    monkeypatch.setattr(store_mod, "_store", TrackStore())
    monkeypatch.setattr(scanner, "_scan_count", 0)
    monkeypatch.setattr(scanner, "_progress", scanner.ScanProgress())

    async def _no_full_dups():
        return None
    monkeypatch.setattr(scanner, "_run_duplicate_detection_async", _no_full_dups)
    root = tmp_path / "lib"
    root.mkdir()
    _zip(root / "pack.zip", {"song.mod": _pt(b"Song"), "mod.Opus": _pt(b"Opus")})
    _zip(root / "other.zip", {"tune.mod": _pt(b"Tune")})
    root = root.resolve()
    store_mod._store._lib_root = root
    return root, store_mod._store


def _tid(path: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, path))


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", ["small", "batch"])
async def test_a_member_listed_under_its_new_name_keeps_its_user_data(scan_lib, monkeypatch,
                                                                      commit):
    from soniqboom.core import scanner
    root, store = scan_lib
    if commit == "batch":
        monkeypatch.setattr(scanner, "_SMALL_COMMIT", 0)
    assert await scanner._run_scan([str(root)]) is True
    old_path, new_path = f"{root}/pack.zip::mod.Opus", f"{root}/pack.zip::mod.Opus.mod"
    old, new = _tid(old_path), _tid(new_path)
    gone = _tid(f"{root}/other.zip::tune.mod")
    assert new in store._tracks and gone in store._tracks
    # The library as a scan by the older naming rules left it: the member
    # under its name reading, rated, played and in a playlist.
    row = dict(store._tracks[new], id=old, path=old_path, format="Opus")
    store.delete_track_ids([new])
    store.upsert_tracks_batch([row])
    store.set_rating(old, 4)
    store.record_play(old, uid="")
    store.create_playlist("pl1", "Mix", [old, gone])
    store.set_rating(gone, 3)
    (root / "other.zip").unlink()                    # (a member that really goes)
    assert await scanner._run_scan([str(root)]) is True
    assert old not in store._tracks and new in store._tracks
    assert store._tracks[new]["format"] == "ProTracker"
    assert store.get_rating(new) == 4 and store.get_rating(old) == 0
    assert store.get_play_stats(new, uid="")["count"] == 1 and store.get_play_stats(old, uid="")["count"] == 0
    assert store._playlists["pl1"]["track_ids"] == [new, gone]
    # Nothing renamed it: its data stays with its id (it may come back).
    assert gone not in store._tracks and store.get_rating(gone) == 3


def test_only_a_renamed_member_whose_new_row_is_there_is_mapped(tmp_path):
    from soniqboom.core import scanner
    from soniqboom.core.store import TrackStore
    store = TrackStore()
    a, b, c = "/l/x.zip::mod.Opus", "/l/x.zip::mod.Sun", "/l/y.zip::mod.Gone.mod"
    store.upsert_tracks_batch([{"id": _tid(p), "path": p, "title": p} for p in (a, b, c)]
                              + [{"id": _tid(a + ".mod"), "path": a + ".mod", "title": "n"}])
    got = scanner._renamed_member_ids({_tid(a), _tid(b), _tid(c)}, store)
    assert got == {_tid(a): _tid(a + ".mod")}       # b: no new row; c: no other reading


@pytest.mark.asyncio
async def test_a_remote_member_listed_under_its_new_name_keeps_its_user_data(scan_lib, tmp_path):
    """The same on a network share (WebDAV): the ghost cleanup hands the
    old row's user data to its renamed row."""
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from _webdav_server import DavServer
    from soniqboom.core import scanner
    from soniqboom.core.filesource import register_source, remove_source
    from soniqboom.core.filesource_webdav import WebDAVFileSource
    lib, store = scan_lib
    srv = DavServer(lib).start()                    # (the same two ZIPs, served)
    root = srv.url
    src = WebDAVFileSource(root + "/")
    register_source(root, src)
    try:
        assert (await scanner.start_remote_scan("dav1", root, src)).get("extract")
        by_name = {t["path"].rsplit("::", 1)[-1]: t for t in store._tracks.values()}
        assert {"song.mod", "mod.Opus.mod", "tune.mod"} <= set(by_name)
        new_path = by_name["mod.Opus.mod"]["path"]
        new, gone = by_name["mod.Opus.mod"]["id"], by_name["tune.mod"]["id"]
        old_path = new_path[:-len(".mod")]
        old = _tid(old_path)
        # As a scan by the older naming rules left it (no naming record either).
        row = dict(store._tracks[new], id=old, path=old_path, format="Opus")
        store.delete_track_ids([new])
        store.upsert_tracks_batch([row])
        store.set_rating(old, 5)
        store.create_playlist("pl2", "Mix", [old])
        side = scanner._enum_sidecar_path()
        data = json.loads(side.read_text())
        data.pop("named", None)
        side.write_text(json.dumps(data))
        (lib / "other.zip").unlink()
        await scanner.start_remote_scan("dav1", root, src)
        assert old not in store._tracks and new in store._tracks
        assert store.get_rating(new) == 5 and store._playlists["pl2"]["track_ids"] == [new]
        assert gone not in store._tracks                 # (really gone: no new name)
    finally:
        remove_source(root)
        srv.stop()


def test_a_module_without_a_signature_named_after_another_format(tmp_path):
    """A Soundtracker module (15 samples: no signature) named ``mod.Ref.ogg``
    is no Ogg Vorbis file: neither format's mark — the prefix names it.  Real
    Ogg bytes keep the name reading."""
    st15 = b"Ref".ljust(20, b"\0") + b"\0" * 450 + bytes([4, 0x78]) + b"\0" * 128 + b"\0" * 1024
    z = tmp_path / "pack.zip"
    _zip(z, {"mod.Ref.ogg": st15, "mod.Real.ogg": OGG, "mod.Tiny.ogg": b"x"})
    assert set(archive.list_members(z)) >= {"mod.Ref.ogg.mod", "mod.Real.ogg"}
    assert archive.read_member(z, "mod.Ref.ogg.mod")[:3] == b"Ref"
    assert archive.content_name("mod.Tiny.ogg", lambda r: b"x") == "mod.Tiny.ogg"   # too short to tell



@pytest.mark.asyncio
async def test_an_archive_listed_by_the_current_rules_is_not_listed_on_every_scan(
        scan_lib, monkeypatch):
    """An archive with a name its bytes decided to keep (``mod.Ogg`` holding
    Ogg) is enumerated again once per naming rules, not on every scan."""
    from soniqboom.core import scanner
    root, _store = scan_lib
    z = root / "amb.zip"
    _zip(z, {"mod.Ogg": OGG, "tune2.mod": _pt(b"T2")})
    assert await scanner._run_scan([str(root)]) is True
    seen = []
    real = scanner._find_audio_files

    def spy(dirs, scan_zips, scope, known, failed):
        seen.append(set(known or ()))
        return real(dirs, scan_zips, scope, known, failed)
    monkeypatch.setattr(scanner, "_find_audio_files", spy)
    await scanner._run_scan([str(root)], light=True)
    await scanner._run_scan([str(root)], light=True)
    assert str(z) not in seen[0]                     # listed once by the current rules …
    assert str(z) in seen[1]                         # … then from the library
