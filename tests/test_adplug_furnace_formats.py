# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rest of AdPlug's formats and Furnace's own modules.

AdPlug: a file of one of the formats it reads by its verdict
(``metadata._ADLIB_VERIFY_EXTS``) is listed when adplay reads it — for a
suffix other files have, as that suffix's format (``adlib_verdict``); one
whose suffix another engine owns (``ADLIB_SHARED_FORMATS``: ``.mus``,
``.ims``, ``.sng``, ``.dtm``) after that engine refuses it, routed to adplay
by its record's format.  Furnace: ``.fur`` by its header, title and author
from its INFO block, rendered by Furnace as FamiTracker modules are.  Where
adplay / furnace-headless and the reference files are there (``SB_ADPLUG_SAMPLES``,
default ``internal/testdata/adplug`` — AdPlug's own test music;
``SB_FUR_SAMPLES``, default ``internal/testdata/furnace``), real scans and
renders."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from soniqboom.core import metadata as md
from soniqboom.core import uade_formats as uf

ROOT = Path(__file__).resolve().parents[1]
ADPLUG = Path(os.environ.get("SB_ADPLUG_SAMPLES") or ROOT / "internal/testdata/adplug")
FUR = Path(os.environ.get("SB_FUR_SAMPLES") or ROOT / "internal/testdata/furnace")
ADPLAY = shutil.which("adplay")
FURNACE = shutil.which("furnace-headless")


# ── AdPlug ───────────────────────────────────────────────────────────────────

def test_adlib_verdict_reads_adplays_type():
    assert md.adlib_verdict(".a2t", {"type": "Adlib Tracker 2 (tiny module version 11)"})
    assert not md.adlib_verdict(".a2t", {})
    assert not md.adlib_verdict(".a2t", {"title": "x"})
    # a suffix other files have: that suffix's player only
    assert md.adlib_verdict(".m", {"type": "Ultima 6 Music"})
    assert not md.adlib_verdict(".m", {"type": "Raw AdLib Capture"})
    assert md.adlib_verdict(".raw", {"type": "Raw AdLib Capture"})
    assert md.adlib_verdict(".sng", {"type": "Faust Music Creator"})
    assert not md.adlib_verdict(".sng", {"type": "HSC Adlib Composer / HSC-Tracker"})


def test_the_new_adlib_suffixes_are_supported_and_named():
    for ext, name in md._ADLIB_VERIFY_NAMES.items():
        assert ext in md.SUPPORTED_EXTENSIONS and ext in md._ADLIB_EXTS, ext
        assert md.FORMAT_NAMES[ext] == name
        assert uf.ext_owned_elsewhere(ext), ext
    for ext in md.ADLIB_SHARED_FORMATS:
        assert ext in md.SUPPORTED_EXTENSIONS, ext
    js = (ROOT / "soniqboom/frontend/js/utils.js").read_text()
    block = js.split("export const ADLIB_FORMAT_NAMES = new Set([", 1)[1].split("]);", 1)[0]
    block = re.sub(r"//[^\n]*", "", block)                       # (comments: apostrophes)
    names = {a or b for a, b in re.findall(r"\"([^\"]+)\"|'([^']+)'", block)}
    assert set(md._ADLIB_VERIFY_NAMES.values()) <= names
    assert set(md.ADLIB_SHARED_FORMAT_NAMES) <= names


def test_shared_suffix_adlib_tunes_route_to_adplay_by_their_format():
    from soniqboom.api import stream as st
    for ext, fmt in md.ADLIB_SHARED_FORMATS.items():
        assert st._render_ident(f"/m/x{ext}", {"format": fmt}) == (st._ADLIB_ROUTE, False), ext
    assert st._ADLIB_ROUTE in st._ADLIB_EXTS
    assert st._render_ident("/m/x.sng", {"format": md.GOATTRACKER_FORMAT})[0] == st._GOAT_ROUTE
    assert st._render_ident("/m/x.mus", {"format": md.C64_SIDPLAYER_FORMAT})[0] == st._SIDPLAYER_ROUTE
    assert st._render_ident("/m/x.mus", {"format": md.DMX_MUS_FORMAT})[0] == st._DMXMUS_ROUTE
    # (a verified-suffix AdLib file routes by its own suffix)
    assert st._render_ident("/m/WONDERIN.WLF", {"format": "AdLib IMF"}) == (".wlf", False)


