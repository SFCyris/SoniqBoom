# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DefleMask modules (``.dmf``, Furnace): told from libopenmpt's X-Tracker /
DSMI Compact / Software Visions ``.dmf`` by content, their header (title,
author, system), the one-tune routing (web play, cast, archives, skip rules),
Modland's DefleMask folder, and — where Furnace is built (``SB_FURNACE``,
else on PATH) — real renders of the reference songs
(``internal/testdata/deflemask``, from Modland's ``Deflemask/DevEd``)."""
from __future__ import annotations

import array
import os
import shutil
import wave
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from soniqboom.core import metadata as md

ROOT = Path(__file__).resolve().parents[1]
DEFLE = ROOT / "internal/testdata/deflemask"
NP = ROOT / "internal/testdata/nostalgicplayer"


def _bin(env: str, name: str) -> "str | None":
    p = os.environ.get(env) or shutil.which(name)
    return p if p and os.path.exists(p) else None


FURNACE = _bin("SB_FURNACE", "furnace-headless") or _bin("SB_FURNACE", "furnace")
needs_data = pytest.mark.skipif(not DEFLE.is_dir(), reason="internal/testdata/deflemask absent")
needs_furnace = pytest.mark.skipif(not (FURNACE and DEFLE.is_dir()),
                                   reason="furnace / internal/testdata/deflemask absent")


def _dmf(version: int, system: "int | None", *texts: bytes, compress: bool = True) -> bytes:
    body = b".DelekDefleMask." + bytes([version])
    if system is not None:
        body += bytes([system])
    for t in texts:
        body += bytes([len(t)]) + t
    body += bytes(64)
    return zlib.compress(body) if compress else body


# ── Header ───────────────────────────────────────────────────────────────────

def test_deflemask_header():
    h = md.deflemask_header(_dmf(0x18, 0x04, b"Song", b"Ann"))
    assert h == {"title": "Song", "author": "Ann", "system": "Game Boy", "version": 0x18}
    assert md.deflemask_header(_dmf(0x0b, 0x02, b"", b""))["system"] == "Sega Genesis"
    assert md.deflemask_header(_dmf(0x12, 0x99, b"T", b"A"))["system"] == ""   # (an unknown code)
    # (not compressed: read as is)
    assert md.deflemask_header(_dmf(0x18, 0x06, b"X", b"Y", compress=False))["title"] == "X"
    # an old Yamaha YMU759 module: no system byte, 13 texts, name and author 4th / 5th
    ymu = _dmf(0x05, None, b"vendor", b"carrier", b"cat", b"Name", b"Auth", b"w")
    assert md.deflemask_header(ymu) == {"title": "Name", "author": "Auth",
                                        "system": "Yamaha YMU759", "version": 5}
    # control bytes in a text are dropped
    assert md.deflemask_header(_dmf(0x18, 0x04, b"A\x01B\x7f", b""))["title"] == "AB"


def test_what_isnt_deflemask():
    for raw in (b"", b"DDMF" + bytes(100), b"DMF\x0e" + bytes(100), zlib.compress(b"hello" * 50),
                b"\x78\x9c" + bytes(30), b".DelekDefle"):
        assert not md.is_deflemask_module(raw)
        assert md.deflemask_header(raw) is None
    # a truncated module: the magic is still there, the texts end early
    head = _dmf(0x18, 0x04, b"Title", b"Author")[:12]
    assert md.deflemask_header(head) is None or md.deflemask_header(head)["title"] in ("", "Title")


def test_the_compressed_read_is_bounded():
    """A module whose data inflates to far more than the head is read only
    up to ``_DEFLE_HEAD_MAX``."""
    big = zlib.compress(b".DelekDefleMask.\x18\x04\x01T\x01A" + bytes(50_000_000))
    assert len(md._deflemask_plain(big)) <= md._DEFLE_HEAD_MAX
    assert md.deflemask_header(big)["title"] == "T"


@needs_data
@pytest.mark.parametrize("name, title, author, system", [
    ("7_grand_cop.dmf", "7 GRAND COP (FLEENSTONES!???)", "DevEd", "Game Boy"),
    ("DuckTales2.dmf", "DuckTales 2 - Niagra Falls", "???, DevEd", "Sega Genesis"),
    ("JonTron.dmf", "JonTron", "", "Game Boy"),          # (no name in the file: its own)
])
def test_reference_deflemask_scan(name, title, author, system):
    t = md.extract(DEFLE / name, "d").model_dump()
    assert (t["format"], t["title"], t.get("artist"), t.get("comment"), t["duration"]) == (
        "DefleMask", title, author, system, 0.0)
    assert t["genre"] == ["Chiptune"]


@pytest.mark.skipif(not (NP / "dsmicompact").is_dir(), reason="nostalgicplayer test data absent")
def test_the_other_dmf_formats_stay_libopenmpts():
    if ".dmf" not in md.OPENMPT_EXTRA_EXTS:
        pytest.skip("libopenmpt here doesn't read .dmf")
    t = md.extract(NP / "dsmicompact/tronic-gameover.dmf", "x").model_dump()
    assert t["format"] == "DSMI Compact" and t["duration"] > 20
    sv = next((NP / "softwarevisions").glob("*.dmf"))
    assert md.extract(sv, "y").model_dump()["format"] == "Software Visions"


def test_a_dmf_that_isnt_deflemask_needs_libopenmpt(tmp_path, monkeypatch):
    """Without a libopenmpt that reads ``.dmf``, a DefleMask module is still
    read (Furnace); any other ``.dmf`` is skipped like an unplayed suffix."""
    monkeypatch.setattr(md, "OPENMPT_EXTRA_EXTS", frozenset())
    other = tmp_path / "x.dmf"
    other.write_bytes(b"DDMF" + bytes(500))
    with pytest.raises(md.NotPlayableHere):
        md.extract(other, "x")
    defle = tmp_path / "y.dmf"
    defle.write_bytes(_dmf(0x18, 0x04, b"Tune", b"Me"))
    t = md.extract(defle, "y").model_dump()
    assert (t["format"], t["title"], t["artist"]) == ("DefleMask", "Tune", "Me")
    assert ".dmf" in md.SUPPORTED_EXTENSIONS


# ── Routing ──────────────────────────────────────────────────────────────────

def test_routing_names_it_by_format():
    from soniqboom.api import stream
    rec = {"id": "1", "path": "/m/a.dmf", "format": "DefleMask"}
    assert stream._render_ident(rec["path"], rec) == (stream._DEFLE_ROUTE, False)
    assert stream._DEFLE_ROUTE in stream._PSF_STREAM_EXTS
    assert stream._own_engine(stream._DEFLE_ROUTE) == ("fami", "furnace", "DefleMask")
    assert stream._own_engine(Path("/m/a.dmf")) == ("fami", "furnace", "DefleMask")
    assert stream.renderer_key_for(rec) == "furnace"
    assert stream._route_variant(Path("x.dmf")) == "ft1"
    for fmt in ("X-Tracker", "DSMI Compact", "Software Visions"):
        ext, uade = stream._render_ident("/m/b.dmf", {"format": fmt})
        assert ext == ".dmf" and not uade and ext not in stream._PSF_STREAM_EXTS


def test_cast_routes_it_like_a_play():
    from soniqboom.api import cast_stream, stream
    from soniqboom.core import cast_render
    from soniqboom.core.conversion_cache import _cache_key
    assert cast_render.one_tune_route("a", "/m/a.dmf", {"format": "DefleMask"}) == ".deflemask"
    assert cast_render.one_tune_route("a", "/m/a.dmf", {"format": "X-Tracker"}) is None
    assert cast_render.is_rendered_format(".deflemask") and cast_render.is_rendered_format(".dmf")
    assert cast_render.rendered_cache_key("t1", ".deflemask") == _cache_key(
        "t1", "psf", subsong=0, variant=stream._own_variant("fami"))
    rec = SimpleNamespace(id="t2", format="DefleMask", genre=["Chiptune"], path="/m/a.dmf")
    assert cast_stream._ext_for("/m/a.dmf", rec) == ".deflemask"


def test_archive_members_are_checked_by_content(monkeypatch):
    from soniqboom.core import archive
    defle, other = _dmf(0x18, 0x04, b"T", b"A"), b"DDMF" + bytes(100)
    monkeypatch.setattr(archive, "OPENMPT_EXTRA_EXTS", frozenset())
    assert not archive.rejected_by_bytes("a.dmf", "a.dmf", lambda r: defle)
    assert archive.rejected_by_bytes("b.dmf", "b.dmf", lambda r: other)
    monkeypatch.setattr(archive, "OPENMPT_EXTRA_EXTS", frozenset({".dmf"}))
    assert not archive.rejected_by_bytes("b.dmf", "b.dmf", lambda r: other)


def test_the_skip_rules_and_families_know_it():
    from soniqboom.core import retro, skip_rules, unplayable
    skip_rules._tune_exts = None
    assert ".dmf" in skip_rules._exts()
    assert retro.coarse_family("DefleMask") == "chiptune"
    assert unplayable.KIND_KEYS["DefleMask"] == "furnace"


def test_modland_lists_the_deflemask_folder_as_deflemask(monkeypatch):
    from soniqboom.core import modland_catalog as mc
    assert mc._classify("Deflemask/DevEd/7_grand_cop.dmf") == mc.KIND_MUSIC
    assert mc.format_for("Deflemask/DevEd/7_grand_cop.dmf") == "DefleMask"
    assert mc.family_genre("Deflemask/DevEd/7_grand_cop.dmf") == ["Chiptune"]
    if ".dmf" in md.OPENMPT_EXTRA_EXTS:
        assert mc._classify("X-Tracker/Knuddel/one.dmf") == mc.KIND_MUSIC
        assert mc.format_for("X-Tracker/Knuddel/one.dmf") == "X-Tracker"
    # without a libopenmpt reading .dmf, only DefleMask's folder is listed
    monkeypatch.setattr(md, "OPENMPT_EXTRA_EXTS", frozenset())
    assert mc._classify("X-Tracker/Knuddel/one.dmf") == mc.KIND_OTHER
    assert mc._classify("Deflemask/DevEd/7_grand_cop.dmf") == mc.KIND_MUSIC


# ── Renders ──────────────────────────────────────────────────────────────────

@pytest.fixture
def furnace(monkeypatch):
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "furnace_path", FURNACE or "")


