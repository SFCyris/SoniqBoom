# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ZX Spectrum (SAM Coupé, Amstrad CPC) tracker modules, played by zxtune123:
the scan's verdict (``metadata.zx_verdict``: zxtune's type for the suffix, the
module at the file's start for a suffix other files have), the metadata it
reads (title, author, length), the suffixes another engine has too (uade's
``.stp`` / ``.sqt``, libopenmpt's ``.psm``, a Stereo Sidplayer song's
``.str``), the PSF route they render on, an install without zxtune123 — and,
where zxtune123 and the reference modules are there (``SB_ZX_SAMPLES``,
default ``internal/testdata/zxspectrum``: one or more modules per format,
named by their suffix), real scans and renders whose length is the scan's."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import wave
from pathlib import Path

import pytest

from soniqboom.core import metadata as md
from soniqboom.core import uade_formats as uf

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = Path(os.environ.get("SB_ZX_SAMPLES") or ROOT / "internal/testdata/zxspectrum")
ZXTUNE = shutil.which("zxtune123")

INFO = (
    "Playing: a.asc\n"
    "Type:    ASC\tContainer:            \tProgram: ASC Sound Master v1.x\n"
    "Title:   STARICK KOZLODOEV\nAuthor:  AVIASOFT\n\n"
    "Time:    1:39.20\tLoop duration:  1:39.20\n"
    "Playing: a.asc?+7112\n"
    "Type:    COP\tContainer: RAW\tProgram: E-Tracker v1.x\n"
    "Time:    0:57.60\tLoop duration:  0:50.40\n"
)


def test_parse_zxtune_info_reads_the_first_module():
    info = md.parse_zxtune_info(INFO)
    assert info == {"type": "ASC", "container": "", "program": "ASC Sound Master v1.x",
                    "title": "STARICK KOZLODOEV", "author": "AVIASOFT",
                    "seconds": pytest.approx(99.2), "loop_seconds": pytest.approx(99.2)}
    assert md.parse_zxtune_info("") == {}
    assert md.parse_zxtune_info("Invalid parameter format 'x'.\n") == {}
    assert md._zx_clock("1:02:03.04") == pytest.approx(3723.04)
    assert md._zx_clock("junk") == 0.0


def test_zx_verdict_wants_the_suffixs_type_and_for_a_shared_suffix_the_files_start():
    asc = {"type": "ASC", "container": "", "seconds": 99.2}
    assert md.zx_verdict(".asc", asc)
    assert md.zx_verdict(".asc", {**asc, "type": "AS0"})       # (ASC 0.x under .asc too)
    assert not md.zx_verdict(".pt3", asc)                       # another format's type
    assert not md.zx_verdict(".asc", {**asc, "container": "RAW"})   # found inside: not listed
    assert not md.zx_verdict(".asc", {**asc, "seconds": 0.0})
    cop = {"type": "COP", "container": "RAW", "seconds": 57.6}
    assert md.zx_verdict(".cop", cop)          # (a SAM file with its player: the module inside)
    assert not md.zx_verdict(".cop", {})
    assert md.zx_verdict(".pt3", {"type": "TS", "container": "", "seconds": 1.0})


def test_every_zx_suffix_is_supported_and_named():
    for ext, (name, types) in md.ZX_FORMATS.items():
        assert ext in md.SUPPORTED_EXTENSIONS, ext
        assert types and all(t == t.upper() for t in types)
        if ext not in md.ZX_SHARED_EXTS:
            assert md.FORMAT_NAMES[ext] == name
            # (a uade prefix name never takes a ZX module from zxtune)
            assert uf.ext_owned_elsewhere(ext), ext
    # the suffixes uade owns keep uade's name for what isn't a ZX module
    for ext in (".stp", ".sqt"):
        assert md.FORMAT_NAMES.get(ext) != md.ZX_FORMATS[ext][0]
    assert md.ZX_SHARED_EXTS <= md.ZX_EXTS
    assert {".asc", ".chi", ".str", ".psm"} <= md._ZX_STRICT


def test_frontend_zx_names_mirror_the_backend():
    js = (ROOT / "soniqboom/frontend/js/utils.js").read_text()
    block = js.split("export const ZX_FORMAT_NAMES = new Set([", 1)[1].split("]);", 1)[0]
    assert set(re.findall(r"'([^']+)'", block)) == set(md.ZX_FORMAT_NAMES)
    assert "...ZX_FORMAT_NAMES" in js.split("export const PSF_FORMAT_NAMES")[1].split("]);")[0]


def test_routing_puts_zx_modules_on_the_psf_route():
    from soniqboom.api import stream as st
    for ext in md.ZX_EXTS - md.ZX_SHARED_EXTS:
        assert ext in st._PSF_STREAM_EXTS, ext
        assert st._own_engine(ext) is None                       # (zxtune123's)
        assert st._route_renderer(Path("x" + ext)) == "zxtune123"
        assert st._zxtune_kind(Path("x" + ext)) == "ZX Spectrum"
        assert st._render_ident("/m/x" + ext, {"format": md.ZX_FORMATS[ext][0]}) == (ext, False)
    assert st._ZX_ROUTE in st._PSF_STREAM_EXTS
    for ext in md.ZX_SHARED_EXTS:
        assert ext not in st._PSF_STREAM_EXTS, ext
        zx = {"format": md.ZX_FORMATS[ext][0]}
        assert st._render_ident(f"/m/x{ext}", zx) == (st._ZX_ROUTE, False), ext
        assert st._render_ident(f"/m/a.zip::x{ext}", zx)[0] == st._ZX_ROUTE
        # (the same suffix as the other engine's format keeps its route)
        assert st._render_ident(f"/m/x{ext}", {"format": "Something else"})[0] != st._ZX_ROUTE
    assert st._render_ident("/m/x.psm", {"format": "Epic MegaGames MASI"})[0] == ".psm"
    assert st._psf_render_plan(Path("/nonexistent/x.pt3")) == (0.0, 0.0)