def test_without_adplay_a_verified_format_is_not_listed(monkeypatch, tmp_path):
    real = md._find_atari_binary
    monkeypatch.setattr(md, "_find_atari_binary",
                        lambda s, b: None if b == "adplay" else real(s, b))
    f = tmp_path / "song.a2t"
    f.write_bytes(bytes(range(256)) * 8)
    with pytest.raises(md.NotPlayableHere):
        md.extract(f, "a1")


def test_an_implay_song_name_is_read_as_johab(monkeypatch, tmp_path):
    # adplay prints the bytes of an IMPlay song's name (Korean, Johab):
    # read as latin-1 they would be mojibake
    johab = "떠나버려! -DeuX".encode("johab").decode("latin-1")
    f = tmp_path / "song.ims"
    f.write_bytes(b"\0" * 64)
    d = md._extract_adlib(f, "a2", head={"title": johab, "type": "AdLib Visual Composer: IMPlay"},
                          fmt="AdLib IMS")
    assert d["title"] == "떠나버려! -DeuX" and d["format"] == "AdLib IMS"


@pytest.mark.skipif(not ADPLAY, reason="adplay not installed")
def test_text_or_other_formats_under_generic_suffixes_are_not_listed(tmp_path):
    for name, body in (("script.m", b"x = linspace(0, 1);\nplot(x);\n"),
                       ("photo.raw", bytes(range(256)) * 40),
                       ("snapin.msc", b"<?xml version='1.0'?><MMC_ConsoleFile/>")):
        f = tmp_path / name
        f.write_bytes(body)
        with pytest.raises(md.NotPlayableHere):
            md.extract(f, "a3")
    pmd = ROOT / "internal/testdata/unsupported/calm milly.m"     # (a PC-98 PMD song)
    if pmd.is_file():
        with pytest.raises(md.NotPlayableHere):
            md.extract(pmd, "a4")


def _adplug(name: str) -> "Path | None":
    p = ADPLUG / name
    return p if p.is_file() else None


@pytest.mark.skipif(not ADPLAY or not ADPLUG.is_dir(), reason="adplay / AdPlug test music absent")
@pytest.mark.parametrize("name,fmt", [
    ("AB_JULIA.A2T", "AdLib Tracker 2"), ("WONDERIN.WLF", "AdLib IMF"), ("BOOTUP.M", "Ultima 6 Music"),
    ("crusader.raw", "Raw AdLib"), ("bmf1_2.xad", "XAD AdLib"), ("ARRAKIS.SDB", "HERAD"),
    ("lines1.mus", "AdLib MUS"), ("revival.ims", "AdLib IMS"), ("REVELAT.SNG", "AdLib SNG"),
    ("DTM-TRK1.DTM", "DeFy AdLib Tracker"), ("blaster2.msc", "AdLib MSC"), ("menu.got", "God of Thunder"),
])
def test_real_adplug_files_scan_as_their_format(name, fmt):
    f = _adplug(name)
    if f is None:
        pytest.skip(f"{name} absent")
    m = md.extract(f, "a5")
    assert m.format == fmt, name


@pytest.mark.skipif(not ADPLAY or not ADPLUG.is_dir(), reason="adplay / AdPlug test music absent")
def test_real_adplug_renders_are_audible():
    from soniqboom.api import stream as st
    for name in ("CHILD1.XSM", "loudness.lds", "lines1.mus", "DEMO4.JBM"):
        f = _adplug(name)
        if f is None:
            continue
        out = asyncio.run(st._render_adlib(f))
        try:
            assert st._wav_is_audible(out), name
            assert st._wav_audio_seconds(out) > 3, name
        finally:
            Path(out).unlink(missing_ok=True)


# ── Furnace ──────────────────────────────────────────────────────────────────

def _fur(title: bytes, author: bytes, *, compress: bool = True, version: int = 212) -> bytes:
    """A Furnace module head: magic, version, the INFO block pointer and an
    INFO block with its 24 bytes of timing / counts, 4 × 32 chip bytes and
    the song name and author."""
    info = b"INFO" + struct.pack("<I", 400) + b"\0" * 24 + b"\0" * 224 + title + b"\0" + author + b"\0"
    raw = b"-Furnace module-" + struct.pack("<HHI", version, 0, 32) + b"\0" * 8 + info + b"\0" * 64
    return zlib.compress(raw) if compress else raw


