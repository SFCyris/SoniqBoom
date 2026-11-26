# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A member named as an archive (``abk.mod.zip``, ``x.lha``) whose bytes are
one is an archive, never a module — even when its name starts with an Amiga
prefix token (``abk`` = AMOS).

* the archive layer (network shares, LHA, online collections, playback) listed
  ``abk.mod.zip`` as the AMOS module ``abk.mod.zip.abk``
  (``archive.rejected_by_bytes`` now refuses it);
* the scan's walk indexed a DAMAGED nested zip with such a name as a module —
  now it is skipped like any other damaged nested zip (a rewritten outer
  archive's content is what it holds now: ``test_outer_rewritten``); a member
  that only has an archive's name (bytes of a module) is still a module;
* the folder browse of an unindexed folder (``fstree._scan_zip``) raised
  ``TypeError`` for any zip holding a nested zip with a tune in it;
* scans before 1.10.0 tested the audio name first and indexed
  ``AB.zip::abk.mod.zip`` itself: an automatic scan lists such an archive
  again once (``scanner._content_named``), so the row goes and the tune
  inside comes."""
from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path

import pytest

from soniqboom.core import archive, scanner


def _pt(title: bytes) -> bytes:
    return title.ljust(20, b"\0") + b"\0" * 1060 + b"M.K." + b"\0" * 1024


AMOS = b"AmBk" + b"\0" * 300
LHA = b"\x22\x00-lh0-" + b"\0" * 300          # an LHA level-0 header


def _zipbytes(members: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return b.getvalue()


def _zip(path, members: dict) -> None:
    Path(path).write_bytes(_zipbytes(members))


def _needs_uade_prefix(*toks):
    from soniqboom.core import uade_formats as u
    missing = [t for t in toks if t not in u.player_map()]
    return pytest.mark.skipif(bool(missing), reason=f"no eagleplayer.conf token {missing}")


def _pack() -> dict:
    nested = _zipbytes({"abk.mod": _pt(b"Inner")})
    return {
        "abk.mod.zip": nested,                         # a nested zip, AMOS-token name
        "ast.xm.zip": nested[:40],                     # a damaged one (zip header, cut)
        "abk.x.lha": LHA,                              # an LHA archive inside the zip
        "abk.title": AMOS,                             # a real AMOS member
        "abk.bank.zip": AMOS,                          # a module that only has a zip's name
        "plain.mod.zip": _zipbytes({"plain.mod": _pt(b"Plain")}),
    }


@_needs_uade_prefix("abk", "ast")
def test_the_archive_layer_never_lists_a_nested_archive_as_a_module(tmp_path):
    z = tmp_path / "AB.zip"
    _zip(z, _pack())
    assert set(archive.list_members(z)) == {"abk.title.abk", "abk.bank.zip.abk"}


@_needs_uade_prefix("abk", "ast")
def test_the_scan_walk_opens_nested_zips_and_skips_a_damaged_one(tmp_path):
    root = tmp_path / "M"
    root.mkdir()
    _zip(root / "AB.zip", _pack())
    failed: set = set()
    found, _errors = scanner._find_audio_files([str(root)], failed_archives=failed)
    got = {str(p).split("AB.zip", 1)[1] for ps in found.values() for p in ps}
    assert got == {"::abk.mod.zip::abk.mod", "::plain.mod.zip::plain.mod",
                   "::abk.title", "::abk.bank.zip"}
    assert failed == set()


YM = Path(__file__).resolve().parents[1] / "internal/testdata/atari/ym/gear1.ym"


@pytest.mark.skipif(not YM.exists(), reason="needs internal/testdata/atari/ym")
@_needs_uade_prefix("ym")
def test_an_lha_packed_ym_tune_is_never_taken_for_an_archive(tmp_path):
    # Atari YM files ARE LHA-packed (``-lh5-`` at offset 2): named after the
    # archive they came in (``gear1.lzh.ym``) or in prefix form with an LHA
    # name (``YM.song.lha``), they stay tunes — in the listing and the walk.
    ym = YM.read_bytes()
    assert ym[2:7] == b"-lh5-"
    root = tmp_path / "M"
    root.mkdir()
    z = root / "ST.zip"
    _zip(z, {"gear1.lzh.ym": ym, "YM.song.lha": ym, "gear1.ym": ym, "pack.lha": LHA})
    assert set(archive.list_members(z)) == {"gear1.lzh.ym", "YM.song.lha.ym", "gear1.ym"}
    found, _errors = scanner._find_audio_files([str(root)])
    got = {str(q).split("ST.zip", 1)[1] for qs in found.values() for q in qs}
    assert got == {"::gear1.lzh.ym", "::YM.song.lha", "::gear1.ym"}


def test_the_folder_browse_lists_the_tunes_of_nested_zips(tmp_path):
    from soniqboom.api import fstree
    z = tmp_path / "AB.zip"
    _zip(z, _pack())
    got = {str(p).split("AB.zip", 1)[1] for p in fstree._scan_zip(str(z))}
    assert {"::abk.mod.zip::abk.mod", "::plain.mod.zip::plain.mod"} <= got
    assert not any(g.endswith((".zip", ".lha")) for g in got)


def test_rows_named_as_an_archive_make_their_archive_listed_again():
    assert scanner._content_named(["/m/AB.zip::abk.mod.zip"])           # a pre-1.10.0 row
    assert scanner._content_named(["/m/x.zip::abk.mod.zip.abk"])        # the archive layer's
    assert scanner._content_named(["/m/x.lha::Tunes\\abk.x.lha.abk"])
    assert not scanner._content_named(["/m/AB.zip::abk.mod.zip::abk.mod"])
    assert not scanner._content_named(["/m/x.zip::abk.title.abk", "/m/x.zip::zip.mod"])
    # (a YM named after its archive: listed again once — its bytes keep it)
    assert scanner._content_named(["/m/ST.zip::gear1.lzh.ym"])


def _tid(p: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, p))


def test_a_row_named_as_an_archive_is_no_renamed_member():
    from soniqboom.core.store import TrackStore
    store = TrackStore()
    p = "/l/AB.zip::abk.mod.zip"
    store.upsert_tracks_batch([{"id": _tid(p), "path": p, "title": "mod.zip"}])
    assert scanner._renamed_member_ids({_tid(p)}, store) == {}


@pytest.fixture
def scan_lib(tmp_path, monkeypatch, tmp_data_dir):
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
    _zip(root / "AB.zip", {"abk.mod.zip": _zipbytes({"abk.mod": _pt(b"Inner")}),
                           "song.mod": _pt(b"Song")})
    root = root.resolve()
    store_mod._store._lib_root = root
    return root, store_mod._store


@pytest.mark.asyncio
@_needs_uade_prefix("abk")
async def test_an_automatic_scan_replaces_a_nested_zip_indexed_as_a_module(scan_lib):
    root, store = scan_lib
    inner = f"{root}/AB.zip::abk.mod.zip::abk.mod"
    stale = f"{root}/AB.zip::abk.mod.zip"
    assert await scanner._run_scan([str(root)]) is True
    assert _tid(inner) in store._tracks
    # The library as a scan before 1.10.0 left it: the nested zip itself
    # indexed as an AMOS module, the tune inside it missing.
    row = dict(store._tracks[_tid(inner)], id=_tid(stale), path=stale,
               format="AMOS", title="mod.zip")
    store.delete_track_ids([_tid(inner)])
    store.upsert_tracks_batch([row])
    assert await scanner._run_scan([str(root)], light=True) is True
    assert _tid(stale) not in store._tracks
    assert store._tracks[_tid(inner)]["format"] == "ProTracker"
    assert _tid(f"{root}/AB.zip::song.mod") in store._tracks


@pytest.mark.asyncio
@_needs_uade_prefix("abk")
async def test_a_network_share_lists_no_nested_zip_as_a_module(scan_lib):
    """On a network share (WebDAV) a nested zip is not opened at all — and no
    longer listed as an AMOS module (``AB.zip::abk.mod.zip.abk``).  A row an
    earlier listing left goes when its archive is listed again: here the
    naming record is dropped, which only a change of
    ``archive.MEMBER_NAMING_VERSION`` (or of the archive) does for real."""
    import json
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from _webdav_server import DavServer
    from soniqboom.core.filesource import register_source, remove_source
    from soniqboom.core.filesource_webdav import WebDAVFileSource
    lib, store = scan_lib
    srv = DavServer(lib).start()
    root = srv.url
    src = WebDAVFileSource(root + "/")
    register_source(root, src)
    try:
        assert (await scanner.start_remote_scan("dav1", root, src)).get("extract")
        names = {t["path"].rsplit("::", 1)[-1] for t in store._tracks.values()}
        assert "song.mod" in names
        assert not any(n.startswith("abk.mod.zip") for n in names)
        song = next(t for t in store._tracks.values() if t["path"].endswith("::song.mod"))
        stale = song["path"][:-len("song.mod")] + "abk.mod.zip.abk"
        store.upsert_tracks_batch([dict(song, id=_tid(stale), path=stale,
                                        format="AMOS", title="mod.zip")])
        side = scanner._enum_sidecar_path()
        data = json.loads(side.read_text())
        data.pop("named", None)
        side.write_text(json.dumps(data))
        await scanner.start_remote_scan("dav1", root, src)
        assert _tid(stale) not in store._tracks
        assert song["id"] in store._tracks
    finally:
        remove_source(root)
        srv.stop()