def test_render_without_zxtune_says_so(monkeypatch):
    from soniqboom.api import stream as st
    monkeypatch.setattr(st, "_find_renderer", lambda *_a: None)
    monkeypatch.setattr(st._dsf, "binary_for", lambda _p: None)
    with pytest.raises(st.HTTPException) as exc:
        asyncio.run(st._render_psf(Path("/nonexistent/x.pt3")))
    assert exc.value.status_code == 501
    assert "ZX Spectrum tracker modules require it" in exc.value.detail
    assert "--slim" in exc.value.detail


def test_scan_without_zxtune(monkeypatch, tmp_path):
    """No zxtune123 (``install.sh --slim``): a suffix only ZX modules use is
    listed by its name (its play says zxtune123 is missing); one other files
    have too is not listed (only zxtune could tell)."""
    real = md._find_atari_binary
    monkeypatch.setattr(md, "_find_atari_binary",
                        lambda s, b: None if b == "zxtune123" else real(s, b))
    pt3 = tmp_path / "Speccy.pt3"
    pt3.write_bytes(b"ProTracker 3.5 compilation of " + bytes(range(200)))
    m = md.extract(pt3, "t1")
    assert m.format == "Pro Tracker 3" and m.title == "Speccy" and m.duration == 0.0
    for name in ("notes.asc", "x.chi", "y.psg"):
        f = tmp_path / name
        f.write_bytes(bytes(range(256)) * 4)
        with pytest.raises(md.NotPlayableHere):
            md.extract(f, "t2")


def test_a_stereo_sidplayer_str_is_a_companion_not_a_module(tmp_path):
    # (a C64 Sidplayer voice set: load address, three voice lengths, each
    # voice ending in HLT — what ``is_c64_sidplayer`` checks)
    raw = b"\x00\x00" + (4).to_bytes(2, "little") * 3 + b"\x00\x00\x01\x4f" * 3
    assert md.is_c64_sidplayer(raw)
    f = tmp_path / "andante.str"
    f.write_bytes(raw)
    with pytest.raises(md.NotPlayableHere, match="Stereo Sidplayer"):
        md.extract(f, "t3")


@pytest.mark.skipif(not ZXTUNE, reason="zxtune123 not installed")
def test_text_under_a_zx_suffix_is_not_listed(tmp_path):
    for name in ("readme.asc", "help.chi", "x.dst", "y.pdt"):
        f = tmp_path / name
        f.write_text("This is just a text file about nothing in particular.\n" * 20)
        with pytest.raises(md.NotPlayableHere):
            md.extract(f, "t4")


def _samples() -> "list[Path]":
    if not SAMPLES.is_dir():
        return []
    return sorted(p for p in SAMPLES.iterdir() if p.suffix.lower() in md.ZX_EXTS)


@pytest.mark.skipif(not ZXTUNE or not _samples(), reason="zxtune123 / ZX reference modules absent")
def test_real_modules_scan_with_zxtunes_metadata():
    seen = set()
    for f in _samples():
        m = md.extract(f, "zx")
        assert m.format == md.ZX_FORMATS[f.suffix.lower()][0], f.name
        assert m.duration > 5, f.name
        assert m.title
        seen.add(f.suffix.lower())
    assert len(seen) >= 10           # (the reference set covers most formats)


@pytest.mark.skipif(not ZXTUNE or not _samples(), reason="zxtune123 / ZX reference modules absent")
def test_real_renders_are_audible_and_as_long_as_the_scan_says(tmp_path):
    from soniqboom.api import stream as st
    picks = {}
    for f in _samples():
        picks.setdefault(f.suffix.lower(), f)
    for ext in (".pt3", ".stc", ".asc", ".sqt", ".chi", ".tfe", ".cop", ".stp"):
        f = picks.get(ext)
        if f is None:
            continue
        m = md.extract(f, "zx")
        out = asyncio.run(st._render_psf(f))
        try:
            with wave.open(str(out)) as w:
                secs = w.getnframes() / w.getframerate()
                assert (w.getframerate(), w.getnchannels()) == (44100, 2)
            assert abs(secs - m.duration) < 1.0, (f.name, secs, m.duration)
            assert st._wav_is_audible(out), f.name
        finally:
            Path(out).unlink(missing_ok=True)


@pytest.mark.skipif(not ZXTUNE or not _samples(), reason="zxtune123 / ZX reference modules absent")
def test_shared_suffix_modules_take_zxtune_after_the_other_engine(tmp_path):
    picks = {f.suffix.lower(): f for f in _samples()}
    for ext in (".stp", ".sqt", ".psm", ".str"):
        if ext not in picks:
            continue
        m = md.extract(picks[ext], "zx")
        assert m.format == md.ZX_FORMATS[ext][0], ext
