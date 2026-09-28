# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub #13 — the in-file GAME name of a console rip becomes its album at
extract time (``album_source="tag"``): SPC ID666 game title, NSF name, NSFe
``auth`` game, GBS title, VGM/VGZ GD3 game, PSF ``game=``.  All fixtures are
synthetic header bytes, so these run anywhere."""
from __future__ import annotations

import gzip
import struct
from pathlib import Path

from soniqboom.core import metadata


def _pad(s: bytes, n: int) -> bytes:
    return s[:n].ljust(n, b"\x00")


def _spc(tmp_path: Path, *, game: bytes = b"Super Metroid", has_tag: int = 26,
         name: str = "t.spc") -> Path:
    b = bytearray(0x10200)
    b[0:33] = b"SNES-SPC700 Sound File Data v0.30"
    b[0x21:0x23] = b"\x1a\x1a"
    b[0x23] = has_tag
    b[0x24] = 30
    b[0x2E:0x4E] = _pad(b"Title Screen", 32)
    b[0x4E:0x6E] = _pad(game, 32)
    b[0xB1:0xD1] = _pad(b"Kenji Yamamoto", 32)
    p = tmp_path / name
    p.write_bytes(bytes(b))
    return p


def test_spc_id666_game_becomes_album(tmp_path):
    d = metadata._extract_gme(_spc(tmp_path), "t1")
    assert d["title"] == "Title Screen"
    assert d["artist"] == "Kenji Yamamoto"
    assert d["album"] == "Super Metroid"
    assert d["album_source"] == "tag"


def test_spc_without_id666_tag_has_no_album(tmp_path):
    """Byte 0x23 == 27 means "no ID666" — the game bytes are then garbage."""
    d = metadata._extract_gme(_spc(tmp_path, has_tag=27), "t1")
    assert "album" not in d and "album_source" not in d


def test_spc_placeholder_or_binary_game_is_refused(tmp_path):
    for i, game in enumerate((b"<?>", b"   ", b"\x01\x02\x03abc", b"")):
        d = metadata._extract_gme(_spc(tmp_path, game=game, name=f"{i}.spc"), "t")
        assert "album" not in d, game


def test_spc_full_extract_carries_album_source_on_trackmeta(tmp_path):
    meta = metadata.extract(_spc(tmp_path), "t1")
    assert meta.album == "Super Metroid"
    assert meta.album_source == "tag"
    # The model field exists, so every TrackMeta-shaped API response and the
    # store dict (model_dump) keep it.
    assert meta.model_dump()["album_source"] == "tag"


def _nsf(tmp_path: Path, name: bytes, artist: bytes = b"Koji Kondo") -> Path:
    b = bytearray(0x80)
    b[0:5] = b"NESM\x1a"
    b[5] = 1
    b[6] = 3
    b[0x0E:0x2E] = _pad(name, 32)
    b[0x2E:0x4E] = _pad(artist, 32)
    p = tmp_path / "g.nsf"
    p.write_bytes(bytes(b) + b"\x00" * 64)
    return p


def test_nsf_name_becomes_album(tmp_path):
    d = metadata._extract_gme(_nsf(tmp_path, b"Super Mario Bros."), "n1")
    assert d["album"] == "Super Mario Bros." and d["album_source"] == "tag"
    assert d["artist"] == "Koji Kondo"


def test_nsf_placeholder_name_is_not_an_album(tmp_path):
    d = metadata._extract_gme(_nsf(tmp_path, b"<?>"), "n1")
    assert "album" not in d


def test_nsfe_auth_chunk_game_and_artist(tmp_path):
    def chunk(cid: bytes, data: bytes) -> bytes:
        return struct.pack("<I", len(data)) + cid + data
    body = (b"NSFE"
            + chunk(b"INFO", b"\x00" * 10)
            + chunk(b"DATA", b"\xEA" * 5000)          # skipped by seeking
            + chunk(b"auth", b"Mega Man 2\x00Takashi Tateishi\x00Capcom\x00ripper\x00")
            + chunk(b"NEND", b""))
    p = tmp_path / "mm2.nsfe"
    p.write_bytes(body)
    d = metadata._extract_gme(p, "e1")
    assert d["album"] == "Mega Man 2" and d["album_source"] == "tag"
    assert d["artist"] == "Takashi Tateishi"


def test_gbs_title_becomes_album(tmp_path):
    b = bytearray(0x70)
    b[0:3] = b"GBS"
    b[3] = 1
    b[0x10:0x30] = _pad(b"Tetris", 32)
    b[0x30:0x50] = _pad(b"Hirokazu Tanaka", 32)
    p = tmp_path / "t.gbs"
    p.write_bytes(bytes(b) + b"\x00" * 64)
    d = metadata._extract_gme(p, "g1")
    assert d["album"] == "Tetris" and d["album_source"] == "tag"


def _vgm_bytes(track="Green Hill Zone", game="Sonic the Hedgehog",
               author="Masato Nakamura") -> bytes:
    fields = [track, "", game, "", "Sega Mega Drive", "", author, "",
              "1991", "ripper", "notes"]
    strings = "".join(f + "\x00" for f in fields).encode("utf-16-le")
    gd3 = b"Gd3 " + struct.pack("<II", 0x100, len(strings)) + strings
    header = bytearray(0x40)
    header[0:4] = b"Vgm "
    data = b"\x66" * 300                               # command stream (end)
    gd3_off = 0x40 + len(data)
    struct.pack_into("<I", header, 0x14, gd3_off - 0x14)
    struct.pack_into("<I", header, 0x04, gd3_off + len(gd3) - 4)
    return bytes(header) + data + gd3


def test_vgm_gd3_game_title_and_author(tmp_path):
    p = tmp_path / "ghz.vgm"
    p.write_bytes(_vgm_bytes())
    d = metadata._extract_gme(p, "v1")
    assert d["album"] == "Sonic the Hedgehog" and d["album_source"] == "tag"
    assert d["title"] == "Green Hill Zone"
    assert d["artist"] == "Masato Nakamura"


def test_vgz_gd3_is_read_through_gzip(tmp_path):
    p = tmp_path / "ghz.vgz"
    p.write_bytes(gzip.compress(_vgm_bytes(game="Streets of Rage 2")))
    d = metadata._extract_gme(p, "v2")
    assert d["album"] == "Streets of Rage 2"


def test_vgm_without_gd3_or_corrupt_keeps_filename_title(tmp_path):
    raw = bytearray(_vgm_bytes())
    struct.pack_into("<I", raw, 0x14, 0)               # no GD3
    p = tmp_path / "plain.vgm"
    p.write_bytes(bytes(raw))
    d = metadata._extract_gme(p, "v3")
    assert d["title"] == "plain" and "album" not in d
    bad = tmp_path / "bad.vgz"
    bad.write_bytes(b"\x1f\x8b" + b"not really gzip")
    d = metadata._extract_gme(bad, "v4")
    assert d["title"] == "bad" and "album" not in d


def test_psf_game_tag_is_stamped_tag_source(tmp_path):
    body = b"PSF\x01" + b"\x00" * 12 + b"[TAG]title=Opening\ngame=Quest 64\nartist=Masamichi Amano\n"
    p = tmp_path / "01.minipsf"
    p.write_bytes(body)
    meta = metadata.extract(p, "p1")
    assert meta.album == "Quest 64" and meta.album_source == "tag"


def test_modules_get_no_album_source_from_extract(tmp_path):
    """A plain module never claims a tag album (album_source stays None)."""
    p = tmp_path / "x.gbs"
    p.write_bytes(b"GBS" + b"\x01" + b"\x00" * 0x6C)  # empty title
    d = metadata._extract_gme(p, "x")
    assert "album_source" not in d
