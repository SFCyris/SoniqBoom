# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GitHub #24 (format magic as a tracker title; Amiga prefix names), #25
(``artist:`` matched only a whole artist name) and #27 (VGM files using a chip
libgme can't play rendered silence)."""
from __future__ import annotations

import gzip
import shutil
import struct
import zipfile
from pathlib import Path

import pytest

from soniqboom.core import metadata, repair, scanner
from soniqboom.core.store import TrackStore


# ── #24 ─────────────────────────────────────────────────────────────────────

def _mmd(magic: bytes = b"MMD0", name: bytes = b"Dying Forest", exp_at: int = 0x40) -> bytes:
    raw = bytearray(0x200)
    raw[0:4] = magic
    struct.pack_into(">I", raw, 4, len(raw))           # file length: its top byte is 0
    if exp_at:
        struct.pack_into(">I", raw, 32, exp_at)
        if name and exp_at + 52 <= len(raw):                # else: a pointer past the end
            struct.pack_into(">II", raw, exp_at + 44, 0x100, len(name))
            raw[0x100:0x100 + len(name)] = name
    return bytes(raw)


@pytest.mark.parametrize("magic", [b"MMD0", b"MMD1", b"MMD2", b"MMD3"])
def test_an_octamed_title_is_its_song_name_never_the_magic(tmp_path, magic):
    p = tmp_path / "MED.Dying_Forest.med"
    p.write_bytes(_mmd(magic))
    assert metadata._extract_tracker(p, "x")["title"] == "Dying Forest"


@pytest.mark.parametrize("data", [_mmd(name=b""), _mmd(exp_at=0), _mmd(exp_at=0x1F0)])
def test_an_octamed_file_without_a_song_name_is_named_after_its_file(tmp_path, data):
    p = tmp_path / "MED.Dying_Forest.med"
    p.write_bytes(data)
    assert metadata._extract_tracker(p, "x")["title"] == "Dying_Forest"


@pytest.mark.parametrize("ext,head", [
    (".okt", b"OKTASONGCMOD\0\0\0\x08"), (".669", b"ifCool 669 song mess"),
    (".far", b"FAR\xfeFar Title"), (".gdm", b"GDM\xfeGdm Title"),
    (".ult", b"MAS_UTrack_V004Ult Title"), (".amf", b"ASYLUM Music Format V1.0"),
])
def test_format_magic_never_becomes_a_title(tmp_path, ext, head):
    p = tmp_path / f"tune{ext}"
    p.write_bytes(head.ljust(0x400, b"\0"))
    assert metadata._extract_tracker(p, "x")["title"] == "tune"


def test_a_stand_in_song_name_is_no_title(tmp_path):
    p = tmp_path / "MED.Dying_Forest.med"
    p.write_bytes(_mmd(name=b"<unnamed>"))
    assert metadata._extract_tracker(p, "x")["title"] == "Dying_Forest"
    for name in ("soul crystal - dying forest.mmd0", "MMD0.Dying_Forest"):
        d = metadata._extract_uade(Path("/m") / name, "x",
                                   {"modulename": "<no songtitle>", "playername": "MED"})
        assert d["title"] == {"MMD0.Dying_Forest": "Dying_Forest"}.get(
            name, "soul crystal - dying forest")
    d = metadata._extract_uade(Path("/m/mdat.acieed1"), "x", {"modulename": "Acid Song"})
    assert d["title"] == "Acid Song"
    d = metadata._extract_uade(Path("/m/mdat.acieed1"), "x", {})
    assert d["title"] == "acieed1"
    assert repair._has_bad_tracker_title({"path": "/m/x.mmd0", "title": "<no songtitle>"})


def test_an_stm_keeps_its_title_at_offset_0(tmp_path):
    p = tmp_path / "x.stm"
    p.write_bytes(b"Stm Title".ljust(20, b"\0") + b"!Scream!" + b"\0" * 0x400)
    assert metadata._extract_tracker(p, "x")["title"] == "Stm Title"


@pytest.mark.parametrize("name,want", [
    ("MED.Intro", "Intro"), ("med.Intro", "Intro"), ("MOD.Space_Debris", "Space_Debris"),
    ("mdat.acieed1", "acieed1"), ("MOD.Space_Debris.mod", "Space_Debris"),
    ("Intro.med", "Intro"), ("Intro.mod", "Intro"), ("Dr.Awesome.mod", "Dr.Awesome"),
    ("Fred.mod", "Fred"), ("ice.mod", "ice"), ("one.xm", "one"), ("Star.Wars.mod", "Star.Wars"),
    ("song.mp3", "song"), ("a.zip::SUB\\mdat.foo.mdat", "foo"),
    ("x.lha::MOD.Space.Debris.mod", "Space.Debris"), ("/music/it.was.fun.mp3", "it.was.fun"),
])
def test_title_from_filename(name, want):
    assert metadata.title_from_filename(name) == want


