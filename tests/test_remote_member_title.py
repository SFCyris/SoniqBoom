# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The title fallback of an archive member is the MEMBER's stem.

A member whose extractor finds no embedded title is titled after its own
name.  The network-share scan (``scanner._extract_one_remote``) and the
repair re-extract (``repair._re_extract_remote_sync``) took the stem of the
whole path's last ``/`` component instead — for a member at the archive root
that is ``ch17_vot.zip::DR_FIST.XM``, so the track was titled
``ch17_vot.zip::DR_FIST``.  The startup temp-title fixup
(``scanner.purge_junk_tracks``) did the same to a member whose real name
looks like a temp file (``tmpmachine.mod``).  All three now use
``scanner._member_stem``: the stem of the name after the last ``::`` (Amiga
members may use backslash folders), like the local archive path.

The scan's Amiga (uade) branch names its temp file after the member too
(``scanner._member_basename``) — named ``a.zip::SUB\\b.bd``, the extractor's
own fallback titled it ``SUB\\b``; and the repair's stub-read guard
(``repair._read_lost_data``) expects the same stem the re-extract now gives."""
from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest

from soniqboom.core import repair, scanner
from soniqboom.core.store import TrackStore

REPO = Path(__file__).resolve().parents[1]
# Private test asset (internal/ is gitignored) — skipped when absent.
BD = REPO / "internal/testdata/uade/Ben Daglish/mickey mouse.bd"


def _xm(name: bytes = b"") -> bytes:
    """A minimal FastTracker 2 module whose 20-byte module name is ``name``
    (empty: no embedded title, so the extractor falls back to the file name)."""
    head = (b"Extended Module: " + name.ljust(20, b"\x00") + b"\x1a"
            + b"FastTracker v2.00   " + struct.pack("<H", 0x0104))
    # header size, song length, restart, channels, patterns, instruments,
    # flags, tempo, BPM — no patterns / instruments to walk.
    body = struct.pack("<IHHHHHHHH", 276, 1, 0, 4, 0, 0, 1, 6, 125)
    return (head + body).ljust(400, b"\x00")


# (path, expected title) — remote subpaths as the scan passes them, and full
# share URLs as the repair passes them.
_UNTITLED = [
    ("music/artists/ch17_vot.zip::DR_FIST.XM", "DR_FIST"),
    ("ftp://10.0.0.88/Music/Demo:/music/artists/ch17_vot.zip::DR_FIST.XM", "DR_FIST"),
    ("ftp://h/s:/a/outer.zip::inner.zip::song.xm", "song"),
    ("ftp://h/s:/a/pack.zip::sub/dir/song.xm", "song"),
    ("ftp://h/s:/a/pack.zip::SUB\\song.xm", "song"),       # Amiga backslash folder
    ("ftp://h/s:/a/it214p2.zip::IT.EXE.xm", "IT.EXE"),     # multi-dot member name
    ("ftp://h/s:/music/plain.xm", "plain"),                # not an archive member
]


@pytest.mark.parametrize("path,want", _UNTITLED)
def test_scan_remote_member_without_title_is_titled_after_the_member(path, want):
    _p, meta, *_ = scanner._extract_one_remote(_xm(), path, "t")
    assert not isinstance(meta, str), meta
    assert meta.title == want
    assert meta.path == path


@pytest.mark.parametrize("path,want", _UNTITLED)
def test_repair_remote_member_without_title_is_titled_after_the_member(path, want):
    got, err = repair._re_extract_remote_sync(_xm(), path, "t")
    assert err is None
    assert got["title"] == want
    assert got["path"] == path


@pytest.mark.parametrize("path", [p for p, _w in _UNTITLED])
def test_embedded_title_is_kept(path):
    # A real module name wins, also one that itself holds "::".
    _p, meta, *_ = scanner._extract_one_remote(_xm(b":: First Assault ::"), path, "t")
    assert meta.title == ":: First Assault ::"
    got, _err = repair._re_extract_remote_sync(_xm(b":: First Assault ::"), path, "t")
    assert got["title"] == ":: First Assault ::"


@pytest.mark.parametrize("path,want", [
    ("x.zip::song.mod", "song"),
    ("/m/T/TM.zip::tmpmachine.mod.zip::tmpmachine.mod", "tmpmachine"),
    ("ftp://h/s:/a/pack.zip::SUB\\Song.MOD", "Song"),
    ("ftp://h/s:/a/song.mod", "song"),
    ("C:\\m\\song.mod", "song"),
])
def test_member_stem(path, want):
    assert scanner._member_stem(path) == want


@pytest.mark.skipif(not BD.exists() or not shutil.which("uade123"),
                    reason="needs uade123 and internal/testdata/uade")
@pytest.mark.parametrize("path", [
    "ftp://h/s:/a/pack.zip::SUB\\mickey mouse.bd",
    "ftp://h/s:/a/pack.zip::mickey mouse.bd",
    "ftp://h/s:/a/pack.zip::sub/mickey mouse.bd",
])
def test_scan_amiga_member_is_titled_after_the_member(path):
    # A Ben Daglish module has no embedded name; uade's temp-dir branch.
    data = BD.read_bytes()
    _p, meta, *_ = scanner._extract_one_remote(data, path, "t")
    assert not isinstance(meta, str), meta
    assert meta.title == "mickey mouse"
    got, err = repair._re_extract_remote_sync(data, path, "t")
    assert err is None and got["title"] == "mickey mouse"


@pytest.mark.parametrize("path", [
    "ftp://h/s:/a/pack.zip::song.xm",
    "ftp://h/s:/a/pack.zip::sub/song.xm",
    "ftp://h/s:/a/pack.zip::SUB\\song.xm",
    "/m/pack.zip::SUB\\song.xm",
])
def test_repair_refuses_a_stub_read_over_a_real_title(path):
    # A read that came back as the extractor's stub (the member's name, no
    # tags) must not replace a stored title — for backslash members too.
    empty = {"artist": "", "album": "", "album_artist": "", "composer": "",
             "game": "", "year": None}
    cur = dict(empty, path=path, title="Real Module Name")
    new = dict(empty, path=path, title="song")
    assert repair._read_lost_data(cur, new, {"title": "song"})


async def test_purge_temp_title_fixup_uses_the_member_name(monkeypatch):
    s = TrackStore()
    monkeypatch.setattr("soniqboom.core.store.get_store", lambda: s)
    base = {"artist": "", "album": "", "album_artist": "", "composer": "",
            "format": "ProTracker", "genre": [], "added_at": 1}
    s.upsert_tracks_batch([
        # A leaked temp-file title on an archive member → the member's stem.
        dict(base, id="leak", path="/m/x.zip::song.mod", title="tmpab12cd34"),
        # A member whose REAL name looks like a temp file: already right —
        # it was rewritten to "TM.zip::tmpmachine.mod.zip::tmpmachine".
        dict(base, id="real", path="/m/T/TM.zip::tmpmachine.mod.zip::tmpmachine.mod",
             title="tmpmachine"),
        dict(base, id="plain", path="/m/tune.mod", title="tmpzz99yy88"),
    ])
    res = await scanner.purge_junk_tracks()
    assert s.get_track("leak")["title"] == "song"
    assert s.get_track("real")["title"] == "tmpmachine"
    assert s.get_track("plain")["title"] == "tune"
    assert res["repaired_titles"] == 2