def test_furnace_header_reads_title_and_author():
    for comp in (True, False):
        head = md.furnace_header(_fur(b"Bridge Zone", b"Yuzo Koshiro", compress=comp))
        assert head == {"version": 212, "title": "Bridge Zone", "author": "Yuzo Koshiro"}
    assert md.is_furnace_module(_fur(b"a", b"b")[:4096])
    assert not md.is_furnace_module(b"\x78\x9c" + b"\0" * 30)      # (zlib, not Furnace)
    assert not md.is_furnace_module(b".DelekDefleMask." + b"\0" * 16)
    assert md.furnace_header(b"nope") is None


def test_furnace_files_scan_and_route(tmp_path):
    from soniqboom.api import stream as st
    f = tmp_path / "song.fur"
    f.write_bytes(_fur(b"FamiTune #1", b"Wegfrei"))
    m = md.extract(f, "f1")
    assert (m.format, m.title, m.artist, m.duration) == ("Furnace", "FamiTune #1", "Wegfrei", 0.0)
    bad = tmp_path / "junk.fur"
    bad.write_bytes(b"not a furnace module" * 10)
    with pytest.raises(ValueError, match="not a Furnace module"):
        md.extract(bad, "f2")
    assert st._own_engine(".fur") == ("fami", "furnace", "Furnace")
    assert ".fur" in st._PSF_STREAM_EXTS and st._route_renderer(Path("x.fur")) == "furnace"
    assert st._own_variant("fami") == "ft1"


def test_furnace_runs_with_a_home_of_its_own_on_macos_too():
    src = (ROOT / "soniqboom/api/stream.py").read_text()
    body = src.split("async def _render_famitracker(", 1)[1].split("\nasync def ", 1)[0]
    assert '"CFFIXED_USER_HOME": str(home)' in body
    from soniqboom.api import admin
    env = admin._furnace_env("/tmp/x")
    assert env["HOME"] == env["CFFIXED_USER_HOME"] == "/tmp/x" and "XDG_CONFIG_HOME" not in env


@pytest.mark.skipif(not FURNACE or not FUR.is_dir(), reason="furnace-headless / Furnace songs absent")
def test_real_furnace_songs_render_audible():
    from soniqboom.api import stream as st
    songs = sorted(FUR.glob("*.fur"))[:2]
    if not songs:
        pytest.skip("no .fur song")
    for f in songs:
        m = md.extract(f, "f3")
        assert m.format == "Furnace" and m.title
        out = asyncio.run(st._render_psf(f))
        try:
            assert st._wav_is_audible(out), f.name
        finally:
            Path(out).unlink(missing_ok=True)


def test_without_uade_the_shared_suffixes_go_straight_to_adplug_and_zxtune(monkeypatch):
    """A host without uade (a ``SLIM=1`` image): an AdLib ``.ims`` / ``.sng``
    / ``.mus`` and a ZX ``.stp`` / ``.sqt`` are asked of AdPlug / zxtune123
    directly; a file neither reads is not listed."""
    monkeypatch.setattr(md, "_UADE_SUFFIX_EXTS", set(md._UADE_SUFFIX_EXTS) - {
        ".ims", ".sng", ".mus", ".stp", ".sqt"})
    monkeypatch.setattr(md._uade, "classify", lambda _name: None)
    zx = Path(os.environ.get("SB_ZX_SAMPLES") or ROOT / "internal/testdata/zxspectrum")
    cases = [(ADPLUG / "revival.ims", "AdLib IMS", ADPLAY), (ADPLUG / "REVELAT.SNG", "AdLib SNG", ADPLAY),
             (ADPLUG / "tafa.mus", "AdLib MUS", ADPLAY),
             (zx / "iris_setup.stp", "Sound Tracker Pro", shutil.which("zxtune123")),
             (zx / "tsd.sqt", "SQ-Tracker", shutil.which("zxtune123"))]
    ran = 0
    for f, fmt, tool in cases:
        if not tool or not f.is_file():
            continue
        assert md.extract(f, "nu").format == fmt, f.name
        ran += 1
    if not ran:
        pytest.skip("adplay / zxtune123 / their reference files absent")


def test_without_uade_an_unread_shared_suffix_is_not_listed(monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_UADE_SUFFIX_EXTS", set(md._UADE_SUFFIX_EXTS) - {".ims", ".stp"})
    monkeypatch.setattr(md._uade, "classify", lambda _name: None)
    for name in ("x.ims", "y.stp"):
        f = tmp_path / name
        f.write_bytes(bytes(range(256)) * 16)
        with pytest.raises(md.NotPlayableHere):
            md.extract(f, "nu2")