def test_an_archived_prefix_named_module_gets_a_clean_title(tmp_path):
    z = tmp_path / "pack.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("MED.Dying_Forest", _mmd(name=b""))
        zf.writestr("MOD.Space_Debris", b"\0" * 20 + b"\0" * 1064 + b"M.K." + b"\0" * 64)
    for member, want in (("MED.Dying_Forest.med", "Dying_Forest"),
                         ("MOD.Space_Debris.mod", "Space_Debris")):
        assert scanner._extract_from_zip(f"{z}::{member}", "t").title == want
    assert scanner._member_stem("pack.zip::MED.Dying_Forest.med") == "Dying_Forest"


def test_the_metadata_repair_finds_the_magic_titles():
    assert repair._has_bad_tracker_title({"path": "/m/MED.x.med", "title": "MMD0"})
    assert repair._has_bad_tracker_title({"path": "/m/x.oct", "title": "MMD2"})
    assert not repair._has_bad_tracker_title({"path": "/m/x.med", "title": "Dying Forest"})
    assert repair._has_bad_tracker_title({"path": "/m/x.hvl", "title": "HVL"})
    assert ".oct" in repair.TRACKER_LIKE_EXTS and ".med" in repair.TRACKER_LIKE_EXTS


# ── #25 ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def artists():
    s = TrackStore()

    def tr(i, artist, album_artist=None):
        return {"id": i, "path": f"/hvsc/{i}.sid", "title": f"T{i}", "artist": artist,
                "album_artist": album_artist, "album": "", "format": "SID",
                "added_at": 1000}
    s.upsert_tracks_batch([
        tr("a", "Marcin Romanowski (Sidder)"), tr("b", "Marcin Romanowski (Sidder)"),
        tr("c", "Marcin Romanowski"), tr("d", "Marek Szoltysek (Shy)"),
        tr("e", "Ghost"), tr("f", "Ghost Inside"), tr("g", "Zoë Keating"),
        tr("h", "A\x00Marcinek", album_artist="Various Marcins"),
    ])
    return s


def _ids(store, **kw):
    return sorted(t["id"] for t in store.filter_tracks(limit=100, **kw))


@pytest.mark.parametrize("value,want", [
    ("Marcin", ["a", "b", "c", "h"]), ("marcin", ["a", "b", "c", "h"]),
    ("Marcin Romanowski", ["a", "b", "c"]), ("marcin   romanowski", ["a", "b", "c"]),
    ("Marcin Romanowski (Sidder)", ["a", "b"]), ("sidder", ["a", "b"]),
    ("arcin", []), ("zoe", ["g"]), ("Ghost", ["e", "f"]), ("nobody", []),
])
def test_artist_match_is_a_word_start(artists, value, want):
    assert _ids(artists, artist_match=value) == want


def test_an_artists_page_stays_exact(artists):
    assert _ids(artists, artist="Ghost") == ["e"]
    assert _ids(artists, artist="Marcin") == []
    assert _ids(artists, album_artist_match="marcins") == ["h"]


@pytest.mark.asyncio
async def test_the_search_box_artist_operator(artists, monkeypatch):
    from soniqboom.api import search as search_api
    from soniqboom.core import store as store_mod
    monkeypatch.setattr(store_mod, "_store", artists)
    adv = search_api._parse_advanced_query("artist:Marcin")
    assert adv == "@artist_match:{Marcin}"
    from soniqboom.core.data import _parse_tag_query
    assert _parse_tag_query(adv) == {"artist_match": "Marcin"}
    rows = await search_api.run_search_dicts("artist:Marcin", limit=50)
    assert sorted(r["id"] for r in rows) == ["a", "b", "c", "h"]
    assert sorted(artists.filter_track_ids(**_parse_tag_query(adv))) == ["a", "b", "c", "h"]


# ── #27 ─────────────────────────────────────────────────────────────────────

def _vgm(clocks: dict, *, version=0x161, data_rel=None, gz=False) -> bytes:
    hdr = bytearray(0x100)
    hdr[0:4] = b"Vgm "
    struct.pack_into("<I", hdr, 0x04, 0x100 + 1 - 4)
    struct.pack_into("<I", hdr, 0x08, version)
    for off, clk in clocks.items():
        struct.pack_into("<I", hdr, off, clk)
    if data_rel is None:
        data_rel = 0x100 - 0x34
    struct.pack_into("<I", hdr, 0x34, data_rel)
    raw = bytes(hdr) + b"\x66"                           # end of sound data
    return gzip.compress(raw) if gz else raw


