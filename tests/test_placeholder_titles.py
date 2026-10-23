# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A tracker module whose stored song name is a stand-in ("<unnamed>", as The
Mod Archive's MOD / XM uploads carry it) is named after its file — at a scan,
and by Repair Garbled Metadata for tracks indexed before — and the repair's
Scan says why it lists a track."""
from __future__ import annotations

import io
import zipfile

import pytest

from soniqboom.api import admin
from soniqboom.core import metadata, repair
from soniqboom.core.store import TrackStore


def _mod(title: bytes) -> bytes:
    return title.ljust(20, b"\0") + b"\0" * 1060 + b"M.K." + b"\0" * 1024


def _xm(title: bytes) -> bytes:
    head = b"Extended Module: " + title.ljust(20, b" ") + b"\x1aFastTracker v2.00   "
    return head.ljust(0x150, b"\0")


@pytest.fixture
def store(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    return s


@pytest.mark.parametrize("name,data", [("ab3d2-packedtest.mod", _mod(b"<unnamed>")),
                                       ("blatem.xm", _xm(b"<unnamed>")),
                                       ("tune.mod", _mod(b"<Untitled>"))],
                         ids=["mod", "xm", "other-stand-in"])
def test_a_placeholder_song_name_becomes_the_file_name(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    assert metadata._extract_tracker(p, "x")["title"] == p.stem


@pytest.mark.parametrize("name,data,want", [
    ("tune.mod", _mod(b"Space Debris"), "Space Debris"),
    ("tune.xm", _xm(b"Song <3>"), "Song <3>"),
    ("tune.mod", _mod(b"<intro>"), "<intro>"),          # not a stand-in
], ids=["plain", "angle-brackets-inside", "bracketed-name"])
def test_a_real_song_name_is_kept(tmp_path, name, data, want):
    p = tmp_path / name
    p.write_bytes(data)
    assert metadata._extract_tracker(p, "x")["title"] == want


def _nested(tmp_path, stem: str, ext: str, data: bytes) -> str:
    """The Mod Archive layout: ``AB.zip::<name>.zip::<name>``."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr(f"{stem}{ext}", data)
    outer = tmp_path / "AB.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr(f"{stem}{ext}.zip", inner.getvalue())
    return f"{outer}::{stem}{ext}.zip::{stem}{ext}"


def test_repair_renames_a_stored_placeholder_title(tmp_path, store):
    path = _nested(tmp_path, "ab3d2-packedtest", ".mod", _mod(b"<unnamed>"))
    store.upsert_tracks_batch([{"id": "m", "path": path, "title": "<unnamed>", "artist": "",
                                "album": "", "format": "ProTracker", "genre": []}])
    assert [t["id"] for t in repair.find_corrupt_tracks()] == ["m"]
    new, err = repair._re_extract_local_sync(path, "m")
    assert err is None and new["title"] == "ab3d2-packedtest"
    ok, applied, err = repair._apply_re_extract("m", new)
    assert (ok, applied, err) == (True, True, None)
    assert store.get_track("m")["title"] == "ab3d2-packedtest"
    assert repair.find_corrupt_tracks() == []                # the next Scan lists none


def test_repair_kind():
    assert repair.repair_kind({"path": "/m/a.mod", "title": "Ti�le"}) == "garbled"
    assert repair.repair_kind({"path": "/m/a.xm", "title": "<unnamed>"}) == "placeholder"
    assert repair.repair_kind({"path": "/m/a.mmd0", "title": "<no songtitle>"}) == "placeholder"
    assert repair.repair_kind({"path": "/m/a.hvl", "title": "HVL"}) == "bad-title"
    assert repair.repair_kind({"path": "/m/a.sid", "title": "Hⁿlsbeck"}) == "cp437"
    assert repair.repair_kind({"path": "/m/a.mod", "title": "Fine"}) is None
    assert repair.repair_kind({"path": "/m/a.it", "title": " <Untitled> "}) == "placeholder"
    assert repair.repair_kind({"path": "/m/a.mod", "title": "<intro>"}) is None
    # ordinary audio tagged so is its own tag, never re-read as a module
    assert repair.repair_kind({"path": "/m/a.mp3", "title": "<unnamed>"}) is None


def test_a_write_the_store_refuses_is_an_error(store, monkeypatch):
    store.upsert_tracks_batch([{"id": "m", "path": "/m/x.mod", "title": "<unnamed>",
                                "artist": "", "album": "", "format": "ProTracker", "genre": []}])
    monkeypatch.setattr(store, "update_track_fields", lambda *a, **k: False)
    assert repair._apply_re_extract("m", {"title": "x"}) == (False, False, "store-rejected")


async def test_the_scan_counts_each_kind(store):
    store.upsert_tracks_batch([
        {"id": i, "path": f"/m/{i}.{ext}", "title": title, "artist": "", "album": "",
         "format": "ProTracker", "genre": []}
        for i, ext, title in (("a", "mod", "<unnamed>"), ("b", "xm", "<unnamed>"),
                              ("c", "mod", "T�"), ("d", "mod", "Fine"))])
    res = await admin.metadata_repair_scan({"tracker_only": True}, _tok="t")
    assert res["count"] == 3
    assert res["kinds"] == {"placeholder": 2, "garbled": 1}