def _stats(path: Path) -> "tuple[float, int, int]":
    with wave.open(str(path)) as w:
        n, rate, ch = w.getnframes(), w.getframerate(), w.getnchannels()
        a = array.array("h")
        a.frombytes(w.readframes(n))
    return n / rate, max(map(abs, a), default=0), ch


@needs_furnace
@pytest.mark.asyncio
@pytest.mark.parametrize("name, secs", [("7_grand_cop.dmf", 119.47), ("DuckTales2.dmf", 70.40),
                                        ("JonTron.dmf", 32.00)])
async def test_deflemask_renders(name, secs, furnace, tmp_path, monkeypatch):
    from soniqboom.api import stream
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = await stream._render_psf(DEFLE / name)
    try:
        got, peak, ch = _stats(out)
        assert got == pytest.approx(secs, abs=0.1) and ch == 2 and peak > 1000
    finally:
        out.unlink(missing_ok=True)


@needs_furnace
@pytest.mark.asyncio
async def test_a_broken_deflemask_module_is_a_refusal(tmp_path, furnace):
    from soniqboom.api import stream
    from soniqboom.core import unplayable
    bad = tmp_path / "bad.dmf"
    bad.write_bytes((DEFLE / "DuckTales2.dmf").read_bytes()[:200])
    with pytest.raises(unplayable.RenderRefusal) as e:
        await stream._render_psf(bad)
    assert e.value.renderer == "furnace" and "DefleMask" in str(e.value.detail)


@pytest.mark.asyncio
async def test_no_furnace_is_a_501_naming_deflemask(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from soniqboom.api import stream
    from soniqboom.config import settings
    monkeypatch.setattr(settings, "furnace_path", "")
    monkeypatch.setattr(stream, "_find_renderer", lambda p, name: None)
    monkeypatch.setattr(stream.shutil, "which", lambda n: None)
    f = tmp_path / "x.dmf"
    f.write_bytes(_dmf(0x18, 0x04, b"T", b"A"))
    with pytest.raises(HTTPException) as e:
        await stream._render_psf(f)
    assert e.value.status_code == 501 and "DefleMask" in e.value.detail