SN, YM2413, YM2612, YM2151, NES, AY = 0x0C, 0x10, 0x2C, 0x30, 0x84, 0x74


@pytest.mark.parametrize("clocks,want", [
    ({SN: 3579545}, False),
    ({SN: 3579545, YM2612: 7670453}, False),
    ({NES: 1789773}, True), ({YM2151: 4000000}, True), ({AY: 1500000}, True),
    ({SN: 3579545, NES: 1789773}, True),
    ({YM2413: 3579545}, True), ({SN: 3579545, YM2413: 3579545}, True),   # libgme's YM2413 is a stub
    ({SN: 3579545 | 0x40000000}, False),               # a dual-chip flag is still a clock
    ({SN: 3579545, 0x78: 0x0010, 0x7C: 0x0A}, False),   # AY type / volume fields: no chips
])
@pytest.mark.parametrize("gz", [False, True])
def test_vgm_needs_other_engine(tmp_path, clocks, want, gz):
    from soniqboom.core.gme_render import vgm_needs_other_engine
    p = tmp_path / ("t.vgz" if gz else "t.vgm")
    p.write_bytes(_vgm(clocks, gz=gz))
    assert vgm_needs_other_engine(p) is want


def test_a_pre_110_vgm_shares_the_ym2413_field(tmp_path):
    from soniqboom.core.gme_render import vgm_needs_other_engine
    p = tmp_path / "t.vgm"
    p.write_bytes(_vgm({SN: 3579545, YM2413: 7670453}, version=0x101))
    assert vgm_needs_other_engine(p) is False


def test_vgm_header_end_bounds_the_chip_fields(tmp_path):
    from soniqboom.core.gme_render import vgm_needs_other_engine
    p = tmp_path / "t.vgm"
    # v1.50, data right after 0x40: the bytes from 0x40 on are music, not clocks
    p.write_bytes(_vgm({SN: 3579545, NES: 0xDEADBEEF}, version=0x150, data_rel=0x0C))
    assert vgm_needs_other_engine(p) is False
    p.write_bytes(_vgm({SN: 3579545, YM2151: 4000000}, version=0x101))
    assert vgm_needs_other_engine(p) is True
    for junk in (b"", b"Vgm ", b"\x1f\x8b\x08junk", b"RIFF" + b"\0" * 0x100):
        p.write_bytes(junk)
        assert vgm_needs_other_engine(p) is False
    assert vgm_needs_other_engine(tmp_path / "missing.vgm") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("clocks,engine", [({NES: 1789773}, "zxtune"),
                                           ({SN: 3579545}, "libgme")])
async def test_render_gme_routes_by_chip(tmp_path, monkeypatch, clocks, engine):
    from soniqboom.api import stream
    from soniqboom.core import gme_render
    p = tmp_path / "t.vgz"
    p.write_bytes(_vgm(clocks, gz=True))
    used = []

    async def zx(path, *, kind):
        used.append("zxtune")
        out = tmp_path / "zx.wav"
        out.write_bytes(b"RIFF")
        return out

    def gme(data, subsong, duration):
        used.append("libgme")
        return b"RIFF"
    monkeypatch.setattr(stream, "_render_zxtune_capped", zx)
    monkeypatch.setattr(gme_render, "is_available", lambda: True)
    monkeypatch.setattr(gme_render, "render_wav", gme)
    out = await stream._render_gme(p)
    out.unlink(missing_ok=True)
    assert used == [engine]


@pytest.mark.asyncio
async def test_an_nes_apu_vgm_renders_audibly(tmp_path):
    if not shutil.which("zxtune123"):
        pytest.skip("zxtune123 is missing")
    from soniqboom.api import stream
    from soniqboom.core import silence
    # NES APU: pulse 1 at full volume, a 440 Hz-ish tone, then 2 s of waits
    cmds = bytes([0xB4, 0x00, 0xBF, 0xB4, 0x02, 0xFD, 0xB4, 0x03, 0x00,
                  0xB4, 0x15, 0x01]) + bytes([0x61, 0x44, 0xAC]) * 2 + b"\x66"
    hdr = bytearray(_vgm({NES: 1789773})[:0x100])
    total = 2 * 0xAC44
    struct.pack_into("<I", hdr, 0x18, total)
    data = bytes(hdr) + cmds
    struct.pack_into("<I", hdr, 0x04, len(data) - 4)
    p = tmp_path / "nes.vgm"
    p.write_bytes(bytes(hdr) + cmds)
    out = await stream._render_gme(p)
    try:
        assert silence.wav_audible(out) is True
    finally:
        out.unlink(missing_ok=True)
